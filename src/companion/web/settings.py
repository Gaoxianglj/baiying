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
