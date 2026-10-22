"""事件入口：所有身体都通过这里上报“发生了什么”。

负责把平台账号解析成人（陌生账号自动建档），组装成事件写进日志。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from companion.memory.events import Actor, Event, EventKind, EventLog
from companion.memory.layout import MemoryLayout, load_manifest
from companion.memory.people import SELF_ID, PeopleRegistry


class Ingest:
    def __init__(self, layout: MemoryLayout) -> None:
        load_manifest(layout)  # 不是记忆文件夹就别往里写
        self.layout = layout
        self.log = EventLog(layout.events_dir)
        self.people = PeopleRegistry(layout)

    def record(
        self,
        *,
        body: str,
        channel: str,
        kind: EventKind,
        account: str,
        content: str,
        platform: str | None = None,
        display_name: str | None = None,
        ts: datetime | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Event:
        """记录别人（或环境）发生的事。`platform` 默认与 `body` 相同。"""
        platform = platform or body
        actor = Actor(
            platform=platform,
            platform_id=account,
            person_id=self.people.ensure(platform, account, display_name),
            display_name=display_name,
        )
        return self._append(body, channel, kind, actor, content, ts, meta)

    def record_self(
        self,
        *,
        body: str,
        channel: str,
        content: str,
        kind: EventKind = "message",
        ts: datetime | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Event:
        """记录伙伴自己说的话、做的事。"""
        actor = Actor(platform=SELF_ID, platform_id=SELF_ID, person_id=SELF_ID)
        return self._append(body, channel, kind, actor, content, ts, meta)

    def _append(
        self,
        body: str,
        channel: str,
        kind: EventKind,
        actor: Actor,
        content: str,
        ts: datetime | None,
        meta: dict[str, Any] | None,
    ) -> Event:
        fields: dict[str, Any] = {
            "body": body,
            "channel": channel,
            "kind": kind,
            "actor": actor,
            "content": content,
            "meta": meta or {},
        }
        if ts is not None:
            fields["ts"] = ts
        event = Event(**fields)
        self.log.append(event)
        return event
