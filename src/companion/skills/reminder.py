"""约定与提醒：自然语言建立、修改与解除约定，覆盖准点提醒、周期行动履约与持续相处约定。

- 私聊默认可用；群聊默认限主人或熟络度达到“熟络”及以上的群友建立，只作用于当前聊天。
- 存在 data/reminders.json（兼容旧提醒格式），重启不丢，控制台跨进程实时同步。
- 支持三类形态：
  1. 准点/周期提醒（remind）：到点用自己的话提醒对方；
  2. 行动约定（action）：到点（支持“每天”“每天 下午”等弹性时段）调用受控技能干活（如画花存入花册），记录履约进度防止重样；
  3. 持续相处约定（ongoing）：无定时触发，常驻在对话上下文中遵守（如“聊虫子不发虫图”），对方说解除即取消。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from companion.config import RemindersSkillConfig
from companion.memory.people import familiarity_rank
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

if TYPE_CHECKING:
    from companion.memory.people import PeopleRegistry

log = logging.getLogger(__name__)

PromiseKind = Literal["remind", "action", "ongoing"]
NotifyMode = Literal["always", "quiet"]

WEEKDAYS = "一二三四五六日"
EVERY_DAY = list(range(7))
WORKDAYS = list(range(5))

SLOT_CLOCKS: dict[str, tuple[int, int]] = {
    "早上": (9, 0),
    "上午": (10, 30),
    "中午": (12, 30),
    "下午": (15, 30),
    "傍晚": (18, 30),
    "晚上": (20, 30),
    "白天": (15, 0),
}
_SLOT_PAT = "|".join(SLOT_CLOCKS)
ONGOING_WORDS = {"持续", "长期", "一直", "平时", "常驻", "每次", "随时"}

_CLOCK = r"(\d{1,2})[:：](\d{2})"
_RELATIVE = re.compile(r"^(?:(\d+)\s*天)?\s*(?:(\d+)\s*个?小时)?\s*(?:(\d+)\s*分钟?)?\s*后$")
_EVERY_DAY = re.compile(rf"^每[天日]\s*{_CLOCK}$")
_EVERY_DAY_FLEX = re.compile(rf"^每[天日](?:\s*({_SLOT_PAT}))?$")
_WORKDAY = re.compile(rf"^(?:每个?)?工作日\s*{_CLOCK}$")
_WORKDAY_FLEX = re.compile(rf"^(?:每个?)?工作日(?:\s*({_SLOT_PAT}))?$")
_EVERY_WEEK = re.compile(rf"^每(?:周|星期)([一二三四五六日天])\s*{_CLOCK}$")
_EVERY_WEEK_FLEX = re.compile(rf"^每(?:周|星期)([一二三四五六日天])(?:\s*({_SLOT_PAT}))?$")
_FULL = re.compile(rf"^(\d{{4}})[-/.年](\d{{1,2}})[-/.月](\d{{1,2}})日?\s*{_CLOCK}(?::\d{{2}})?$")
_MONTH_DAY = re.compile(rf"^(\d{{1,2}})[-/.月](\d{{1,2}})日?\s*{_CLOCK}$")
_NEAR_DAY = re.compile(rf"^(今天|明天|后天)\s*{_CLOCK}$")
_NEAR_DAY_FLEX = re.compile(rf"^(?:(今天|明天|后天)\s*({_SLOT_PAT})|(明天|后天))$")
_CLOCK_ONLY = re.compile(rf"^{_CLOCK}$")

_ACTION_HINT = re.compile(
    r"画|花册|图册|存进|存到|存入|放进|收进|写进|写入|写一首|写一篇|写段|整理进|小册子|文件夹|做图册|搜集|查一查"
)
_QUIET_HINT = re.compile(r"不用(?:每次|天天)?(?:发|喊|叫|通知|打扰|说)|静默|悄悄(?:存|攒|画|记)")

_UNSET = object()


def _flex_jitter_minutes(d: date, clock: str) -> int:
    """弹性时段每天带一点稳定随机偏移（-15～+15 分钟），不卡整点秒针。"""
    raw = zlib.crc32(f"{d.isoformat()}|{clock}".encode()) % 31
    return int(raw) - 15


@dataclass
class Repeat:
    """重复规则：本地时间几点，星期几（0 = 周一）；flexible 时为日常弹性时段。"""

    clock: str  # HH:MM
    weekdays: list[int]
    flexible: bool = False
    slot_label: str = ""

    def label(self) -> str:
        if self.slot_label:
            return self.slot_label
        if self.weekdays == EVERY_DAY:
            return f"每天 {self.clock}"
        if self.weekdays == WORKDAYS:
            return f"工作日 {self.clock}"
        return f"每周{WEEKDAYS[self.weekdays[0]]} {self.clock}"

    def next_after(self, after: datetime) -> datetime:
        hour, minute = map(int, self.clock.split(":"))
        day = after.astimezone().date()
        for offset in range(8):
            d = day + timedelta(days=offset)
            when = _local(d, hour, minute)
            if self.flexible:
                when = when + timedelta(minutes=_flex_jitter_minutes(d, self.clock))
            if d.weekday() in self.weekdays and when > after:
                return when
        raise ValueError(f"算不出下一次时间：{self}")


@dataclass
class Promise:
    id: int
    channel: str  # private:<QQ 号> 或 group:<群号>
    person_id: str
    what: str
    due: datetime | None = None
    repeat: Repeat | None = None
    kind: PromiseKind = "remind"
    notify: NotifyMode = "always"
    history: list[str] = field(default_factory=list)
    created_at: str = ""

    def when(self) -> str:
        if self.kind == "ongoing" or self.due is None:
            return "持续遵守"
        if self.repeat is not None:
            return self.repeat.label()
        local = self.due.astimezone()
        weekday = WEEKDAYS[local.weekday()]
        return f"{local.year}年{local.month}月{local.day}日（周{weekday}）{local:%H:%M}"


# 兼容旧命名导入
Reminder = Promise


def _local(d: date, hour: int, minute: int) -> datetime:
    return datetime.combine(d, time(hour, minute)).astimezone()


def _clock(hour: str, minute: str) -> tuple[int, int]:
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        raise SkillError(f"时间不对：{hour}:{minute}")
    return h, m


def parse_schedule(text: str, now: datetime) -> tuple[datetime | None, Repeat | None, bool]:
    """解析触发时间：返回 (下次时间, 重复规则, 是否为持续约定)。"""
    s = " ".join(text.strip().split())
    if s in ONGOING_WORDS:
        return None, None, True
    due, repeat = parse_when(s, now)
    return due, repeat, False


def parse_when(text: str, now: datetime) -> tuple[datetime, Repeat | None]:
    """把准点写法（每天 08:00、30分钟后）或弹性时段（每天、每天 下午、每周五 晚上）解析成时间。"""
    s = " ".join(text.strip().split())
    start_tomorrow = False
    for prefix in ("从明天起", "明天起", "从明天开始"):
        if s.startswith(prefix):
            s = s.removeprefix(prefix).strip()
            start_tomorrow = True
            break
    for prefix in ("从今天起", "今天起", "从今天开始", "以后"):
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s.removeprefix(prefix).strip()
            break

    base_now = (
        _local(now.astimezone().date() + timedelta(days=1), 0, 1) if start_tomorrow else now
    )
    today = now.astimezone().date()

    if m := _EVERY_DAY.match(s):
        return _repeating(EVERY_DAY, m.group(1), m.group(2), base_now)
    if m := _EVERY_DAY_FLEX.match(s):
        slot = m.group(1)
        h, mi = SLOT_CLOCKS[slot] if slot else (15, 0)
        label = f"每天 {slot}" if slot else "每天"
        return _repeating_flex(EVERY_DAY, h, mi, label, base_now)
    if m := _WORKDAY.match(s):
        return _repeating(WORKDAYS, m.group(1), m.group(2), base_now)
    if m := _WORKDAY_FLEX.match(s):
        slot = m.group(1)
        h, mi = SLOT_CLOCKS[slot] if slot else (15, 0)
        label = f"工作日 {slot}" if slot else "工作日"
        return _repeating_flex(WORKDAYS, h, mi, label, base_now)
    if m := _EVERY_WEEK.match(s):
        day = WEEKDAYS.index("日" if m.group(1) == "天" else m.group(1))
        return _repeating([day], m.group(2), m.group(3), base_now)
    if m := _EVERY_WEEK_FLEX.match(s):
        w = "日" if m.group(1) == "天" else m.group(1)
        day = WEEKDAYS.index(w)
        slot = m.group(2)
        h, mi = SLOT_CLOCKS[slot] if slot else (15, 0)
        label = f"每周{w} {slot}" if slot else f"每周{w}"
        return _repeating_flex([day], h, mi, label, base_now)
    if (m := _RELATIVE.match(s)) and any(m.groups()):
        days, hours, minutes = (int(x or 0) for x in m.groups())
        delta = timedelta(days=days, hours=hours, minutes=minutes)
        if delta <= timedelta(0):
            raise SkillError("时间要在现在之后")
        return now + delta, None
    try:
        if m := _FULL.match(s):
            h, mi = _clock(m.group(4), m.group(5))
            due = _local(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), h, mi)
        elif m := _MONTH_DAY.match(s):
            h, mi = _clock(m.group(3), m.group(4))
            due = _local(date(today.year, int(m.group(1)), int(m.group(2))), h, mi)
            if due <= now:
                due = _local(date(today.year + 1, int(m.group(1)), int(m.group(2))), h, mi)
        elif m := _NEAR_DAY.match(s):
            h, mi = _clock(m.group(2), m.group(3))
            offset = {"今天": 0, "明天": 1, "后天": 2}[m.group(1)]
            due = _local(today + timedelta(days=offset), h, mi)
        elif m := _NEAR_DAY_FLEX.match(s):
            near = m.group(1) or m.group(3)
            slot = m.group(2)
            h, mi = SLOT_CLOCKS[slot] if slot else (15, 0)
            offset = {"今天": 0, "明天": 1, "后天": 2}[near]
            due = _local(today + timedelta(days=offset), h, mi)
            if offset == 0 and due <= now:
                due = now + timedelta(minutes=20)
        elif m := _CLOCK_ONLY.match(s):
            h, mi = _clock(m.group(1), m.group(2))
            due = _local(today, h, mi)
            if due <= now:
                due = _local(today + timedelta(days=1), h, mi)
        else:
            raise SkillError(
                f"看不懂这个时间：{text}。可写 2026-11-02 18:30、30分钟后、每天 08:00、每天、每天 下午 或 持续"
            )
    except ValueError as e:
        raise SkillError(f"没有这个日期：{text}") from e
    if due <= now:
        raise SkillError(f"这个时间已经过去了：{text}")
    return due, None


def _repeating(
    weekdays: list[int], hour: str, minute: str, now: datetime
) -> tuple[datetime, Repeat]:
    h, m = _clock(hour, minute)
    repeat = Repeat(f"{h:02d}:{m:02d}", weekdays)
    return repeat.next_after(now), repeat


def _repeating_flex(
    weekdays: list[int], hour: int, minute: int, label: str, now: datetime
) -> tuple[datetime, Repeat]:
    repeat = Repeat(f"{hour:02d}:{minute:02d}", weekdays, flexible=True, slot_label=label)
    return repeat.next_after(now), repeat


def infer_promise_mode(
    what: str, mode_text: str = "", *, ongoing: bool = False, legacy_remind: bool = False
) -> tuple[PromiseKind, NotifyMode]:
    if ongoing:
        return "ongoing", "quiet"
    mode = mode_text.strip()
    quiet = bool("静默" in mode or "不发" in mode or _QUIET_HINT.search(what))
    notify: NotifyMode = "quiet" if quiet else "always"
    if "提醒" in mode and "行动" not in mode:
        return "remind", notify
    if "行动" in mode or "做事" in mode:
        return "action", notify
    if legacy_remind:
        return "remind", notify
    if _ACTION_HINT.search(what):
        return "action", notify
    return "remind", notify


class PromiseBook:
    """所有生效中的约定与提醒，存在一个 JSON 文件里（写入先写临时文件再替换）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._next_id = 1
        self._items: dict[int, Promise] = {}
        self._stamp: int | None = None
        self._reload_if_changed()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            items = [_from_json(x) for x in raw["items"]]
            self._next_id = int(raw["next_id"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as e:
            backup = self.path.with_suffix(".bad.json")
            log.error("约定文件读不懂（%s），已改名为 %s", e, backup.name)
            self.path.replace(backup)
            return
        self._items = {r.id: r for r in items}

    def _save(self) -> None:
        data = {
            "next_id": self._next_id,
            "items": [_to_json(r) for r in self._items.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        tmp.replace(self.path)
        self._stamp = self.path.stat().st_mtime_ns

    def add(
        self,
        channel: str,
        person_id: str,
        what: str,
        due: datetime | None,
        repeat: Repeat | None,
        *,
        kind: PromiseKind = "remind",
        notify: NotifyMode = "always",
        now: datetime | None = None,
    ) -> Promise:
        with self._lock:
            self._reload_if_changed()
            created = (now or datetime.now(UTC)).isoformat(timespec="seconds")
            promise = Promise(
                self._next_id,
                channel,
                person_id,
                what,
                None if kind == "ongoing" else due,
                None if kind == "ongoing" else repeat,
                kind=kind,
                notify=notify,
                created_at=created,
            )
            self._next_id += 1
            self._items[promise.id] = promise
            self._save()
            return promise

    def update(
        self,
        channel: str,
        promise_id: int,
        *,
        what: str | None = None,
        due: Any = _UNSET,
        repeat: Any = _UNSET,
        kind: PromiseKind | None = None,
        notify: NotifyMode | None = None,
        owner_override: bool = False,
    ) -> Promise | None:
        with self._lock:
            self._reload_if_changed()
            promise = self._items.get(promise_id)
            if promise is None:
                return None
            if not owner_override and promise.channel != channel:
                return None
            if what is not None:
                promise.what = what
            if kind is not None:
                promise.kind = kind
            if notify is not None:
                promise.notify = notify
            if promise.kind == "ongoing":
                promise.due = None
                promise.repeat = None
            else:
                if due is not _UNSET:
                    promise.due = due
                if repeat is not _UNSET:
                    promise.repeat = repeat
            self._save()
            return promise

    def cancel(
        self, channel: str, promise_id: int, *, owner_override: bool = False
    ) -> Promise | None:
        """只能解除同一个聊天里的约定（主人控制台或全局维护可显式 override）。"""
        with self._lock:
            self._reload_if_changed()
            promise = self._items.get(promise_id)
            if promise is None:
                return None
            if not owner_override and promise.channel != channel:
                return None
            del self._items[promise_id]
            self._save()
            return promise

    def _reload_if_changed(self) -> None:
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return
        if stamp != self._stamp:
            self._stamp = stamp
            self._load()

    def pending(self, channel: str | None = None) -> list[Promise]:
        with self._lock:
            self._reload_if_changed()
            items = [r for r in self._items.values() if channel in (None, r.channel)]
        far = datetime.max.replace(tzinfo=UTC)
        return sorted(items, key=lambda r: (r.due is None, r.due or far, r.id))

    def for_context(self, channel: str, person_id: str, *, private: bool) -> list[Promise]:
        """当前对话应看到的约定：本聊天的全部约定 + 私聊时该人物跨频道的持续约定。"""
        with self._lock:
            self._reload_if_changed()
            items = [
                r
                for r in self._items.values()
                if r.channel == channel
                or (private and r.person_id == person_id and r.kind == "ongoing")
            ]
        far = datetime.max.replace(tzinfo=UTC)
        return sorted(items, key=lambda r: (r.due is None, r.due or far, r.id))

    def due(self, now: datetime) -> list[Promise]:
        return [
            r
            for r in self.pending()
            if r.due is not None and r.kind != "ongoing" and r.due <= now
        ]

    def postpone(self, promise_id: int, until: datetime) -> None:
        """因额度暂满等原因推迟到稍后再试，不消耗本次周期。"""
        with self._lock:
            self._reload_if_changed()
            promise = self._items.get(promise_id)
            if promise is not None and promise.due is not None:
                promise.due = until
                self._save()

    def finish(self, promise_id: int, now: datetime, *, summary: str = "") -> None:
        """履约完成：记下本次履约摘要；重复的排到下一次，一次性的删掉。"""
        with self._lock:
            self._reload_if_changed()
            promise = self._items.get(promise_id)
            if promise is None:
                return
            if summary.strip():
                stamp = f"{now.astimezone():%Y-%m-%d %H:%M}"
                clean = " ".join(summary.split())[:160]
                promise.history = [*promise.history[-19:], f"{stamp}：{clean}"]
            if promise.kind == "ongoing":
                self._save()
                return
            if promise.repeat is None:
                del self._items[promise_id]
            else:
                promise.due = promise.repeat.next_after(now)
            self._save()


ReminderBook = PromiseBook


def _to_json(r: Promise) -> dict[str, object]:
    data = asdict(r)
    data["due"] = r.due.astimezone(UTC).isoformat() if r.due is not None else None
    return data


def _from_json(data: dict[str, object]) -> Promise:
    repeat_raw = data.get("repeat")
    repeat = None
    if isinstance(repeat_raw, dict):
        repeat = Repeat(
            clock=str(repeat_raw["clock"]),
            weekdays=[int(x) for x in repeat_raw["weekdays"]],
            flexible=bool(repeat_raw.get("flexible", False)),
            slot_label=str(repeat_raw.get("slot_label", "")),
        )
    due_raw = data.get("due")
    due = datetime.fromisoformat(str(due_raw)) if due_raw else None
    raw_kind = str(data.get("kind") or ("ongoing" if due is None else "remind"))
    kind: PromiseKind = (
        raw_kind if raw_kind in ("remind", "action", "ongoing") else "remind"  # type: ignore[assignment]
    )
    raw_notify = str(data.get("notify") or "always")
    notify: NotifyMode = "quiet" if raw_notify == "quiet" else "always"
    history = [str(x) for x in (data.get("history") or []) if str(x).strip()]
    return Promise(
        id=int(data["id"]),  # type: ignore[call-overload]
        channel=str(data["channel"]),
        person_id=str(data["person_id"]),
        what=str(data["what"]),
        due=due,
        repeat=repeat,
        kind=kind,
        notify=notify,
        history=history[-20:],
        created_at=str(data.get("created_at") or ""),
    )


class Promises(Skill):
    name = "reminders"
    label = "约定与提醒"
    markers: ClassVar[dict[str, str]] = {
        "约定": (
            "【约定：时间或持续｜约定内容｜方式（可选）】当你在自然聊天中与对方达成约定、承诺定期做某件事"
            "（如每天画一幅不同的花放进花册里）或答应到点提醒时调用，无需对方说指令词。"
            "第 1 段写时间：准点时间（2026-11-02 18:30、30分钟后、每天 08:00、每周一 09:00、工作日 08:00）、"
            "不卡几点的日常周期（每天、每天 下午、每周五 晚上、明天 上午），或平时一直遵守的相处约定（写 持续）。"
            "第 2 段写清完整要求与保存目录（如 每天画一朵不重样的花存入 花册/ 并发给大家看），履约时你靠这句话和过往记录行动。"
            "第 3 段可选：需要调画画/读写文件/做图册/搜索等技能去完成的写 行动（只口头提醒可省）；"
            "若约定悄悄存好不用每次发消息打扰，写 行动+静默。单独写，先登记再回复对方"
        ),
        "修改约定": (
            "【修改约定：编号｜新时间或持续｜新内容｜方式（可选）】对方在聊天中提出调整已有约定的频率、时间或做法时调用"
        ),
        "解除约定": (
            "【解除约定：编号】对方在聊天中自然表示某个约定或提醒不用继续、先停下或取消时调用"
        ),
    }
    aliases: ClassVar[tuple[str, ...]] = ("提醒", "取消提醒")

    def __init__(
        self,
        cfg: RemindersSkillConfig,
        book: PromiseBook,
        people: PeopleRegistry | None = None,
    ) -> None:
        self.cfg = cfg
        self.book = book
        self.people = people

    def enabled(self) -> bool:
        return self.cfg.enabled

    def allowed(self, ctx: SkillContext) -> bool:
        if ctx.body != "qq":
            return False
        if ctx.private and ctx.channel.startswith("private:"):
            return ctx.owner or not self.cfg.owner_only
        if not ctx.private and ctx.channel.startswith("group:"):
            if not self.cfg.in_groups:
                return False
            if ctx.owner:
                return True
            if self.people is None:
                return False
            person = self.people.get(ctx.person_id)
            if person is None:
                return False
            return familiarity_rank(person.familiarity) >= familiarity_rank(
                self.cfg.group_min_familiarity
            )
        return False

    def status(self) -> str:
        items = self.book.pending()
        ongoing = sum(1 for x in items if x.kind == "ongoing")
        timed = len(items) - ongoing
        if ongoing:
            return f"{timed} 条定时约定/提醒，{ongoing} 条持续约定"
        return f"还有 {timed} 条约定/提醒生效中"

    def context(self, ctx: SkillContext) -> str:
        items = self.book.for_context(ctx.channel, ctx.person_id, private=ctx.private)
        if not items:
            return ""
        lines = []
        for r in items:
            tag = (
                "持续遵守"
                if r.kind == "ongoing"
                else ("行动约定" + ("·静默" if r.notify == "quiet" else "") if r.kind == "action" else "提醒")
            )
            recent = f"（最近履约：{r.history[-1]}）" if r.history else ""
            lines.append(f"- 编号 {r.id} [{tag}｜{r.when()}]：{r.what}{recent}")
        return (
            "当前生效的约定与提醒（持续遵守的要在对话里自觉做到；别重复登记；对方说不用继续时用【解除约定：编号】）：\n"
            + "\n".join(lines)
        )

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        if marker in ("解除约定", "取消提醒"):
            return self._cancel(arg, ctx)
        if marker == "修改约定":
            return self._update(arg, ctx)
        return self._create(arg, ctx, legacy_remind=(marker == "提醒"))

    def _create(self, arg: str, ctx: SkillContext, *, legacy_remind: bool) -> SkillResult:
        parts = [p.strip() for p in arg.replace("|", "｜").split("｜")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            if legacy_remind:
                raise SkillError("要写成【提醒：时间｜要提醒的事】")
            raise SkillError("要写成【约定：时间或持续｜约定内容】")
        when_text, what = parts[0], " ".join(parts[1].split())[:300]
        mode_text = parts[2] if len(parts) >= 3 else ""
        now = ctx.now or datetime.now(UTC)
        due, repeat, ongoing = parse_schedule(when_text, now)
        if len(self.book.pending(ctx.channel)) >= self.cfg.max_per_person:
            raise SkillError(
                f"已经有 {self.cfg.max_per_person} 条约定或提醒了，先取消或解除几条再设"
            )
        kind, notify = infer_promise_mode(
            what, mode_text, ongoing=ongoing, legacy_remind=legacy_remind
        )
        promise = self.book.add(
            ctx.channel, ctx.person_id, what, due, repeat, kind=kind, notify=notify, now=now
        )
        log.info("立下约定 %d（%s）：%s，%s", promise.id, promise.kind, promise.when(), what)
        if promise.kind == "ongoing":
            return SkillResult(
                f"已记入约定本，编号 {promise.id}（持续遵守）：“{what}”。"
                "用你自己的话自然回应对方即可，不用报编号。"
            )
        mode_desc = "到时间会自己动手完成" if promise.kind == "action" else "到时间会提醒对方"
        if promise.notify == "quiet":
            mode_desc += "（静默保存，不发消息打扰）"
        return SkillResult(
            f"已记入约定本，编号 {promise.id}：{promise.when()}，{mode_desc}——“{what}”。"
            "用你自己的话自然告诉对方记下了，时间或周期顺口说清楚（听错了对方能马上发现），不用念编号。"
        )

    def _update(self, arg: str, ctx: SkillContext) -> SkillResult:
        parts = [p.strip() for p in arg.replace("|", "｜").split("｜")]
        if len(parts) < 3:
            raise SkillError("要写成【修改约定：编号｜新时间或持续｜新内容】")
        digits = re.search(r"\d+", parts[0])
        if digits is None:
            raise SkillError("要写清楚修改哪一条（编号）")
        pid = int(digits.group())
        when_text = parts[1]
        what = " ".join(parts[2].split())[:300]
        mode_text = parts[3] if len(parts) >= 4 else ""
        if not what:
            raise SkillError("修改后的约定内容不能为空")
        now = ctx.now or datetime.now(UTC)
        due, repeat, ongoing = parse_schedule(when_text, now)
        kind, notify = infer_promise_mode(what, mode_text, ongoing=ongoing)
        updated = self.book.update(
            ctx.channel,
            pid,
            what=what,
            due=due,
            repeat=repeat,
            kind=kind,
            notify=notify,
            owner_override=(ctx.owner and ctx.private),
        )
        if updated is None:
            raise SkillError(f"没有编号 {pid} 这条约定")
        return SkillResult(
            f"已更新约定 {updated.id}：{updated.when()}，“{updated.what}”。自然告诉对方一声就好。"
        )

    def _cancel(self, arg: str, ctx: SkillContext) -> SkillResult:
        digits = re.search(r"\d+", arg)
        if digits is None:
            raise SkillError("要写清楚解除哪一条（编号）")
        promise = self.book.cancel(
            ctx.channel, int(digits.group()), owner_override=(ctx.owner and ctx.private)
        )
        if promise is None:
            raise SkillError(f"没有编号 {digits.group()} 这条约定或提醒")
        return SkillResult(f"已解除：{promise.when()}，“{promise.what}”。自然告诉对方一声。")


Reminders = Promises
