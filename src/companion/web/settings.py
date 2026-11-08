"""控制台可编辑的设置：字段定义、当前值与来源、保存与校验。

写入规则：
- 默认写进 `config.local.toml`（本机覆盖，不进仓库）。
- 如果这一项现在来自 `.env`（如 QQ_GROUPS），就改 `.env` 里对应的那一行，否则改了也会被 .env 覆盖。
- 密钥（API key、NapCat token）不在这里出现，也不能从网页修改。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from companion.config import (
    CONFIG_FILE,
    ENV_FILE,
    ENV_MAP,
    LOCAL_CONFIG_FILE,
    SECRET_PATHS,
    Config,
    ConfigError,
    load_config,
)


@dataclass(frozen=True)
class SettingField:
    path: str
    label: str
    kind: str  # text / number / bool / select / list / slider
    section: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    options: list[str] = field(default_factory=list)
    restart: bool = True  # 保存后要重启 companion qq / chat 才生效


FIELDS: list[SettingField] = [
    # 人格
    SettingField("persona.name", "名字", "text", "人格", "伙伴的名字，会出现在提示词和说明里"),
    # 模型
    SettingField("models.subscription.enabled", "使用 GPT 订阅", "bool", "模型", "关闭后直接用兜底 API"),
    SettingField("models.subscription.model", "订阅模型", "text", "模型",
                 "留空 = Codex 默认（gpt-6.1-sol，约 6.6 秒/条）；gpt-6-luna 更快（约 4.3 秒）"),
    SettingField("models.subscription.effort", "推理强度", "select", "模型",
                 "聊天建议 low：更快、更省额度", options=["low", "medium", "high"]),
    SettingField("models.subscription.switch_at_used_percent", "额度用到多少就切换", "slider",
                 "模型", "任一额度窗口达到这个百分比，就改用兜底 API，直到窗口重置",
                 min=50, max=100, step=1, unit="%"),
    SettingField("models.subscription.timeout_seconds", "订阅多久没响应就改用兜底", "number", "模型",
                 "GPT 订阅偶尔会卡住不回；等这么久没有任何响应，就这一条先用兜底 API，10 分钟后再试订阅",
                 min=15, max=300, step=5, unit="秒"),
    SettingField("models.fallback.model", "兜底模型", "text", "模型",
                 "DeepSeek 可用：deepseek-flash（便宜快）、deepseek-v4-pro（更强更贵）"),
    SettingField("models.fallback.monthly_budget_usd", "兜底 API 每月预算", "number", "模型",
                 "花到 80% 会告警，到 100% 停用", min=0, max=1000, step=1, unit="美元"),
    # 记忆
    SettingField("models.embedding.model", "Embedding 模型", "text", "记忆",
                 "本地 Ollama 模型名；更换后会自动重算全部向量"),
    SettingField("models.embedding.min_cosine", "相关度门槛", "slider", "记忆",
                 "向量相似度低于它的记忆视为不相关", min=0.3, max=0.9, step=0.01),
    SettingField("retrieval.limit", "每轮最多想起几条记忆", "number", "记忆", min=1, max=20, step=1, unit="条"),
    SettingField("retrieval.standing_facts", "常驻事实条数", "number", "记忆",
                 "每轮都带上的、关于对话者的事实", min=0, max=40, step=1, unit="条"),
    SettingField("retrieval.recent_episodes", "常驻共同经历", "number", "记忆",
                 min=0, max=10, step=1, unit="段"),
    SettingField("retrieval.recent_messages", "带上最近几条对话", "number", "记忆",
                 min=4, max=60, step=1, unit="条"),
    SettingField("retrieval.recent_hours", "最近对话的时间范围", "number", "记忆",
                 min=0.5, max=48, step=0.5, unit="小时"),
    SettingField("retrieval.session_idle_minutes", "空闲多久算会话结束", "number", "记忆",
                 "之后这段对话会被整理成记忆", min=5, max=240, step=5, unit="分钟"),
    # QQ
    SettingField("qq.groups", "允许进入的群", "list", "QQ", "群号，逗号分隔；留空则只私聊"),
    SettingField("qq.owner_accounts", "主人的 QQ 号", "list", "QQ", "可确认绑定、查看状态"),
    SettingField("qq.aliases", "叫她的名字", "list", "QQ", "群里提到这些词就算在叫她"),
    SettingField("qq.unsolicited_probability", "主动插话概率", "slider", "QQ",
                 "话题和她的记忆强相关时，插一句的概率", min=0, max=1, step=0.05),
    SettingField("qq.unsolicited_min_cosine", "插话所需的相关度", "slider", "QQ",
                 min=0.5, max=0.95, step=0.01),
    SettingField("qq.unsolicited_min_interval_minutes", "同一个群插话间隔", "number", "QQ",
                 min=1, max=240, step=1, unit="分钟"),
    SettingField("qq.max_replies_per_minute", "每分钟最多发言", "number", "QQ",
                 "防刷屏、降低风控风险", min=1, max=30, step=1, unit="条"),
    SettingField("qq.debounce_private_seconds", "私聊等对方说完", "number", "QQ",
                 "这段时间内的连续消息（文字 + 表情）合在一起只回一次", min=0, max=20, step=0.5, unit="秒"),
    SettingField("qq.debounce_group_seconds", "群聊等对方说完", "number", "QQ",
                 min=0, max=20, step=0.5, unit="秒"),
    SettingField("qq.conversation_seconds", "群聊对话延续时间", "number", "QQ",
                 "她回复某人后，这段时间里对方不用 @ 也能接着聊；每次回复都会刷新",
                 min=0, max=600, step=10, unit="秒"),
    SettingField("qq.peer_bots", "群里其他机器人的 QQ 号", "list", "QQ",
                 "和它们连着聊满下面的轮数就停下，防止两个机器人无限对聊"),
    SettingField("qq.max_bot_rounds", "和机器人最多连聊几轮", "number", "QQ",
                 "有真人说话就重新计数", min=1, max=50, step=1, unit="轮"),
    SettingField("qq.bot_pause_minutes", "停下后多久再理机器人", "number", "QQ",
                 "期间真人说话会立刻恢复", min=1, max=1440, step=1, unit="分钟"),
    SettingField("qq.others_join_conversation", "允许旁人加入对话", "bool", "QQ",
                 "对话进行中，其他人的话也交给她判断要不要接（会多用一些额度）"),
    SettingField("qq.backfill", "补上离线期间的群消息", "bool", "QQ",
                 "每次连上 NapCat，把服务没开时群里的消息补记进来（只记不回）"),
    SettingField("qq.backfill_max", "每个群最多补几条", "number", "QQ",
                 min=10, max=500, step=10, unit="条"),
    SettingField("qq.backfill_hours", "最多补到多久以前", "number", "QQ",
                 min=1, max=168, step=1, unit="小时"),
    SettingField("qq.reactions", "表情回应", "bool", "QQ",
                 "她可以给消息点一个表情回应，不想说话时也能表个态"),
    SettingField("qq.poke", "戳一戳", "bool", "QQ", "被戳了会回应，也会戳回去"),
    SettingField("qq.poke_cooldown_seconds", "戳一戳冷却", "number", "QQ",
                 "同一个聊天里隔这么久才理下一次戳一戳，防止来回戳", min=0, max=3600, step=10, unit="秒"),
    SettingField("qq.voice_in", "听语音", "bool", "QQ",
                 "发给她的语音用 QQ 自带的语音转文字听（群里没冲她来的不听）"),
    SettingField("qq.voice_out", "用声音说话", "bool", "QQ",
                 "她偶尔在群里用 QQ 的 AI 声音发语音（只在群里：私聊做不到，会被发到群里）"),
    SettingField("qq.voice_character", "声音", "select", "QQ", "QQ AI 声聊的角色",
                 options=["lucy-voice-f37", "lucy-voice-xueling", "lucy-voice-female1",
                          "lucy-voice-f38", "lucy-voice-suxinjiejie", "lucy-voice-daji"]),
    SettingField("qq.ocr", "读出图里的字", "bool", "QQ",
                 "她没看的图片，用 QQ 自带的文字识别读出图里的字（在本机做，不花模型额度）"),
    SettingField("qq.long_text_chars", "长内容打包成聊天记录", "number", "QQ",
                 "她一次说的话超过这么多字就打包成一条聊天记录发，不刷屏；0 = 不打包",
                 min=0, max=5000, step=50, unit="字"),
    # 看图
    SettingField("vision.enabled", "看图", "bool", "看图", "关闭后所有图片都只记作“[图片]”"),
    SettingField("vision.active_seconds", "说完话后多久内看群里的图", "number", "看图",
                 "群里没冲她来的图片，只在她刚说完话的这段时间里看", min=0, max=600, step=10, unit="秒"),
    # 表情包
    SettingField("stickers.enabled", "收藏和使用表情包", "bool", "表情包", "群聊和私聊共用一个表情库"),
    SettingField("stickers.send_probability", "想发时真的发出去的概率", "slider", "表情包",
                 "她觉得适合配表情时，按这个概率真的发", min=0, max=1, step=0.05),
    SettingField("stickers.min_interval_seconds", "同一个聊天里发表情的间隔", "number", "表情包",
                 min=0, max=1800, step=10, unit="秒"),
    SettingField("stickers.min_library", "库里至少几张才开始用", "number", "表情包",
                 min=1, max=50, step=1, unit="张"),
    SettingField("stickers.capacity", "表情库容量", "number", "表情包",
                 "满了会淘汰见得少、很久没见的", min=20, max=2000, step=10, unit="张"),
    # 技能（显示在“技能”页；section 以“技能”开头）
    SettingField("skills.enabled", "启用技能", "bool", "技能",
                 "总开关：关闭后她不会用任何技能（各技能还有自己的开关）"),
    SettingField("skills.max_rounds", "一次回复最多用几次技能", "number", "技能",
                 "比如先搜索、再打开某个网页，算两次", min=1, max=10, step=1, unit="次"),
    SettingField("skills.owner_max_rounds", "主人私聊时最多用几次", "number", "技能",
                 "当电脑助手用时经常要连着做好几步（找文件、读、存、发）", min=1, max=30, step=1, unit="次"),
    SettingField("skills.max_result_chars", "交回给她的结果最长", "number", "技能",
                 "太长会更慢、更耗额度", min=500, max=20000, step=500, unit="字"),
    SettingField("skills.search.enabled", "联网搜索与读网页", "bool", "技能·联网搜索",
                 "用 DeepSeek 的搜索服务（需要兜底 API key），每次约 0.0008 美元，计入 DeepSeek 月预算"),
    SettingField("skills.search.in_groups", "群聊里也能用", "bool", "技能·联网搜索",
                 "关闭后只在私聊里搜索"),
    SettingField("skills.image.enabled", "画画", "bool", "技能·画画",
                 "用 GPT 订阅自带的生图（DeepSeek 不能画）；每张约占 5 小时额度的 1%，额度快用完时自动暂停"),
    SettingField("skills.image.group_limit", "群聊限额：最多几张", "number", "技能·画画",
                 "所有群加起来共享；私聊不限", min=0, max=50, step=1, unit="张"),
    SettingField("skills.image.group_window_minutes", "群聊限额：时间窗口", "number", "技能·画画",
                 min=1, max=1440, step=1, unit="分钟"),
    SettingField("skills.image.queue_limit", "最多排几张", "number", "技能·画画",
                 "接连点了几张就排队一张张画（做花册时有用）", min=1, max=30, step=1, unit="张"),
    SettingField("skills.image.gif_frame_ms", "动图每帧多久", "number", "技能·画画",
                 "动图一共 16 帧；越小动得越快", min=30, max=1000, step=10, unit="毫秒"),
    SettingField("skills.image.gif_side", "动图最大边长", "number", "技能·画画",
                 "一张图切成 16 帧，每帧最大约 310 像素，再大也不会更清楚", min=64, max=512, step=8, unit="像素"),
    SettingField("skills.agent.enabled", "帮主人干活（Agent）", "bool", "技能·帮主人干活",
                 "只限主人私聊或本地命令行。独立的 Codex 进程：工作文件夹内可读写，其余只读，黑名单禁止访问；越权操作会发给你审批"),
    SettingField("skills.agent.workspace", "工作文件夹", "text", "技能·帮主人干活",
                 "助手只能在这里新建和修改文件；相对路径从项目根目录算；不能把 data 文件夹包进去"),
    SettingField("skills.agent.extra_deny", "额外的黑名单", "list", "技能·帮主人干活",
                 "逗号分隔的路径，连读都不允许。默认已包含 .env、记忆文件夹、Codex 登录信息、浏览器与密码管理器数据、NapCat"),
    SettingField("skills.agent.web_search", "允许联网搜索", "bool", "技能·帮主人干活",
                 "用 Codex 自带的搜索（沙箱里的命令本身连不了网）"),
    SettingField("skills.agent.effort", "推理强度", "select", "技能·帮主人干活",
                 "越高越仔细，也越慢、越耗额度", options=["low", "medium", "high"]),
    SettingField("skills.agent.task_timeout_minutes", "单个任务最长", "number", "技能·帮主人干活",
                 "超时会让助手停下并汇报进展", min=1, max=240, step=1, unit="分钟"),
    SettingField("skills.agent.approval_timeout_minutes", "审批等待时间", "number", "技能·帮主人干活",
                 "这段时间里没回 /批准 就算拒绝", min=1, max=120, step=1, unit="分钟"),
    SettingField("skills.agent.progress_every_seconds", "进度汇报间隔", "number", "技能·帮主人干活",
                 min=30, max=1800, step=30, unit="秒"),
    SettingField("skills.agent.deepseek_fallback", "订阅额度用完时改用 DeepSeek", "bool", "技能·帮主人干活",
                 "用 DeepSeek 模型驱动 Codex 继续干活，计入 DeepSeek 月预算"),
    SettingField("skills.agent.deepseek_model", "DeepSeek 模型", "select", "技能·帮主人干活",
                 "flash 便宜、能看图；v4-pro 更强", options=["deepseek-flash", "deepseek-v4-pro"]),
    SettingField("skills.group_agent.enabled", "群聊里的沙箱助手", "bool", "技能·群聊助手",
                 "群里的人能让她派任务：独立的 Codex 进程，只能读写这个群的文件夹，读不到电脑上别的文件，也不能联网。"
                 "越权一律拒绝，不会来问你。每次启动前自检，沙箱没生效就不跑"),
    SettingField("skills.group_agent.limit_per_group", "每个群最多几个任务", "number", "技能·群聊助手",
                 "只用 GPT 订阅额度，不用 DeepSeek 兜底", min=1, max=100, step=1, unit="个"),
    SettingField("skills.group_agent.limit_all_groups", "所有群加起来最多几个", "number", "技能·群聊助手",
                 min=1, max=500, step=1, unit="个"),
    SettingField("skills.group_agent.window_hours", "限额按多久算", "number", "技能·群聊助手",
                 min=1, max=168, step=1, unit="小时"),
    SettingField("skills.group_agent.task_timeout_minutes", "单个任务最长", "number", "技能·群聊助手",
                 "到时间强制结束，沙箱里的进程一起停", min=1, max=60, step=1, unit="分钟"),
    SettingField("skills.group_agent.effort", "推理强度", "select", "技能·群聊助手",
                 "越高越仔细，也越慢、越耗额度", options=["low", "medium", "high"]),
    SettingField("skills.group_agent.tool_paths", "沙箱里能用的程序", "list", "技能·群聊助手",
                 "逗号分隔的文件夹，只读。填整个盘、用户文件夹或项目文件夹会被忽略"),
    SettingField("skills.group_agent.extra_deny", "另外不许读的路径", "list", "技能·群聊助手",
                 "逗号分隔。少数软件把自己的文件夹权限设得很松，沙箱也挡不住，发现了就加进来"),
    SettingField("skills.files.enabled", "收发文件", "bool", "技能·发文件",
                 "收：私聊、@她、或她刚说完话时别人发的文件，存进工作文件夹的 inbox/（助手也能处理）；"
                 "发：这个聊天的文件夹和这个聊天收到的文件；主人私聊还能发这台电脑上的其他文件（黑名单除外）"),
    SettingField("skills.files.max_mb", "文件大小上限", "number", "技能·发文件",
                 "收发都不超过这么大", min=1, max=100, step=1, unit="MB"),
    SettingField("skills.files.preview_chars", "文本文件直接给她看多少字", "number", "技能·发文件",
                 "txt、md、csv 这类小文件，把开头一段放进消息里", min=0, max=10000, step=100, unit="字"),
    SettingField("skills.files.send_owner_only", "替主人做的东西只给主人", "bool", "技能·发文件",
                 "管工作文件夹里替主人做的东西；每个聊天自己的文件夹和收到的文件不受影响"),
    SettingField("skills.files.send_in_groups", "替主人做的东西群里也能发", "bool", "技能·发文件",
                 "管工作文件夹里替主人做的东西；别的聊天的文件夹和收到的文件，打开了也发不了"),
    SettingField("skills.files.read_enabled", "读文件", "bool", "技能·读文件",
                 "她自己读文件（文本、PDF、Word、zip 里的文件），内容直接进她的上下文，也会发给模型服务。"
                 "任何聊天都能读这个聊天里收到的文件和这个聊天的文件夹"),
    SettingField("skills.files.read_local", "主人私聊时可读本机文件", "bool", "技能·读文件",
                 "只限主人私聊。密钥、记忆、登录信息、浏览器和密码管理器数据不能读（黑名单同“帮主人干活”）"),
    SettingField("skills.files.group_read_paths", "群聊也能读的文件夹", "list", "技能·读文件",
                 "逗号分隔。默认群聊读不到本机文件；想让群里查什么资料就放进来。只能读，不能发，黑名单照样不行"),
    SettingField("skills.files.read_chars", "长文件每段多少字", "number", "技能·读文件",
                 "一次读这么多，越大越耗额度", min=2000, max=100000, step=1000, unit="字"),
    SettingField("skills.files.read_max_mb", "能读的文件大小上限", "number", "技能·读文件",
                 min=1, max=200, step=1, unit="MB"),
    SettingField("skills.files.folder_enabled", "每个聊天有自己的文件夹", "bool", "技能·写文件",
                 "工作文件夹的 chats/ 下一个聊天一个文件夹：能存新文件和图片、能读、能发给这个聊天里的任何人。"
                 "只新建，不改已有文件；可以运行的文件类型不能写；别的聊天碰不到"),
    SettingField("skills.files.folder_max_mb", "每个文件夹最多多大", "number", "技能·写文件",
                 "群和别人的私聊；主人私聊不限", min=10, max=10000, step=10, unit="MB"),
    SettingField("skills.history.enabled", "翻聊天记录、看记录里的图", "bool", "技能·聊天记录",
                 "她能翻记录、回头看当时没看的图，也能存图、发图。关掉后消息后面也不再标图片编号"),
    SettingField("skills.history.scope", "群聊和别人的私聊能翻哪些记录", "select", "技能·聊天记录",
                 "主人私聊什么都能翻。self：只能翻当前聊天；groups：群里还能翻别的群（私聊的不行）；"
                 "all：任何聊天都能翻任何记录，包括你的私聊，别人可以借此问出你私聊里说过的话和读过的文件内容",
                 options=["self", "groups", "all"]),
    SettingField("skills.history.page_size", "一次翻多少条", "number", "技能·聊天记录",
                 "越多越耗额度", min=5, max=100, step=5, unit="条"),
    SettingField("skills.reminders.enabled", "定时提醒", "bool", "技能·定时提醒",
                 "只限私聊。关掉后不能再设，没到点的提醒先留着，打开后再发（错过的会补发）"),
    SettingField("skills.reminders.owner_only", "只给主人设提醒", "bool", "技能·定时提醒",
                 "关闭时，任何私聊她的人都能让她提醒自己"),
    SettingField("skills.reminders.max_per_person", "每个人最多几条", "number", "技能·定时提醒",
                 min=1, max=200, step=1, unit="条"),
    SettingField("skills.screen.enabled", "看屏幕", "bool", "技能·看屏幕",
                 "打开前想清楚：屏幕上的一切（密码、别人的消息、工作资料）都会截成图发给模型服务（GPT 订阅或 DeepSeek）。"
                 "只限主人私聊，你明确要求时她才看；截图看完就删，不进记忆，但她回复你的话照常记录"),
    SettingField("skills.clipboard.enabled", "读剪贴板", "bool", "技能·读剪贴板",
                 "打开前想清楚：剪贴板里的文字或图片会发给模型服务（GPT 订阅或 DeepSeek），手动复制的密码也一样。"
                 "只限主人私聊，你明确要求时她才读；密码管理器复制的内容不读；内容只留在这一轮，不进记忆，"
                 "但她回复你的话照常记录"),
    SettingField("skills.search.count", "每次搜索看几条结果", "number", "技能·联网搜索",
                 "越多越全面，但更慢、更耗额度", min=1, max=20, step=1, unit="条"),
    # 今日状态与心情
    SettingField("life.day.enabled", "今日状态（AI 版日常）", "bool", "今日状态与心情",
                 "每天睡眠后排好当天的安排；回复时带上已经过去的时段和今天真实发生的事"),
    SettingField("life.day.history_days", "排安排时参考最近几天", "number", "今日状态与心情",
                 "让今天和前几天连贯（书接着读），又不重样", min=0, max=14, step=1, unit="天"),
    SettingField("life.day.max_happenings", "“今天发生的事”最多列几条", "number", "今日状态与心情",
                 min=0, max=20, step=1, unit="条"),
    SettingField("life.mood.enabled", "心情", "bool", "今日状态与心情",
                 "她在回复末尾标出心情变化；心情会带到别的聊天里，并随时间回落到当天的基调"),
    SettingField("life.mood.half_life_minutes", "心情减弱一半要多久", "number", "今日状态与心情",
                 min=10, max=600, step=5, unit="分钟"),
    SettingField("life.mood.negative_half_life_minutes", "难过、生气减弱一半要多久", "number",
                 "今日状态与心情", "一般比开心回落得快", min=10, max=600, step=5, unit="分钟"),
    # 主动找主人
    SettingField("life.owner_chat.enabled", "主动找主人", "bool", "主动找主人",
                 "只在主人私聊里主动开口；群聊从不主动"),
    SettingField("life.owner_chat.greetings", "早安、晚安", "bool", "主动找主人"),
    SettingField("life.owner_chat.morning", "早安时间", "text", "主动找主人",
                 "HH:MM，每天前后随机几分钟"),
    SettingField("life.owner_chat.night", "晚安时间", "text", "主动找主人", "HH:MM"),
    SettingField("life.owner_chat.jitter_minutes", "时间前后随机", "number", "主动找主人",
                 min=0, max=60, step=1, unit="分钟"),
    SettingField("life.owner_chat.max_per_day", "每天最多主动找几次", "number", "主动找主人",
                 "含早晚安", min=0, max=20, step=1, unit="次"),
    SettingField("life.owner_chat.min_gap_minutes", "两次之间至少隔", "number", "主动找主人",
                 min=10, max=1440, step=10, unit="分钟"),
    SettingField("life.owner_chat.quiet_start", "安静时段开始", "text", "主动找主人",
                 "HH:MM；这段时间不主动分享（早晚安按设定时间）"),
    SettingField("life.owner_chat.quiet_end", "安静时段结束", "text", "主动找主人", "HH:MM"),
    SettingField("life.owner_chat.skip_if_chatted_minutes", "刚聊过就不主动找", "number",
                 "主动找主人", "这段时间内聊过天就不再主动开口", min=0, max=600, step=5, unit="分钟"),
    # 自主活动
    SettingField("life.activities.enabled", "额度充足时自主活动", "bool", "自主活动",
                 "5 小时额度快重置、却还剩很多时，她按心情和兴趣自己找点事做。只用 GPT 订阅，在用 DeepSeek 时不做"),
    SettingField("life.activities.trigger_minutes_before_reset", "距离重置不到多久开始", "number",
                 "自主活动", min=10, max=300, step=10, unit="分钟"),
    SettingField("life.activities.trigger_used_below", "5 小时额度用了不到多少才开始", "slider",
                 "自主活动", min=0, max=100, step=1, unit="%"),
    SettingField("life.activities.budget_percent", "每个 5 小时窗口最多花掉", "slider", "自主活动",
                 "按窗口用量的增加来算", min=1, max=50, step=1, unit="%"),
    SettingField("life.activities.weekly_max_percent", "7 天额度用到多少就不做", "slider", "自主活动",
                 min=10, max=100, step=1, unit="%"),
    SettingField("life.activities.min_gap_minutes", "两件事之间至少隔", "number", "自主活动",
                 min=5, max=240, step=5, unit="分钟"),
    SettingField("life.activities.max_per_window", "每个窗口最多做几件", "number", "自主活动",
                 min=1, max=50, step=1, unit="件"),
    SettingField("life.activities.explore", "上网查感兴趣的东西、收集趣事", "bool", "自主活动",
                 "用 Codex 自带的联网搜索（不花 DeepSeek 的钱）"),
    SettingField("life.activities.write", "写短诗、随笔", "bool", "自主活动",
                 "写的东西存在记忆文件夹的 self/writings/"),
    SettingField("life.activities.draw", "画画", "bool", "自主活动", "每张约占 5 小时额度的 1%"),
    SettingField("life.activities.chat_owner", "找主人聊天", "bool", "自主活动",
                 "还受“主动找主人”的次数、间隔和安静时段限制"),
    # 睡眠
    SettingField("sleep.hour", "每天几点开始睡眠", "number", "睡眠",
                 "整理、遗忘、反思、写日记", min=0, max=23, step=1, unit="点"),
    SettingField("sleep.identity_every_days", "自我认知多久反思一次", "number", "睡眠",
                 min=1, max=60, step=1, unit="天"),
]  # fmt: skip

_BY_PATH = {f.path: f for f in FIELDS}
_ENV_BY_PATH = {".".join(p): var for var, p in ENV_MAP.items()}


class SettingsError(ValueError):
    pass


def describe(root: Path) -> list[dict[str, Any]]:
    """每个字段的当前值、默认值与来源。"""
    config = load_config(root)
    layers = _layers(root)
    out = []
    for f in FIELDS:
        keys = tuple(f.path.split("."))
        value = _get(config.model_dump(mode="json"), keys)
        out.append(
            {
                **asdict(f),
                "value": value,
                "default": _get(Config().model_dump(mode="json"), keys),
                "source": _source(keys, layers),
                "env_var": _ENV_BY_PATH.get(f.path),
            }
        )
    return out


def save(root: Path, changes: dict[str, Any]) -> list[str]:
    """校验并保存；返回写入了哪些文件。任何一项不合法就什么都不写。"""
    unknown = [p for p in changes if p not in _BY_PATH]
    if unknown:
        raise SettingsError(f"不能修改这些设置：{', '.join(unknown)}")
    if any(tuple(p.split(".")) in SECRET_PATHS for p in changes):
        raise SettingsError("密钥不能从网页修改")

    layers = _layers(root)
    local = dict(layers["local"])
    env_updates: dict[str, str] = {}
    for path, raw in changes.items():
        value = _coerce(_BY_PATH[path], raw)
        keys = tuple(path.split("."))
        env_var = _ENV_BY_PATH.get(path)
        if env_var and env_var in layers["dotenv"] and env_var not in os.environ:
            if value is None:
                env_updates[env_var] = ""  # 空值视为未设置
            else:
                env_updates[env_var] = ",".join(value) if isinstance(value, list) else str(value)
        elif env_var and env_var in os.environ:
            raise SettingsError(f"{path} 由系统环境变量 {env_var} 设定，请在系统里修改")
        elif value is None:
            _delete(local, keys)
        else:
            _set(local, keys, value)

    # 先在内存里合成新配置并校验，通过后才落盘
    try:
        _validate(root, local, env_updates)
    except ConfigError as e:
        raise SettingsError(str(e)) from e

    written = []
    if local != layers["local"]:
        (root / LOCAL_CONFIG_FILE).write_text(dump_toml(local), encoding="utf-8", newline="\n")
        written.append(LOCAL_CONFIG_FILE)
    if env_updates:
        update_env_file(root / ENV_FILE, env_updates)
        written.append(ENV_FILE)
    return written


def reset(root: Path, path: str) -> None:
    """从本机设置里删掉这一项，回到默认值（.env 里的不动）。"""
    if path not in _BY_PATH:
        raise SettingsError(f"没有这个设置：{path}")
    layers = _layers(root)
    local = dict(layers["local"])
    _delete(local, tuple(path.split(".")))
    (root / LOCAL_CONFIG_FILE).write_text(dump_toml(local), encoding="utf-8", newline="\n")


# ---- 内部 ----


def _layers(root: Path) -> dict[str, Any]:
    def read(name: str) -> dict[str, Any]:
        p = root / name
        if not p.is_file():
            return {}
        with p.open("rb") as f:
            return tomllib.load(f)

    return {
        "toml": read(CONFIG_FILE),
        "local": read(LOCAL_CONFIG_FILE),
        "dotenv": {k: v for k, v in dotenv_values(root / ENV_FILE).items() if v},
    }


def _source(keys: tuple[str, ...], layers: dict[str, Any]) -> str:
    env_var = _ENV_BY_PATH.get(".".join(keys))
    if env_var and os.environ.get(env_var):
        return "环境变量"
    if env_var and env_var in layers["dotenv"]:
        return ".env"
    if _has(layers["local"], keys):
        return "本机设置"
    if _has(layers["toml"], keys):
        return "config.toml"
    return "默认"


def _validate(root: Path, local: dict[str, Any], env_updates: dict[str, str]) -> Config:
    """用将要写入的内容走一遍正式的加载流程。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        for name in (CONFIG_FILE, ENV_FILE):
            if (root / name).exists():
                (tmp_root / name).write_bytes((root / name).read_bytes())
        (tmp_root / LOCAL_CONFIG_FILE).write_text(dump_toml(local), encoding="utf-8")
        if env_updates:
            update_env_file(tmp_root / ENV_FILE, env_updates)
        return load_config(tmp_root)


