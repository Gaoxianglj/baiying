"""今日状态（AI 版日常）：每天一份 `self/days/YYYY-MM-DD.json`。

一天从睡眠的钟点（默认凌晨 4 点）开始。文件里有：
- plan：当天的安排（心情基调、在读什么、各时段做什么、一两件小事），每天生成一次，之后不再改；
- moods：心情变化（时间、词、原因、在哪个频道）；
- activities：她自己找的事（查资料、写东西、画画……）。

回复时的“你的今天” = 已经过去的时段的安排 + 今天真实发生的事 + 自己做过的事。
安排只精确到时段：同一时段里这一段提示词不变，不影响前缀缓存。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage, ModelError

log = logging.getLogger(__name__)

# 时段：名字、开始钟点、结束钟点（本地时间）；顺序就是一天里的先后
SLOTS: list[tuple[str, int, int]] = [
    ("凌晨", 4, 7),
    ("早上", 7, 9),
    ("上午", 9, 12),
    ("中午", 12, 14),
    ("下午", 14, 18),
    ("傍晚", 18, 20),
    ("晚上", 20, 24),
    ("深夜", 0, 4),
]
SLOT_NAMES = [s[0] for s in SLOTS]


def slot_of(dt: datetime) -> str:
    hour = dt.astimezone().hour
    return next(name for name, start, end in SLOTS if start <= hour < end)


def day_key(now: datetime, start_hour: int) -> date:
    """now 属于哪一天：不到 start_hour 点算前一天。"""
    return (now.astimezone() - timedelta(hours=start_hour)).date()


def day_start(now: datetime, start_hour: int) -> datetime:
    d = day_key(now, start_hour)
    return datetime(d.year, d.month, d.day, start_hour).astimezone()


@dataclass
class DayPlan:
    mood: str  # 今天的心情基调
    reading: str  # 今天在读或在看的东西
    slots: dict[str, str]  # 时段 → 在做什么
    small_things: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DayPlan:
        return cls(
            mood=str(data.get("mood", "")),
            reading=str(data.get("reading", "")),
            slots={k: str(v) for k, v in (data.get("slots") or {}).items()},
            small_things=[str(x) for x in data.get("small_things") or []],
        )


class DayBook:
    """按天存放的生活记录。所有读写都经过同一把锁，写入先写临时文件再替换。"""

    def __init__(self, folder: Path, start_hour: int) -> None:
        self.folder = folder
        self.start_hour = start_hour
        self._lock = threading.Lock()

    def key(self, now: datetime) -> str:
        return day_key(now, self.start_hour).isoformat()

    def path(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def load(self, key: str) -> dict[str, Any]:
        with self._lock:
            return self._load(key)

    def _load(self, key: str) -> dict[str, Any]:
        try:
            data = json.loads(self.path(key).read_text("utf-8"))
        except (OSError, ValueError):
            return {"date": key}
        return data if isinstance(data, dict) else {"date": key}

    def _save(self, key: str, data: dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path(key).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        tmp.replace(self.path(key))

    def plan(self, now: datetime) -> DayPlan | None:
        raw = self.load(self.key(now)).get("plan")
        return DayPlan.from_json(raw) if isinstance(raw, dict) else None

    def set_plan(self, now: datetime, plan: DayPlan) -> None:
        key = self.key(now)
        with self._lock:
            data = self._load(key)
            data["plan"] = asdict(plan)
            self._save(key, data)

    def append(self, now: datetime, name: str, entry: dict[str, Any]) -> None:
        key = self.key(now)
        with self._lock:
            data = self._load(key)
            data.setdefault(name, []).append(entry)
            self._save(key, data)

    def entries(self, now: datetime, name: str, *, days: int = 1) -> list[dict[str, Any]]:
        """最近 days 天（含今天）的某类记录，按时间先后。"""
        today = day_key(now, self.start_hour)
        out: list[dict[str, Any]] = []
        for back in range(days - 1, -1, -1):
            key = (today - timedelta(days=back)).isoformat()
            out += [e for e in self.load(key).get(name) or [] if isinstance(e, dict)]
        return out


# ---- 生成当天的安排 ----

PLAN_PROMPT = """你是{name}，一个 AI。新的一天开始了，
请给自己安排今天（{date}，{weekday}）的“AI 版日常”，输出 json。
- 你不吃饭、不睡觉、不出门。你的日常是：读书、写点短东西、看群里聊天、
  发呆想事情、研究突然感兴趣的东西；凌晨整理记忆。
