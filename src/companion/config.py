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
from pydantic import BaseModel, ConfigDict, SecretStr

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
}

# 只允许出现在环境变量里的配置路径
SECRET_PATHS: set[tuple[str, ...]] = {("models", "fallback", "api_key")}


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
    bot_account: str | None = None


class Config(_Section):
    root: Path = Path(".")
    paths: PathsConfig = PathsConfig()
    persona: PersonaConfig = PersonaConfig()
    models: ModelsConfig = ModelsConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    qq: QQConfig = QQConfig()

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
