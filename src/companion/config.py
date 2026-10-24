"""配置加载。

优先级从低到高：代码默认值 < config.toml < config.local.toml < .env < 进程环境变量。
密钥（API key）只能来自 .env 或环境变量；写进 toml 文件会直接报错，避免误提交。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

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
    model: str | None = None  # None 表示用 Codex 的默认模型；可选值见 app-server 的 model/list
    effort: str = "low"  # 聊天要快，推理强度调低
    switch_at_used_percent: float = 99.0  # 任一额度窗口用到这个百分比就切到兜底 API
    timeout_seconds: float = 120.0


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

    @field_validator("owner_accounts", "groups", "aliases", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


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


class Config(_Section):
    root: Path = Path(".")
    paths: PathsConfig = PathsConfig()
    persona: PersonaConfig = PersonaConfig()
    models: ModelsConfig = ModelsConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    qq: QQConfig = QQConfig()
    vision: VisionConfig = VisionConfig()
    stickers: StickerConfig = StickerConfig()
    sleep: SleepConfig = SleepConfig()

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
