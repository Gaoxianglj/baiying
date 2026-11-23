"""原始事件日志：每行一个 JSON 事件，按 UTC 月份分文件，只追加不修改。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

EVENT_SCHEMA_VERSION = 1

EventKind = Literal["message", "action", "observation", "system"]


class Actor(BaseModel):
    """事件的发出者。`person_id` 在身份绑定后才有（P0-04）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str  # 账号所属平台：qq / minecraft / cli；伙伴自己为 "self"
    platform_id: str  # 平台内的账号：QQ 号、MC 玩家名；伙伴自己为 "self"
    person_id: str | None = None  # 写入时解析出的人，可能日后被 merged_into 指向别人
    display_name: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = EVENT_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: uuid4().hex)
    ts: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    body: str  # 哪个身体：qq / minecraft / cli / system
    channel: str  # 哪个频道：private:<id> / group:<id> / world:<name> / cli
    kind: EventKind
    actor: Actor
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    def __init__(self, events_dir: Path) -> None:
        self._dir = events_dir
        self._lock = threading.Lock()

    def file_for(self, ts: datetime) -> Path:
        return self._dir / f"{ts.astimezone(UTC):%Y-%m}.jsonl"

    def append(self, event: Event) -> Path:
        path = self.file_for(event.ts)
        line = event.model_dump_json() + "\n"
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line)
                f.flush()
        return path

    def redact(self, event_ids: set[str]) -> list[Event]:
        """从日志里彻底删除这些事件（用于“忘掉这件事”），返回被删除的事件。

        这是唯一会改写日志的操作：逐个月份文件重写，先写临时文件再原子替换。
        """
        removed: list[Event] = []
        with self._lock:
            for path in sorted(self._dir.glob("*.jsonl")):
                lines = path.read_text("utf-8").splitlines(keepends=True)
                kept = []
                for line in lines:
                    try:
                        event = Event.model_validate_json(line)
                    except ValidationError:
                        event = None
                    if event is not None and event.id in event_ids:
                        removed.append(event)
                    else:
                        kept.append(line)
                if len(kept) != len(lines):
                    tmp = path.with_suffix(".jsonl.tmp")
                    tmp.write_text("".join(kept), encoding="utf-8", newline="")
                    tmp.replace(path)
        return removed

    def iter_events(self, since: datetime | None = None,
                    *, include_private: bool = False) -> Iterator[Event]:
        """按文件（月份）顺序读出事件。损坏的行（如写到一半断电）记录警告后跳过。"""
        for path in sorted(self._dir.glob("*.jsonl")):
            if since is not None and path.stem < f"{since.astimezone(UTC):%Y-%m}":
                continue
            with path.open(encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        event = Event.model_validate_json(line)
                    except ValidationError:
                        log.warning("跳过损坏的事件行 %s:%d", path.name, lineno)
                        continue
                    from companion.trpg.policy import public_event
                    policy = self._dir.parent.parent / "run/trpg-protected.json"
                    if not include_private and not public_event(event, policy):
                        continue
                    if since is None or event.ts >= since:
                        yield event
