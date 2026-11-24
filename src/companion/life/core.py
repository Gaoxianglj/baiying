"""情感与生活：今日状态（AI 版日常）、心情、主动找主人、自主活动。

Companion 持有一个 Life：组装上下文时向它要“你的今天”“你现在的心情”两段；回复里的
【心情：…】交给它记下；睡眠时由它生成新一天的安排，并给日记提供当天的心情和自己做过的事。
主动找主人的规矩在 `owner`；额度充足时做什么、做完记在哪，也在这里。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion.config import Config
from companion.life.activities import KINDS, ActivityPrep, ActivityResult, QuotaSession, quota_check
from companion.life.days import (
    DayBook,
    DayPlan,
    day_key,
    day_start,
    generate_plan,
    render_today,
    slot_of,
)
from companion.life.mood import RULE, MoodNow, current_mood, render_mood
from companion.life.owner import OwnerPolicy
from companion.memory.events import EventLog
from companion.memory.index import Episode, MemoryIndex
from companion.memory.layout import MemoryLayout
from companion.models.codex_app_server import RateLimits
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ModelError

log = logging.getLogger(__name__)

PLAN_RETRY = timedelta(minutes=30)  # 生成安排失败后，隔多久再试
LIMITS_MAX_AGE = timedelta(minutes=5)  # 判断要不要自主活动时，额度读数最多用多旧的
LIFE_CHANNEL = "self:life"  # 她自己做的事记在这个频道，之后像聊天一样整理成记忆
QUOTA_META = "life:quota"
LAST_ACTIVITY_META = "life:last_activity"


class Life:
    def __init__(
        self,
        *,
        config: Config,
        layout: MemoryLayout,
        index: MemoryIndex,
        gateway: ModelGateway,
        log: EventLog,
    ) -> None:
        self.cfg = config.life
        self.name = config.persona.name
        self.start_hour = config.sleep.hour
        self.layout = layout
        self.index = index
        self.gateway = gateway
        self.days = DayBook(layout.self_dir / "days", self.start_hour)
        self.owner = OwnerPolicy(self.cfg.owner_chat, self.days, log)
        self._plan_failed_at: datetime | None = None
        self._limits: tuple[datetime, RateLimits] | None = None

    # ---- 提示词 ----

    def talk_rules(self) -> str:
        """加进“说话时注意”的规则（不常变）。"""
        return RULE if self.cfg.mood.enabled else ""

    def sections(self, person_id: str, channel: str, now: datetime) -> list[str]:
        """“你的今天”“你现在的心情”两段（常变，放在提示词后半）。"""
        out: list[str] = []
        plan = self.days.plan(now) if self.cfg.day.enabled else None
        if self.cfg.day.enabled:
            happenings = self._happenings(person_id, channel, now)
            out.append(render_today(plan, now, happenings, self.cfg.day.max_happenings))
        if self.cfg.mood.enabled:
            out.append(render_mood(self.mood(now), plan.mood if plan else None, channel))
        return [s for s in out if s]

    def _happenings(
        self, person_id: str, channel: str, now: datetime
    ) -> list[tuple[datetime, str]]:
        """今天真实发生的：整理好的经历（按隐私规则筛过）+ 她自己做过的事。"""
        start = day_start(now, self.start_hour)
        out: list[tuple[datetime, str]] = []
        for ep in self.index.episodes_started_since(start):
            if ep.started_at <= now and ep.state != "dormant" and _visible(ep, person_id, channel):
                out.append((ep.started_at, _clip(ep.summary, 80)))
        for a in self.days.entries(now, "activities"):
            try:
                ts = datetime.fromisoformat(a["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts <= now:
                summary = f"：{_clip(a.get('summary', ''), 60)}" if a.get("summary") else ""
                out.append((ts, f"（你自己）{a.get('title', '')}{summary}"))
        return out

    # ---- 心情 ----

    def mood(self, now: datetime) -> MoodNow | None:
        m = self.cfg.mood
        return current_mood(
            self.days.entries(now, "moods", days=2),
            now,
            half_life_minutes=m.half_life_minutes,
            negative_half_life_minutes=m.negative_half_life_minutes,
        )

    def note_mood(
        self,
        word: str,
        cause: str,
        *,
        channel: str,
        person_id: str | None,
        now: datetime | None = None,
    ) -> None:
        if not self.cfg.mood.enabled:
            return
        now = now or datetime.now(UTC)
        entry = {"ts": now.isoformat(), "word": word, "cause": cause, "channel": channel}
        if person_id:
            entry["person"] = person_id
        self.days.append(now, "moods", entry)
        log.info("心情变了：%s（%s）", word, cause or "没说原因")

    # ---- 当天的安排 ----

    def ensure_plan(self, now: datetime | None = None) -> DayPlan | None:
        """今天还没有安排就生成一份（每天一次；失败了隔一会儿再试）。"""
        if not self.cfg.day.enabled:
            return None
        now = now or datetime.now(UTC)
        plan = self.days.plan(now)
        if plan is not None:
            return plan
        if self._plan_failed_at is not None and now - self._plan_failed_at < PLAN_RETRY:
            return None
        try:
            plan = generate_plan(
                self.gateway,
                name=self.name,
                seed=self._persona_text(),
                recent=self.recent_text(now),
                now=now,
                start_hour=self.start_hour,
            )
        except ModelError as e:
            self._plan_failed_at = now
            log.warning("生成今天的安排失败（%s 后再试）：%s", PLAN_RETRY, e)
            return None
        self._plan_failed_at = None
        self.days.set_plan(now, plan)
        log.info("今天的安排：基调 %s；在读 %s", plan.mood, plan.reading)
        return plan

    def recent_text(self, now: datetime) -> str:
        """最近几天的安排、心情、自己做的事和日记，给生成新安排时参考。"""
        blocks = []
        today = day_key(now, self.start_hour)
        for back in range(self.cfg.day.history_days, 0, -1):
            key = (today - timedelta(days=back)).isoformat()
            notes = self.day_notes(key)
            journal = self._journal(key)
            if journal:
                notes += ("\n" if notes else "") + "日记：" + _clip(journal, 200)
            if notes:
                blocks.append(f"{key}\n{notes}")
        return "\n\n".join(blocks)

    def day_notes(self, key: str) -> str:
        """某一天的基调、心情变化、自己做的事（写日记和生成新安排时用）。"""
        data = self.days.load(key)
        lines = []
        plan = data.get("plan")
        if isinstance(plan, dict):
            lines.append(f"基调：{plan.get('mood', '')}；在读：{plan.get('reading', '')}")
        moods = [m for m in data.get("moods") or [] if isinstance(m, dict)]
        if moods:
            lines.append(
                "心情变化："
                + "；".join(
                    f"{_hm(m.get('ts'))} {m.get('word', '')}"
                    + (f"（{m['cause']}）" if m.get("cause") else "")
                    for m in moods
                )
            )
        acts = [a for a in data.get("activities") or [] if isinstance(a, dict)]
        if acts:
            lines.append("自己做的事：" + "；".join(str(a.get("title", "")) for a in acts))
        return "\n".join(lines)

    # ---- 自主活动 ----

    def prepare_activity(
        self, now: datetime, *, owner_name: str | None, owner_ok: bool
    ) -> ActivityPrep | None:
        """额度充足、离上次够久，就准备好挑事情的材料；否则返回 None。"""
        cfg = self.cfg.activities
        if not cfg.enabled:
            return None
        kinds = [k for k in KINDS if getattr(cfg, k)]
        if not owner_ok:
            kinds = [k for k in kinds if k != "chat_owner"]
        if not kinds:
            return None
        last = self.index.get_meta(LAST_ACTIVITY_META)
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=cfg.min_gap_minutes):
            return None
        # 只用订阅额度：被手动指定别的后端、额度不合适、或订阅暂停（在用 DeepSeek）时不做。
        # 额度读数缓存 5 分钟，先看它，免得每分钟都去问订阅。
        if self.gateway.prefer not in ("auto", "subscription"):
            return None
        limits = self._rate_limits(now)
        if limits is None:
            return None
        session = QuotaSession.from_json(self.index.get_meta(QUOTA_META))
        session, why = quota_check(limits, session, cfg, now)
        if session is None:
            log.debug("先不做自主活动：%s", why)
            return None
        if not self.gateway.subscription_available(now):
            return None
        self.index.set_meta(QUOTA_META, session.to_json())
        return ActivityPrep(
            name=self.name,
            owner_name=owner_name if owner_ok else None,
            kinds=kinds,
            context=self._activity_context(now),
            now=now,
        )

    def activity_done(self, now: datetime) -> None:
        """做完（或没做成）一件：记下时间，算进这段自主时间的件数。"""
        self.index.set_meta(LAST_ACTIVITY_META, now.isoformat())
        self._limits = None  # 下次重新读额度，看这件事花了多少
        session = QuotaSession.from_json(self.index.get_meta(QUOTA_META))
        if session is not None:
            session.count += 1
            self.index.set_meta(QUOTA_META, session.to_json())

    def record_activity(
        self, result: ActivityResult, now: datetime, image: str | None = None
    ) -> Path | None:
        """记进当天的生活记录；写的东西另存一份，返回它的路径。"""
        entry: dict[str, object] = {
            "ts": now.isoformat(),
            "kind": result.kind,
            "title": result.title,
            "summary": _clip(result.text, 80),
        }
        saved = None
        if result.kind == "write" and result.text:
            folder = self.layout.self_dir / "writings"
            folder.mkdir(parents=True, exist_ok=True)
            saved = folder / f"{self.days.key(now)}-{now.astimezone():%H%M}.md"
            title = result.title.removeprefix("写了")
            saved.write_text(f"# {title}\n\n{result.text}\n", encoding="utf-8", newline="\n")
            entry["file"] = f"writings/{saved.name}"
        if image:
            entry["image"] = image
        if result.sources:
            entry["sources"] = result.sources
        self.days.append(now, "activities", entry)
        return saved

    def _rate_limits(self, now: datetime) -> RateLimits | None:
        if self._limits is not None and now - self._limits[0] < LIMITS_MAX_AGE:
            return self._limits[1]
        sub = self.gateway.subscription
        if sub is None:
            return None
        try:
            limits = sub.read_rate_limits()
        except ModelError as e:
            log.warning("读取订阅额度失败：%s", e)
            return None
        self._limits = (now, limits)
        return limits

    def _activity_context(self, now: datetime) -> str:
        """挑事情时参考：兴趣、今天的安排、心情、最近做过的事、群里最近在聊的（不含私聊）。"""
        prefs = self._read("preferences.md")
        seed = self._read("seed.md")
        hobbies = _seed_section(prefs, "闲下来") or _seed_section(seed, "闲下来")
        tastes = _seed_section(prefs, "读书与品味")
        interest = "\n".join(x for x in (hobbies, tastes) if x) or _clip(seed, 600)
        parts = ["【你的兴趣】\n" + interest]
        plan = self.days.plan(now)
        if plan is not None:
            slot = slot_of(now)
            parts.append(
                f"【你的今天】\n基调：{plan.mood}；在读：{plan.reading}；"
                f"现在（{slot}）本来打算：{plan.slots.get(slot, '')}"
            )
        mood = self.mood(now)
        if mood is not None:
            parts.append(
                f"【你现在的心情】\n{mood.word}" + (f"（{mood.cause}）" if mood.cause else "")
            )
        done = self.days.entries(now, "activities", days=7)
        if done:
            parts.append(
                "【最近做过的事】\n" + "\n".join(f"- {a.get('title', '')}" for a in done[-12:])
            )
        talk = [
            ep.summary
            for ep in self.index.episodes_started_since(now - timedelta(days=1))
            if ep.channel.startswith("group:") and ep.state != "dormant"
        ]
        if talk:
            parts.append("【最近大家在聊的】\n" + "\n".join(f"- {_clip(t, 80)}" for t in talk[-5:]))
        return "\n\n".join(parts)

    def _journal(self, key: str) -> str:
        path = self.layout.journal_dir / f"{key}.md"
        if not path.exists():
            return ""
        lines = [x for x in path.read_text("utf-8").splitlines() if not x.startswith("#")]
        return " ".join(x.strip() for x in lines if x.strip())

    def _persona_text(self) -> str:
        parts = [self._read("seed.md").strip(), self._read("preferences.md").strip()]
        return "\n\n".join(p for p in parts if p)

    def _read(self, name: str) -> str:
        path = self.layout.self_dir / name
        return path.read_text("utf-8") if path.exists() else ""


def _visible(ep: Episode, person_id: str, channel: str) -> bool:
    """今天的哪些经历可以在这里提：本频道的、群里的；私聊的只在和当事人聊时提。"""
    if ep.channel.startswith("self:"):
        return False  # 她自己做的事另外列
    if ep.channel == channel or ep.channel.startswith("group:"):
        return True
    return person_id in ep.participants


def _seed_section(seed: str, keyword: str) -> str:
    """种子里标题含 keyword 的那一节（没有就返回空）。"""
    lines = seed.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## ") and keyword in line:
            body = []
            for x in lines[i + 1 :]:
                if x.startswith("## "):
                    break
                body.append(x)
            return "\n".join(body).strip()
    return ""


def _clip(text: object, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _hm(ts: object) -> str:
    try:
        return f"{datetime.fromisoformat(str(ts)).astimezone():%H:%M}"
    except ValueError:
        return ""
