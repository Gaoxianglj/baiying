"""定时提醒：【提醒：时间｜事情】记下，到点后她用自己的话主动私聊提醒对方。

- 只限私聊，提醒只发回设它的那个聊天；存在 data/reminders.json，重启不丢。
- 时间由程序解析并在结果里写明，她再告诉对方，对方能当场发现听错了的时间。
- 服务没开、或 NapCat 断线时错过的提醒，恢复后补发，并让她说明迟到了。
- 她知道这个人还有哪些提醒没到（技能说明里列出），可以按编号取消。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from companion.config import RemindersSkillConfig
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

log = logging.getLogger(__name__)

WEEKDAYS = "一二三四五六日"
EVERY_DAY = list(range(7))
WORKDAYS = list(range(5))

_CLOCK = r"(\d{1,2})[:：](\d{2})"
_RELATIVE = re.compile(r"^(?:(\d+)\s*天)?\s*(?:(\d+)\s*个?小时)?\s*(?:(\d+)\s*分钟?)?\s*后$")
_EVERY_DAY = re.compile(rf"^每天\s*{_CLOCK}$")
_WORKDAY = re.compile(rf"^(?:每个?)?工作日\s*{_CLOCK}$")
_EVERY_WEEK = re.compile(rf"^每(?:周|星期)([一二三四五六日天])\s*{_CLOCK}$")
_FULL = re.compile(rf"^(\d{{4}})[-/.年](\d{{1,2}})[-/.月](\d{{1,2}})日?\s*{_CLOCK}(?::\d{{2}})?$")
_MONTH_DAY = re.compile(rf"^(\d{{1,2}})[-/.月](\d{{1,2}})日?\s*{_CLOCK}$")
_NEAR_DAY = re.compile(rf"^(今天|明天|后天)\s*{_CLOCK}$")
_CLOCK_ONLY = re.compile(rf"^{_CLOCK}$")


@dataclass
class Repeat:
    """重复的提醒：本地时间几点，星期几（0 = 周一）。"""

    clock: str  # HH:MM
    weekdays: list[int]

    def label(self) -> str:
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
            if d.weekday() in self.weekdays and when > after:
                return when
        raise ValueError(f"算不出下一次提醒：{self}")  # 周几非空时走不到这里


@dataclass
class Reminder:
    id: int
    channel: str  # private:<QQ 号>，只发回这里
    person_id: str
    what: str
    due: datetime
    repeat: Repeat | None = None

    def when(self) -> str:
        if self.repeat is not None:
            return self.repeat.label()
        local = self.due.astimezone()
        weekday = WEEKDAYS[local.weekday()]
        return f"{local.year}年{local.month}月{local.day}日（周{weekday}）{local:%H:%M}"


def _local(d: date, hour: int, minute: int) -> datetime:
    return datetime.combine(d, time(hour, minute)).astimezone()


def _clock(hour: str, minute: str) -> tuple[int, int]:
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        raise SkillError(f"时间不对：{hour}:{minute}")
    return h, m


def parse_when(text: str, now: datetime) -> tuple[datetime, Repeat | None]:
    """把“2026-11-02 18:30”“30分钟后”“每天 08:00”这类写法解析成（下次时间，重复规则）。"""
    s = " ".join(text.strip().split())
    today = now.astimezone().date()
    if m := _EVERY_DAY.match(s):
        return _repeating(EVERY_DAY, m.group(1), m.group(2), now)
    if m := _WORKDAY.match(s):
        return _repeating(WORKDAYS, m.group(1), m.group(2), now)
    if m := _EVERY_WEEK.match(s):
        day = WEEKDAYS.index("日" if m.group(1) == "天" else m.group(1))
        return _repeating([day], m.group(2), m.group(3), now)
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
            if due <= now:  # 今年这一天已经过了，就是明年
                due = _local(date(today.year + 1, int(m.group(1)), int(m.group(2))), h, mi)
        elif m := _NEAR_DAY.match(s):
            h, mi = _clock(m.group(2), m.group(3))
            offset = {"今天": 0, "明天": 1, "后天": 2}[m.group(1)]
            due = _local(today + timedelta(days=offset), h, mi)
        elif m := _CLOCK_ONLY.match(s):
            h, mi = _clock(m.group(1), m.group(2))
            due = _local(today, h, mi)
            if due <= now:  # 今天这个点已经过了，就是明天
                due = _local(today + timedelta(days=1), h, mi)
        else:
            raise SkillError(
                f"看不懂这个时间：{text}。请写成 2026-11-02 18:30、30分钟后、每天 08:00 这样"
            )
    except ValueError as e:  # 2 月 30 日这样不存在的日期
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


class ReminderBook:
    """所有没到点的提醒，存在一个 JSON 文件里（写入先写临时文件再替换）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._next_id = 1
        self._items: dict[int, Reminder] = {}
        self._stamp: int | None = None  # 上次读到的文件修改时间
        self._reload_if_changed()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            items = [_from_json(x) for x in raw["items"]]
            self._next_id = int(raw["next_id"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as e:
            # 读不懂也不能当成空的覆盖掉：留一份，让人还能找回来
            backup = self.path.with_suffix(".bad.json")
            log.error("提醒文件读不懂（%s），已改名为 %s", e, backup.name)
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
        self, channel: str, person_id: str, what: str, due: datetime, repeat: Repeat | None
    ) -> Reminder:
        with self._lock:
            reminder = Reminder(self._next_id, channel, person_id, what, due, repeat)
            self._next_id += 1
            self._items[reminder.id] = reminder
            self._save()
            return reminder

    def cancel(self, channel: str, reminder_id: int) -> Reminder | None:
        """只能取消同一个聊天里设的提醒。"""
        with self._lock:
            reminder = self._items.get(reminder_id)
            if reminder is None or reminder.channel != channel:
                return None
            del self._items[reminder_id]
            self._save()
            return reminder

    def _reload_if_changed(self) -> None:
        """控制台是另一个进程：文件被 QQ 那边改过就重新读，显示的才是最新的。"""
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return
        if stamp != self._stamp:
            self._stamp = stamp
            self._load()

    def pending(self, channel: str | None = None) -> list[Reminder]:
        with self._lock:
            self._reload_if_changed()
            items = [r for r in self._items.values() if channel in (None, r.channel)]
        return sorted(items, key=lambda r: r.due)

    def due(self, now: datetime) -> list[Reminder]:
        return [r for r in self.pending() if r.due <= now]

    def finish(self, reminder_id: int, now: datetime) -> None:
        """提醒发出去了：重复的排到下一次，一次性的删掉。错过的几次不补，只提醒一回。"""
        with self._lock:
            reminder = self._items.get(reminder_id)
            if reminder is None:
                return
            if reminder.repeat is None:
                del self._items[reminder_id]
            else:
                reminder.due = reminder.repeat.next_after(now)
            self._save()


def _to_json(r: Reminder) -> dict[str, object]:
    data = asdict(r)
    data["due"] = r.due.astimezone(UTC).isoformat()
    return data


def _from_json(data: dict[str, object]) -> Reminder:
    repeat = data.get("repeat")
    return Reminder(
        id=int(data["id"]),  # type: ignore[call-overload]
        channel=str(data["channel"]),
        person_id=str(data["person_id"]),
        what=str(data["what"]),
        due=datetime.fromisoformat(str(data["due"])),
        repeat=Repeat(**repeat) if isinstance(repeat, dict) else None,
    )


class Reminders(Skill):
    name = "reminders"
    label = "定时提醒"
    markers = {
        "提醒": "【提醒：时间｜要提醒的事】到时间后你会主动私聊提醒对方。时间按“现在是……”换算成"
        "2026-11-02 18:30 这样的具体时间，也可以写 30分钟后、每天 08:00、每周一 09:00、"
        "工作日 08:00。事情写清楚，到时候你只看得到这句话。对方没说清是哪天几点就先问，不要猜。"
        "这个标记单独写，前面不要说话，设好后再告诉对方",
        "取消提醒": "【取消提醒：编号】取消一条还没到的提醒",
    }

    def __init__(self, cfg: RemindersSkillConfig, book: ReminderBook) -> None:
        self.cfg = cfg
        self.book = book

    def enabled(self) -> bool:
        return self.cfg.enabled

    def allowed(self, ctx: SkillContext) -> bool:
        # 只有 QQ 身体会到点发消息；命令行和群聊都不能设
        if not (ctx.private and ctx.body == "qq" and ctx.channel.startswith("private:")):
            return False
        return ctx.owner or not self.cfg.owner_only

    def status(self) -> str:
        return f"还有 {len(self.book.pending())} 条提醒没到"

    def context(self, ctx: SkillContext) -> str:
        pending = self.book.pending(ctx.channel)
        if not pending:
            return ""
        lines = [f"- 编号 {r.id}：{r.when()}，{r.what}" for r in pending]
        return "你已经答应要提醒对方的事（别重复设）：\n" + "\n".join(lines)

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        if marker == "取消提醒":
            return self._cancel(arg, ctx)
        when, sep, what = arg.replace("|", "｜").partition("｜")
        what = " ".join(what.split())[:200]
        if not sep or not what:
            raise SkillError("要写成【提醒：时间｜要提醒的事】")
        now = ctx.now or datetime.now(UTC)
        due, repeat = parse_when(when, now)
        if len(self.book.pending(ctx.channel)) >= self.cfg.max_per_person:
            raise SkillError(f"已经有 {self.cfg.max_per_person} 条提醒了，先取消几条再设")
        reminder = self.book.add(ctx.channel, ctx.person_id, what, due, repeat)
        log.info("设了提醒 %d：%s，%s", reminder.id, reminder.when(), what)
        return SkillResult(
            f"设好了，编号 {reminder.id}：{reminder.when()}，提醒“{what}”。"
            "用你自己的话告诉对方记下了，把这个时间说清楚（听错了对方能马上发现）。"
        )

    def _cancel(self, arg: str, ctx: SkillContext) -> SkillResult:
        digits = re.search(r"\d+", arg)
        if digits is None:
            raise SkillError("要写清楚取消哪一条（编号）")
        reminder = self.book.cancel(ctx.channel, int(digits.group()))
        if reminder is None:
            raise SkillError(f"没有编号 {digits.group()} 这条提醒")
        return SkillResult(f"取消了：{reminder.when()}，“{reminder.what}”。告诉对方一声。")
