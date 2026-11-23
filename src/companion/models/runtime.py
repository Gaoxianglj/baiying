"""宿主提供的工具、状态回调与运行控制；不授予 Codex 额外本机权限。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from typing import Any


@dataclass
class ToolSet:
    specs: list[dict[str, Any]]
    call: Callable[[str, dict[str, Any], str], dict[str, Any]]
    # 原生工具已产生副作用时，让网关兜底能够拿到相同的实际结果。
    history: list[Any] = field(default_factory=list)


@dataclass
class RunControl:
    stopped: Event
    pending: Callable[[], list[dict[str, Any]]] = lambda: []
    receipt: Callable[[str, bool, str], None] = lambda *_: None
    compact: Callable[[], bool] = lambda: False
    deadline: float | None = None


Observer = Callable[[str, dict[str, Any]], None]
