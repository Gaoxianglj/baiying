"""配置加载。

优先级从低到高：代码默认值 < config.toml < config.local.toml < .env < 进程环境变量。
密钥（API key）只能来自 .env 或环境变量；写进 toml 文件会直接报错，避免误提交。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

CONFIG_FILE = "config.toml"
LOCAL_CONFIG_FILE = "config.local.toml"
ENV_FILE = ".env"

# 环境变量名 → 配置路径
ENV_MAP: dict[str, tuple[str, ...]] = {
    "COMPANION_DATA_DIR": ("paths", "data_dir"),
    "FALLBACK_BASE_URL": ("models", "fallback", "base_url"),
    "FALLBACK_API_KEY": ("models", "fallback", "api_key"),
    "FALLBACK_MODEL": ("models", "fallback", "model"),
    "FALLBACK_MONTHLY_BUDGET_USD": ("models", "fallback", "monthly_budget_usd"),
    "QQ_BOT_ACCOUNT": ("qq", "bot_account"),
    "NAPCAT_TOKEN": ("qq", "access_token"),
    "QQ_WS_URL": ("qq", "ws_url"),
    "QQ_OWNER_ACCOUNTS": ("qq", "owner_accounts"),
    "QQ_GROUPS": ("qq", "groups"),
    "QQ_PEER_BOTS": ("qq", "peer_bots"),
}

# 只允许出现在环境变量里的配置路径
SECRET_PATHS: set[tuple[str, ...]] = {
    ("models", "fallback", "api_key"),
    ("qq", "access_token"),
}


class ConfigError(ValueError):
    pass


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Section):
    data_dir: Path = Path("data")


class PersonaConfig(_Section):
    name: str = "白璎"


class SubscriptionConfig(_Section):
    """GPT 订阅，经 Codex app-server 调用。"""

    enabled: bool = True
    codex_command: str = "codex"
    codex_home: Path | None = None  # 独立保存 Codex 登录与会话，不混入桌面端
    model: str | None = None  # None 表示用 Codex 的默认模型；可选值见 app-server 的 model/list
    effort: str = "low"  # 聊天要快，推理强度调低
    switch_at_used_percent: float = 99.0  # 任一额度窗口用到这个百分比就切到兜底 API
    timeout_seconds: float = 120.0
    persistent_chats: bool = True  # 普通聊天按频道保留线程；辅助模型调用保持隔离
    native_tools: bool = True  # 原生结构化技能调用；不兼容时回到原标记执行链

    @field_validator("codex_home")
    @classmethod
    def absolute_codex_home(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("codex_home 必须为绝对路径")
        return value


class FallbackConfig(_Section):
    """兜底 API（OpenAI 兼容接口），订阅额度耗尽时使用。"""

    base_url: str = "https://api.deepseek.com"
    api_key: SecretStr | None = None
    model: str | None = None
    monthly_budget_usd: float = 20.0
    # 每百万 token 的美元价格，用于估算花费；默认取 deepseek-flash 的高峰价（偏保守）
    price_input_per_m: float = 0.3
    price_output_per_m: float = 1.2
    timeout_seconds: float = 120.0
    # 原样并入请求体的额外字段；DeepSeek 默认开思考模式，聊天要快，默认关掉
    extra_body: dict[str, Any] = {"thinking": {"type": "disabled"}}


class EmbeddingConfig(_Section):
    """本地 embedding 模型（Ollama 的 OpenAI 兼容接口）。换模型后向量会自动重算。"""

    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3-embedding:4b"
    query_instruction: str = ""  # 查询前缀；评测显示 qwen3-embedding:4b 不加前缀更好
    min_cosine: float = (
        0.55  # 低于它的纯向量结果视为不相关（随模型而定，见 scripts/eval_embeddings.py）
    )
    batch_size: int = 32
    timeout_seconds: float = 60.0


class RetrievalConfig(_Section):
    text_weight: float = (
        0.0  # 关键词一路的权重；评测显示向量单独排序最好，关键词只在向量不可用时兜底
    )
    limit: int = 8  # 每轮最多放进上下文的检索结果
    min_relevance: float = 0.0
    standing_facts: int = 12  # 每轮常驻的、关于对话者的事实条数
    recent_episodes: int = 3  # 每轮常驻的、和对话者最近的共同经历
    recent_messages: int = 20  # 带上同一频道最近多少条对话
    recent_hours: float = 6.0  # 只带这么多小时以内的对话
    session_idle_minutes: float = 30.0  # 空闲多久算一段会话结束，之后抽取成记忆


class LocalChatConfig(_Section):
    """本地对话模型（预留，阶段 3 评估）：订阅和兜底 API 都不可用时的最后一道兜底。"""

    enabled: bool = False
    base_url: str = "http://localhost:11434/v1"
    model: str | None = None


class ModelsConfig(_Section):
    subscription: SubscriptionConfig = SubscriptionConfig()
    fallback: FallbackConfig = FallbackConfig()
    local: LocalChatConfig = LocalChatConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()


class QQConfig(_Section):
    """QQ 身体：经 NapCat 的 OneBot 11 正向 WebSocket 接入。"""

    bot_account: str | None = None
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: SecretStr | None = None  # NapCat WebSocket 服务的 token，只能放 .env
    owner_accounts: list[str] = []  # 你的 QQ 号：可以确认绑定、删除任何人的记忆
    groups: list[str] = []  # 允许接入的群号；为空表示不进任何群（只私聊）
    aliases: list[str] = ["白璎", "小璎"]  # 群里提到这些名字就算在叫她
    unsolicited_probability: float = 0.3  # 没被叫到、但话题和她的记忆强相关时插话的概率
    unsolicited_min_cosine: float = 0.7  # 话题与某条记忆的向量相似度至少这么高才考虑插话
    unsolicited_min_interval_minutes: float = 15.0  # 同一个群里两次主动插话的最短间隔
    max_replies_per_minute: int = 6  # 全局发言上限，防刷屏、降低风控风险
    # 消息合并：要回的消息先等这么久，把同一个聊天里接连发来的几条合成一轮再回
    debounce_private_seconds: float = 3.0
    debounce_group_seconds: float = 4.0
    # 对话延续：她在群里说完话后这么久内算“还在聊”，每回复一次就刷新
    conversation_seconds: float = 90.0
    others_join_conversation: bool = True  # 活跃期内别人插话也交给模型判断（更严格）
    # 防止和别的机器人无限对聊：和它们连着聊满 max_bot_rounds 轮就停下，
    # 直到有真人说话、或过了 bot_pause_minutes 分钟
    # 补上离线期间的群消息：连上 NapCat 时，把服务没开时群里的消息补记进来（只记不回）
    backfill: bool = True
    backfill_max: int = 100  # 每个群最多补多少条
    backfill_hours: float = 24.0  # 最多补到多久以前
    reactions: bool = True  # 她可以给消息点表情回应【回应：😂】
    poke: bool = True  # 被戳一戳会回应，也可以戳回去【戳一戳】
    poke_cooldown_seconds: float = 60.0  # 同一个聊天里多久才理一次戳一戳（防止来回戳）
    voice_in: bool = True  # 语音消息用 QQ 自带的语音转文字听
    voice_out: bool = True  # 她可以在群里用 QQ 的 AI 声音说话【语音：…】（私聊做不到）
    voice_character: str = "lucy-voice-f37"  # 声音（文艺少女）；可选见 NapCat get_ai_characters
    ocr: bool = True  # 没看的图片用 QQ 自带的文字识别读出图里的字（本机，不花额度）
    long_text_chars: int = 300  # 她一次说的话超过这么多字，就打包成聊天记录发；0 = 不打包
    peer_bots: list[str] = []  # 其他机器人的 QQ 号
    max_bot_rounds: int = 5
    bot_pause_minutes: float = 10.0

    @field_validator("owner_accounts", "groups", "aliases", "peer_bots", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    conversation_followups: int = Field(default=2, ge=0, le=20)


class VisionConfig(_Section):
    """看图（交给主模型：GPT 订阅 → DeepSeek）。

    私聊、群里 @她 或引用并 @她 的图片都看；群里没冲她来的图片，只在她“活跃”时
    （在这个群说完话后的 active_seconds 秒内）才看，其余时候只记作“[图片]”，也不下载。
    """

    enabled: bool = True
    active_seconds: float = 60.0
    max_side: int = 1024  # 长边缩到这个像素，省时间和 token
    max_bytes: int = 10_000_000


class StickerConfig(_Section):
    """表情包：群聊和私聊共用一个库。"""

    enabled: bool = True
    capacity: int = 300  # 库里最多存多少张，满了淘汰见得少、很久没见的
    min_library: int = 3  # 库里至少有这么多张，才告诉她可以发表情包
    send_probability: float = 0.6  # 她想发时，真的发出去的概率
    min_interval_seconds: float = 120.0  # 同一个聊天里两次发表情的最短间隔


class SleepConfig(_Section):
    """每日“睡眠”：反思、遗忘、合并重复事实。"""

    hour: int = 4  # 本地时间几点开始
    identity_every_days: int = 7  # 自我认知多久反思一次


class MoodConfig(_Section):
    """心情：她在回复末尾写【心情：词｜原因】，之后随时间回落到当天的基调。"""

    enabled: bool = True
    half_life_minutes: float = 90.0  # 心情减弱一半要多久
    negative_half_life_minutes: float = 45.0  # 难过、生气这类回落得更快


class DayConfig(_Section):
    """今日状态（AI 版日常）：每天生成当天的安排；回复时带上“你的今天”。"""

    enabled: bool = True
    history_days: int = 3  # 生成安排时参考最近几天的安排和日记
    max_happenings: int = 6  # “今天真实发生的”最多列几条


class OwnerChatConfig(_Section):
    """主动找主人：只在主人私聊里主动开口（早晚安、分享自己做的事）；群聊从不主动。"""

    enabled: bool = True
    greetings: bool = True  # 早安、晚安
    morning: str = "08:30"  # 本地时间；每天前后随机 jitter_minutes 分钟
    night: str = "23:30"
    jitter_minutes: float = 20.0
    max_per_day: int = 4  # 每天最多主动找几次（含早晚安）
    min_gap_minutes: float = 120.0  # 两次主动找之间至少隔多久
    quiet_start: str = "00:30"  # 这段时间不主动找（晚安除外）
    quiet_end: str = "08:00"
    skip_if_chatted_minutes: float = 60.0  # 刚聊过就不主动找

    @field_validator("morning", "night", "quiet_start", "quiet_end")
    @classmethod
    def _clock(cls, value: str) -> str:
        hour, _, minute = value.partition(":")
        if not (hour.isdigit() and minute.isdigit() and int(hour) < 24 and int(minute) < 60):
            raise ValueError(f"时间格式应为 HH:MM：{value}")
        return f"{int(hour):02d}:{int(minute):02d}"


class ActivityConfig(_Section):
    """额度充足时自主活动：只用 GPT 订阅额度，在用 DeepSeek 时一律不做。

    5 小时窗口快重置、却还剩很多额度时，按心情、日程和兴趣挑一件事做：
    联网查感兴趣的东西或趣事、写短诗随笔、画画、找主人聊天。
    """

    enabled: bool = True
    trigger_used_below: float = 15.0  # 5 小时窗口用量低于这个百分比才开始
    trigger_minutes_before_reset: float = 90.0  # 距离 5 小时窗口重置不到这么久才开始
    budget_percent: float = 15.0  # 每个 5 小时窗口里，自主活动最多花掉多少
    weekly_max_percent: float = 60.0  # 7 天窗口用到这么多就不做
    min_gap_minutes: float = 20.0  # 两件事之间至少隔多久
    max_per_window: int = 8  # 每个窗口最多做几件（额度读数不准时的保险）
    explore: bool = True  # 联网查感兴趣的东西、收集趣事
    write: bool = True  # 写短诗、随笔
    draw: bool = True  # 画画
    chat_owner: bool = True  # 找主人聊天（还受“主动找主人”的限制）
    timeout_seconds: float = 600.0  # 后台做事，慢一点没关系；画图偶尔要三四分钟


class LifeConfig(_Section):
    """情感与生活：今日状态、心情、主动找主人、自主活动。"""

    day: DayConfig = DayConfig()
    mood: MoodConfig = MoodConfig()
    owner_chat: OwnerChatConfig = OwnerChatConfig()
    activities: ActivityConfig = ActivityConfig()


class SearchSkillConfig(_Section):
    """联网搜索与读网页：DeepSeek Search / Fetch API（用兜底 API 的 key，计入它的月预算）。"""

    enabled: bool = True
    in_groups: bool = True  # 群聊里也能用
    count: int = 5  # 每次搜索取几条结果
    price_per_call_usd: float = 0.00075  # 搜索、抓取各 0.75 美元 / 千次（只有成功返回内容才计费）
    timeout_seconds: float = 30.0


class ImageSkillConfig(_Section):
    """生图：Codex 内置生图（只用 GPT 订阅；额度用尽时暂停）。私聊不限，群聊共享限额。"""

    enabled: bool = True
    group_limit: int = 5  # 所有群加起来，每个窗口最多画几张
    group_window_minutes: float = 20.0
    timeout_seconds: float = 600.0  # 实测偶尔要近 4 分钟（2026-10-26），原来的 300 秒太紧
    queue_limit: int = 5  # 最多排多少张（一张一张画；做花册时可以一次点好几张）
    gif_frame_ms: int = 120  # 动图每帧多少毫秒
    gif_side: int = 320  # 动图长边最多多少像素（一张图切 16 帧，每帧约 310 像素）


class AgentSkillConfig(_Section):
    """Agent：独立的 Codex 进程替主人干活。只限主人私聊或本地命令行。

    权限：工作文件夹内可读写，其余位置只读，黑名单（密钥、记忆、浏览器与密码管理器数据等）连读都不行；
    越权操作会转给主人审批。订阅额度用尽时可改用 DeepSeek 驱动 Codex。
    """

    enabled: bool = True
    workspace: Path = Path("data/workspace")  # 相对于项目根目录
    extra_deny: list[str] = []  # 额外的黑名单路径（绝对路径，或相对于项目根目录）
    effort: str = "medium"
    web_search: bool = True  # 允许 Codex 自带的联网搜索
    task_timeout_minutes: float = 30.0
    approval_timeout_minutes: float = 10.0  # 主人多久没回审批就当拒绝
    progress_every_seconds: float = 120.0  # 多久汇报一次进度
    deepseek_fallback: bool = True  # 订阅额度用尽时改用 DeepSeek 驱动
    deepseek_model: str = "deepseek-flash"

    @field_validator("extra_deny", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


class GroupAgentConfig(_Section):
    """群聊里的沙箱助手：独立的 Codex 进程，只能读写这个群的文件夹，连不了网。

    群里的人能让她做任何事，所以限制由沙箱和程序保证：越权一律拒绝，不转给主人；
    只用 GPT 订阅额度，不用 DeepSeek 兜底。实测依据见 docs/project-plan.md 决策记录。
    """

    enabled: bool = False
    limit_per_group: int = 3  # 每个群在一个窗口里最多做几个任务
    limit_all_groups: int = 10  # 所有群加起来
    window_hours: float = 24.0
    task_timeout_minutes: float = 10.0
    effort: str = "low"
    # 沙箱里能用的程序（只读）；不存在的跳过。配成整个盘、用户文件夹或项目文件夹会被忽略
    tool_paths: list[str] = ["C:/Python314", "C:/Program Files/nodejs"]
    extra_deny: list[str] = []  # 另外不许读的路径（权限设得很松的软件目录，审计时发现了就加）

    @field_validator("tool_paths", "extra_deny", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


class FilesSkillConfig(_Section):
    """文件。每个聊天有自己的文件夹（工作文件夹的 chats/聊天/）：能存新文件、能读、能发。

    收到和生成的文件统一存进 chats/聊天/，不按日期分层。主人私聊还能读、发这台电脑上的其他文件
    （黑名单除外）。任何地方都不改已有文件：重名加序号。
    """

    enabled: bool = True
    max_mb: float = 10.0  # 收发都不超过这么大
    inbox: Path = Path("data/workspace/inbox")  # 历史收件根目录；关闭聊天文件夹时的新收件备用位置
    preview_chars: int = 1500  # 小的文本文件，把开头这么多字直接给她看
    # 下面两项只管工作文件夹里替主人做的东西；每个聊天自己的文件夹和收到的文件不受限
    send_owner_only: bool = True  # 只给主人发
    send_in_groups: bool = False  # 群里也能发（否则只在私聊）
    folder_enabled: bool = True  # 每个聊天有自己的文件夹
    folder_max_mb: float = 500.0  # 群和别人私聊的文件夹最多这么大（主人私聊不限）
    group_read_paths: list[str] = []  # 另外允许群聊读的文件夹（只读，不能发）
    read_enabled: bool = True  # 她能自己读文件（内容进她的上下文，不经过助手）
    read_local: bool = True  # 主人私聊时还能读这台电脑上的其他文件（黑名单除外）
    read_chars: int = 20000  # 长文件分段读，每段多少字
    read_max_mb: float = 20.0  # 超过这么大的文件不读
    pdf_vision: bool = True  # 扫描页用主模型识别；页图可用于图表和混合 PDF
    document_tools: bool = True  # 页图、页码检索与文档缓存；关闭可回到原文本提取
    pdf_batch_pages: int = 3  # 一次交给视觉模型的 PDF 页面数量

    @field_validator("group_read_paths", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


class HistorySkillConfig(_Section):
    """翻聊天记录、看记录里的图（包括当时没看的）。主人私聊什么记录都能翻。

    scope 管群聊和别人的私聊：self = 只能翻当前聊天；groups = 群里还能翻别的群
    （默认；私聊记录不行，别人的私聊也只能翻自己的）；all = 任何聊天都能翻任何记录，
    包括主人的私聊（主人私聊里读过的本机文件内容也可能因此被翻到）。
    """

    enabled: bool = True
    scope: Literal["self", "groups", "all"] = "groups"
    page_size: int = 30  # 一次翻多少条


class RemindersSkillConfig(_Section):
    """约定与提醒：私聊可用，群聊限熟络及以上（或主人）建立；含准点提醒、周期行动约定与持续约定。"""

    enabled: bool = True
    owner_only: bool = False  # 私聊是否只允许主人建立约定
    in_groups: bool = True  # 允许在接入的群聊里建立约定
    group_min_familiarity: Literal["初识", "相熟", "熟络", "亲近"] = "熟络"
    max_per_person: int = 20  # 每个聊天最多同时保留几条约定


class ScreenSkillConfig(_Section):
    """看屏幕：只限主人私聊、主人明确要求时。截图会发给模型服务，所以默认关。"""

    enabled: bool = False


class ClipboardSkillConfig(_Section):
    """读剪贴板：只限主人私聊、主人明确要求时。内容会发给模型服务，所以默认关。"""

    enabled: bool = False


class DiceSkillConfig(_Section):
    """真实骰子：只用程序随机数，按群内约定的CoC七版规则检定。"""

    enabled: bool = True


class SkillsConfig(_Section):
    """技能：她在回复里写【标记：参数】，程序执行后把结果交回给她再接着说。"""

    enabled: bool = True  # 总开关
    dice: DiceSkillConfig = DiceSkillConfig()
    max_rounds: int = 0  # 兼容旧配置，已不再限制技能次数
    owner_max_rounds: int = 0  # 兼容旧配置，已不再限制技能次数
    background_timeout_minutes: float = 30.0  # 后台任务的运行时限，不是技能次数限制
    max_result_chars: int = 6000  # 交回给她的结果最长多少字
    search: SearchSkillConfig = SearchSkillConfig()
    image: ImageSkillConfig = ImageSkillConfig()
    agent: AgentSkillConfig = AgentSkillConfig()
    group_agent: GroupAgentConfig = GroupAgentConfig()
    files: FilesSkillConfig = FilesSkillConfig()
    history: HistorySkillConfig = HistorySkillConfig()
    reminders: RemindersSkillConfig = RemindersSkillConfig()
    screen: ScreenSkillConfig = ScreenSkillConfig()
    clipboard: ClipboardSkillConfig = ClipboardSkillConfig()


class TrpgConfig(_Section):
    enabled: bool = True
    auto_route: bool = True
    narration: bool = True
    allow_paid_fallback: bool = False
    max_input_chars: int = Field(default=7000, ge=2000, le=32000)
    narration_chars: int = Field(default=500, ge=120, le=1500)
    history_events: int = Field(default=4, ge=0, le=12)
    context_turns: int = Field(default=8, ge=1, le=30)
    daily_model_calls: int = Field(default=120, ge=0, le=10000)
    daily_input_chars: int = Field(default=600000, ge=0, le=20_000_000)
    stop_at_used_percent: float = Field(default=85, ge=0, le=100)


class Config(_Section):
    root: Path = Path(".")
    paths: PathsConfig = PathsConfig()
    persona: PersonaConfig = PersonaConfig()
    models: ModelsConfig = ModelsConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    qq: QQConfig = QQConfig()
    vision: VisionConfig = VisionConfig()
    stickers: StickerConfig = StickerConfig()
    skills: SkillsConfig = SkillsConfig()
    sleep: SleepConfig = SleepConfig()
    life: LifeConfig = LifeConfig()
    trpg: TrpgConfig = TrpgConfig()

    @property
    def data_dir(self) -> Path:
        data_dir = self.paths.data_dir
        return data_dir if data_dir.is_absolute() else self.root / data_dir

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"


def load_config(root: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """从项目根目录加载配置。`environ` 默认取进程环境变量，测试时可传入替代值。"""
    root = (root or Path.cwd()).resolve()
    merged: dict[str, Any] = {}

    for name in (CONFIG_FILE, LOCAL_CONFIG_FILE):
        path = root / name
        if path.is_file():
            with path.open("rb") as f:
                data = tomllib.load(f)
            _reject_secrets(data, path)
            _deep_merge(merged, data)

    env: dict[str, str | None] = dict(dotenv_values(root / ENV_FILE))
    env.update(os.environ if environ is None else environ)
    for var, path in ENV_MAP.items():
        value = env.get(var)
        if value:  # 空字符串视为未设置
            _set_path(merged, path, value)

    merged["root"] = root
    try:
        return Config.model_validate(merged)
    except ValueError as e:
        raise ConfigError(f"配置无效：{e}") from e


def _reject_secrets(data: dict[str, Any], path: Path) -> None:
    for secret in SECRET_PATHS:
        node: Any = data
        for key in secret:
            if not isinstance(node, dict) or key not in node:
                break
            node = node[key]
        else:
            env_var = next(var for var, p in ENV_MAP.items() if p == secret)
            raise ConfigError(
                f"{path.name} 里不能写密钥 {'.'.join(secret)}，请改用 .env 的 {env_var}"
            )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data.setdefault(key, {})
    data[path[-1]] = value