def _coerce(f: SettingField, raw: Any) -> Any:
    if raw is None or (isinstance(raw, str) and raw.strip() == "" and f.kind in ("text",)):
        return None
    try:
        if f.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("1", "true", "yes", "on")
        if f.kind in ("number", "slider"):
            num = float(raw)
            if f.min is not None and num < f.min or f.max is not None and num > f.max:
                raise SettingsError(f"{f.label} 应在 {f.min} 到 {f.max} 之间")
            return int(num) if f.step is not None and float(f.step).is_integer() else num
        if f.kind == "list":
            items = raw if isinstance(raw, list) else re.split(r"[,，\s]+", str(raw))
            return [str(i).strip() for i in items if str(i).strip()]
        if f.kind == "select":
            if raw not in f.options:
                raise SettingsError(f"{f.label} 只能是 {', '.join(f.options)}")
            return raw
        return str(raw).strip()
    except (TypeError, ValueError) as e:
        if isinstance(e, SettingsError):
            raise
        raise SettingsError(f"{f.label} 的值不合法：{raw}") from e


def _get(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return data


def _has(data: dict[str, Any], keys: tuple[str, ...]) -> bool:
    for k in keys:
        if not isinstance(data, dict) or k not in data:
            return False
        data = data[k]
    return True


def _set(data: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    for k in keys[:-1]:
        data[k] = dict(data.get(k) or {})
        data = data[k]
    data[keys[-1]] = value


def _delete(data: dict[str, Any], keys: tuple[str, ...]) -> None:
    node = data
    for k in keys[:-1]:
        if k not in node:
            return
        node[k] = dict(node[k])
        node = node[k]
    node.pop(keys[-1], None)


def dump_toml(data: dict[str, Any]) -> str:
    """把嵌套字典写成 TOML（只支持本项目用到的标量、字符串列表与表）。"""
    lines = ["# 本机设置（由控制台或手工维护），会覆盖 config.toml；不进仓库", ""]

    def scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int | float):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(scalar(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    def table(prefix: str, node: dict[str, Any]) -> None:
        plain = {k: v for k, v in node.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in node.items() if isinstance(v, dict)}
        if plain:
            if prefix:
                lines.append(f"[{prefix}]")
            lines.extend(f"{k} = {scalar(v)}" for k, v in plain.items())
            lines.append("")
        for k, v in nested.items():
            table(f"{prefix}.{k}" if prefix else k, v)

    table("", data)
    return "\n".join(lines).rstrip() + "\n"


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """只改 .env 里对应的那几行，其余内容（含密钥和注释）原样保留。"""
    text = path.read_text("utf-8") if path.exists() else ""
    lines = text.splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
