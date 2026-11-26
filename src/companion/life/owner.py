"""主动找主人：只在主人私聊里主动开口，群聊从不主动。

- 早安、晚安：每天各一次，时间在设定钟点前后随机几分钟；刚聊过、或当天次数用完就跳过。
- 分享（自主活动的收获、想聊的话题）：受每天次数、最短间隔、安静时段限制；
  上一条主动消息主人还没回，就先不再找话题（早晚安不受这条限制）。

记录在当天的 `proactive` 里：发出去的、以及跳过的（跳过的不占次数，但当天不会再试）。
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from typing import Any

from companion.config import OwnerChatConfig
from companion.life.days import DayBook, day_key
from companion.memory.events import EventLog

GREETINGS = ("morning", "night")
GREETING_WINDOW = timedelta(minutes=90)  # 过了预定时间这么久还没发（比如服务没开），就不发了


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def in_quiet(now: datetime, start: str, end: str) -> bool:
    t = now.astimezone().time()
    a, b = _clock(start), _clock(end)
    return a <= t < b if a <= b else t >= a or t < b


class OwnerPolicy:
    def __init__(self, cfg: OwnerChatConfig, days: DayBook, log: EventLog) -> None:
        self.cfg = cfg
        self.days = days
        self.log = log

    # ---- 早安、晚安 ----

    def greeting_due(self, owner_id: str, channel: str, now: datetime) -> str | None:
        """该说早安 / 晚安了就返回 morning / night。早晚安固定发送，不受分享次数与未回复影响。"""
        if not (self.cfg.enabled and self.cfg.greetings):
            return None
        today = self.days.entries(now, "proactive")
        done = {e.get("kind") for e in today}
        for kind, clock in (("morning", self.cfg.morning), ("night", self.cfg.night)):
            if kind in done:
                continue
            target = self.greeting_time(now, kind, clock)
            if not target <= now <= target + GREETING_WINDOW:
                continue
            if self._chatted(channel, now):
                self.record(now, kind, "", skipped=True)
                return None
            return kind
        return None

    def greeting_time(self, now: datetime, kind: str, clock: str) -> datetime:
        """这一天的早安 / 晚安时间：设定钟点前后随机 jitter 分钟（同一天固定）。"""
        d = day_key(now, self.days.start_hour)
        c = _clock(clock)
        target = datetime(d.year, d.month, d.day, c.hour, c.minute).astimezone()
        if c.hour < self.days.start_hour:  # 比如 00:30 的晚安：属于这一天的深夜
            target += timedelta(days=1)
        jitter = random.Random(f"{d}:{kind}").uniform(-1, 1) * self.cfg.jitter_minutes
        return target + timedelta(minutes=jitter)

    # ---- 分享 ----

    def can_share(self, owner_id: str, channel: str, now: datetime) -> tuple[bool, str]:
        cfg = self.cfg
        if not cfg.enabled:
            return False, "主动找主人已关闭"
        if in_quiet(now, cfg.quiet_start, cfg.quiet_end):
            return False, "安静时段"
        if len(_sent_shares(self.days.entries(now, "proactive"))) >= cfg.max_per_day:
            return False, "今天主动找的次数用完了"
        recent = _sent_shares(self.days.entries(now, "proactive", days=2))
        if recent:
            last = recent[-1]
            ts = datetime.fromisoformat(last["ts"])
            if now - ts < timedelta(minutes=cfg.min_gap_minutes):
                return False, "离上次主动找太近"
            if not self._answered(channel, owner_id, ts):
                return False, "上一条主人还没回"
        if self._chatted(channel, now):
            return False, "刚聊过"
        return True, ""

    def record(self, now: datetime, kind: str, text: str, *, skipped: bool = False) -> None:
        entry: dict[str, Any] = {"ts": now.isoformat(), "kind": kind, "text": text}
        if skipped:
            entry["skipped"] = True
        self.days.append(now, "proactive", entry)

    # ---- 聊天记录 ----

    def _chatted(self, channel: str, now: datetime) -> bool:
        since = now - timedelta(minutes=self.cfg.skip_if_chatted_minutes)
        return any(
            e.channel == channel and e.kind == "message" and since <= e.ts <= now
            for e in self.log.iter_events(since=since)
        )

    def _answered(self, channel: str, owner_id: str, after: datetime) -> bool:
        return any(
            e.channel == channel and e.kind == "message" and e.actor.person_id == owner_id
            for e in self.log.iter_events(since=after)
            if e.ts > after
        )


def _sent(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if not e.get("skipped") and e.get("ts")]


def _sent_shares(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """非早晚安的主动分享/找主人消息。"""
    return [e for e in _sent(entries) if e.get("kind") not in GREETINGS]