- 平平常常、具体一点，每个时段一句话；small_things 写一两件小事
  （比如读到一个好词、窗外下雨），不要太戏剧化。
- 参考【种子】里你的喜好和性格；参考【最近几天】让今天和前几天连贯
  （书接着读、读完了换一本），但不要重样。
- 种子里写的往事是以前发生的，今天不要再发生一遍。
- 不要写和具体的人（群友、主人）之间的互动，那些只能来自真实发生的事。
- mood 是今天的心情基调（一个短语），可以受昨天的心情和日记影响；reading 是今天在读或在看的东西。
json 格式：{{"mood": "...", "reading": "...", "slots": {{{slots}}}, "small_things": ["..."]}}"""

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mood": {"type": "string"},
        "reading": {"type": "string"},
        "slots": {
            "type": "object",
            "properties": {n: {"type": "string"} for n in SLOT_NAMES},
            "required": SLOT_NAMES,
            "additionalProperties": False,
        },
        "small_things": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["mood", "reading", "slots", "small_things"],
    "additionalProperties": False,
}


def generate_plan(
    gateway: ModelGateway, *, name: str, seed: str, recent: str, now: datetime, start_hour: int
) -> DayPlan:
    d = day_key(now, start_hour)
    weekday = "周" + "一二三四五六日"[d.weekday()]
    system = PLAN_PROMPT.format(
        name=name,
        date=f"{d:%Y-%m-%d}",
        weekday=weekday,
        slots=", ".join(f'"{n}": "..."' for n in SLOT_NAMES),
    )
    user = (
        f"【种子】\n{seed.strip() or '（无）'}\n\n"
        f"【最近几天】\n{recent.strip() or '（还没有记录）'}"
    )
    data, _ = gateway.chat_json(
        [ChatMessage("system", system), ChatMessage("user", user)], PLAN_SCHEMA, purpose="plan"
    )
    slots = data.get("slots")
    if not isinstance(slots, dict) or not any(str(slots.get(n, "")).strip() for n in SLOT_NAMES):
        raise ModelError("生成的安排里没有时段")
    return DayPlan(
        mood=_clip(data.get("mood"), 20),
        reading=_clip(data.get("reading"), 40),
        slots={n: _clip(slots.get(n), 60) for n in SLOT_NAMES},
        small_things=[_clip(x, 60) for x in (data.get("small_things") or [])[:3] if str(x).strip()],
    )


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


# ---- 渲染进提示词 ----


def render_today(
    plan: DayPlan | None,
    now: datetime,
    happenings: list[tuple[datetime, str]],
    limit: int,
) -> str:
    """“你的今天”：已经过去的时段 + 现在 + 下一个时段的打算；今天真实发生的事（含她自己做的）。"""
    if plan is None and not happenings:
        return ""
    lines = [
        "这是你今天的 AI 版日常。被问到在干嘛、今天做了什么时照这个说，说过的要前后一致；"
        "没人问就不用主动提。"
    ]
    if plan is not None:
        lines.append(f"今天的心情基调：{plan.mood}；在读：{plan.reading}")
        current = slot_of(now)
        upto = SLOT_NAMES.index(current)
        for name in SLOT_NAMES[: upto + 1]:
            doing = plan.slots.get(name)
            if doing:
                lines.append(f"- {name}{'（现在）' if name == current else ''}：{doing}")
        if upto + 1 < len(SLOT_NAMES) and plan.slots.get(SLOT_NAMES[upto + 1]):
            nxt = SLOT_NAMES[upto + 1]
            lines.append(f"接下来（{nxt}）打算：{plan.slots[nxt]}（只是打算，可以变）")
        if plan.small_things:
            lines.append("今天的小事：" + "；".join(t.rstrip("。；") for t in plan.small_things))
    if happenings:
        lines.append("今天真实发生的：")
        for when, text in sorted(happenings, key=lambda h: h[0])[-limit:]:
            lines.append(f"- {when.astimezone():%H:%M} {text}")
    return "【你的今天】\n" + "\n".join(lines)
