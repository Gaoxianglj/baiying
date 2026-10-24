"""上下文组装：决定白璎此刻“心里装着什么”。

常驻部分（不依赖检索命中）：种子、自我认知、表达风格、对话者档案与事实、最近的共同经历。
检索部分：按“相关度 × 可回忆程度 × 重要度”排序的记忆；淡化的会标注“记不太清”。
最后带上同一频道最近的对话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from companion.config import RetrievalConfig
from companion.core.retrieval import HybridRetriever
from companion.memory import forgetting
from companion.memory.events import Event, EventLog
from companion.memory.index import Episode, Fact, MemoryIndex, MemoryKind
from companion.memory.layout import MemoryLayout
from companion.memory.people import SELF_ID, PeopleRegistry, Person
from companion.models.openai_compat import ChatMessage

_SCENES = {
    "qq": "在 QQ 上",
    "minecraft": "在《我的世界》里",
    "cli": "在电脑的命令行里",
}


@dataclass
class Recalled:
    kind: MemoryKind
    memory_id: str
    text: str
    when: datetime
    score: float
    faded: bool


@dataclass
class ContextBundle:
    system_prompt: str
    messages: list[ChatMessage]
    recalled: list[Recalled] = field(default_factory=list)
    standing_fact_ids: list[str] = field(default_factory=list)


class ContextAssembler:
    def __init__(
        self,
        *,
        persona_name: str,
        layout: MemoryLayout,
        index: MemoryIndex,
        retriever: HybridRetriever,
        people: PeopleRegistry,
        log: EventLog,
        config: RetrievalConfig,
    ) -> None:
        self.name = persona_name
        self.layout = layout
        self.index = index
        self.retriever = retriever
        self.people = people
        self.log = log
        self.cfg = config

    def build(
        self,
        *,
        person_id: str,
        body: str,
        channel: str,
        query: str,
        now: datetime | None = None,
        sticker_emotions: list[str] | None = None,
    ) -> ContextBundle:
        now = now or datetime.now(UTC)
        person_id = self.people.canonical(person_id)
        person = self.people.get(person_id)

        facts = self._standing_facts(person_id)
        shared = [
            ep
            for ep in self.index.episodes_with(person_id, limit=self.cfg.recent_episodes * 2)
            if ep.state != "dormant"
        ][: self.cfg.recent_episodes]
        skip = {("fact", f.id) for f in facts} | {("episode", e.id) for e in shared}
        recalled = self._recall(query, now, skip)
        dialogue = self._recent_dialogue(channel, now)

        prompt = self._render(person, person_id, body, channel, facts, shared, recalled, now)
        if sticker_emotions:
            prompt += (
                "\n\n【表情包】\n你收藏了一些表情包，能表达："
                + "、".join(sticker_emotions)
                + "。\n"
                "如果这句话很适合配一张，就在回复最后单独写【表情：情绪词】（从上面选），"
                "不适合就不写。大多数时候不需要配，别每句都发。"
            )
        messages = [ChatMessage("system", prompt), *dialogue]
        if not dialogue or dialogue[-1].role != "user":
            messages.append(ChatMessage("user", query))
        return ContextBundle(prompt, messages, recalled, [f.id for f in facts])

    def mark_recalled(self, bundle: ContextBundle, now: datetime | None = None) -> None:
        for r in bundle.recalled:
            self.index.mark_recalled(r.kind, r.memory_id, now=now)

    # ---- 组成部分 ----

    def _standing_facts(self, person_id: str) -> list[Fact]:
        facts = self.index.current_facts(person_id)
        facts.sort(key=lambda f: (f.pinned, f.importance, f.valid_from), reverse=True)
        return facts[: self.cfg.standing_facts]

    def _recall(self, query: str, now: datetime, skip: set[tuple[str, str]]) -> list[Recalled]:
        out: list[Recalled] = []
        for c in self.retriever.search(query, limit=self.cfg.limit * 2):
            if (c.kind, c.memory_id) in skip or c.relevance < self.cfg.min_relevance:
                continue
            strength = self.index.strength(c.kind, c.memory_id)
            r = strength.retrievability(now)
            if r < forgetting.DORMANT_BELOW:
                continue  # 想不起来了
            importance = self.index.importance(c.kind, c.memory_id)
            if c.kind == "episode":
                ep = self.index.get_episode(c.memory_id)
                assert ep is not None
                text, when = ep.summary, ep.started_at
            else:
                fact = self.index.get_fact(c.memory_id)
                assert fact is not None
                text, when = fact.statement, fact.valid_from
            score = c.relevance * r * (0.5 + 0.5 * importance)
            out.append(Recalled(c.kind, c.memory_id, text, when, score, r < forgetting.FADED_BELOW))
        out.sort(key=lambda x: x.score, reverse=True)
        return out[: self.cfg.limit]

    def _recent_dialogue(self, channel: str, now: datetime) -> list[ChatMessage]:
        since = now - timedelta(hours=self.cfg.recent_hours)
        events = sorted(
            (
                e
                for e in self.log.iter_events(since=since)
                if e.channel == channel and e.kind == "message" and e.ts <= now
            ),
            key=lambda e: e.ts,
        )[-self.cfg.recent_messages :]
        return [self._as_message(e) for e in events]

    def _as_message(self, e: Event) -> ChatMessage:
        if e.actor.person_id == SELF_ID:
            return ChatMessage("assistant", e.content)
        who = e.actor.display_name or self._display(e.actor.person_id) or e.actor.platform_id
        return ChatMessage("user", f"{who}：{e.content}")

    def _display(self, person_id: str | None) -> str | None:
        if person_id is None:
            return None
        person = self.people.get(person_id)
        return person.names[0] if person and person.names else None

    # ---- 渲染 ----

    def _render(
        self,
        person: Person | None,
        person_id: str,
        body: str,
        channel: str,
        facts: list[Fact],
        shared: list[Episode],
        recalled: list[Recalled],
        now: datetime,
    ) -> str:
        who = person.names[0] if person and person.names else person_id
        scene = _SCENES.get(body, f"在{body}上")
        place = "群聊" if channel.startswith("group:") else "私聊"
        sections = [
            f"你是{self.name}。",
            _section("你的种子（核心性格与底线，必须遵守）", self._read_self("seed.md")),
            _section("你对自己的认识", self._read_self("identity.md")),
            _section("你的表达习惯", self._read_self("style.md")),
            f"现在是 {now.astimezone():%Y-%m-%d %H:%M}（{_weekday(now)}）。"
            f"你正{scene}和{who}{place}。",
        ]
        if person is not None:
            also = "、".join(person.names[1:])
            sections.append(
                _section(
                    f"你对{who}的了解" + (f"（ta 也叫 {also}）" if also else ""),
                    _clean_markdown(person.body),
                )
            )
        if facts:
            lines = [f"- {f.statement}（{_date(f.valid_from)} 得知）" for f in facts]
            sections.append(_section(f"关于{who}的事实", "\n".join(lines)))
        if shared:
            lines = [f"- [{_date(e.started_at)}] {e.summary}" for e in shared]
            sections.append(_section(f"你和{who}最近的共同经历", "\n".join(lines)))
        if recalled:
            lines = [
                f"- [{_date(r.when)}] {r.text}" + ("（记得不太清了）" if r.faded else "")
                for r in recalled
            ]
            sections.append(_section("此刻想起的相关记忆（越靠前越清晰）", "\n".join(lines)))
        sections.append(
            _section(
                "说话时注意",
                "- 只依据上面的记忆谈论过去；没写到的事不要编造，记不清就直说。\n"
                "- 记忆是让你自然地用上的，不要逐条复述，也不要说“根据我的记忆”。\n"
                "- 回复简短自然，像即时聊天，一般一两句话。",
            )
        )
        return "\n\n".join(s for s in sections if s)

    def _read_self(self, name: str) -> str:
        path = self.layout.self_dir / name
        return _clean_markdown(path.read_text("utf-8")) if path.exists() else ""


def _clean_markdown(text: str) -> str:
    """去掉模板里的说明行、空标题和占位符，只留实际内容。"""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">") or s.startswith("# ") or "（待写" in s:
            continue
        lines.append(line.rstrip())
    # 去掉下面没有内容的二级标题
    out: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            rest = next((x for x in lines[i + 1 :] if x.strip()), "")
            if not rest or rest.startswith("## "):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _section(title: str, content: str) -> str:
    return f"【{title}】\n{content}" if content.strip() else ""


def _date(dt: datetime) -> str:
    return f"{dt.astimezone():%Y-%m-%d}"


def _weekday(dt: datetime) -> str:
    return "周" + "一二三四五六日"[dt.astimezone().weekday()]
