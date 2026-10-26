"""每日“睡眠”：整理会话 → 遗忘 → 合并重复事实 → 反思（人物档案、自我认知、日记）
→ 生成新一天的安排 → 提交记忆仓库。

反思写下的每一条认识都要附出处（记忆 id 的前 8 位），或者是档案里原本就有的内容；
模型编出来、找不到依据的条目一律丢弃。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from companion.life import day_key, day_start
from companion.memory import forgetting
from companion.memory.index import Episode, Fact
from companion.memory.layout import commit_memory, render_template
from companion.memory.people import SELF_ID, dump_person
from companion.models.openai_compat import ChatMessage, ModelError

if TYPE_CHECKING:
    from companion.core.companion import Companion

log = logging.getLogger(__name__)

LAST_SLEEP = "sleep:last"
LAST_IDENTITY = "sleep:last_identity"
DUPLICATE_COSINE = 0.95

PERSON_SECTIONS = ["偏好", "关系与称呼", "共同经历", "只对 ta 使用的说法"]
SELF_SECTIONS = ["我是谁", "我在意的事", "最近的我"]


def _sections_schema(names: list[str]) -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text", "sources"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {n: {"type": "array", "items": item} for n in names},
        "required": names,
        "additionalProperties": False,
    }


PERSON_PROMPT = """你是 {name}，正在睡前整理自己对“{who}”的了解。请输出 json。
根据【现有档案】和【新的记忆】，重写档案的这几个部分：{sections}。
- 每条一句话，写你对 ta 的认识或你们之间的事，用“ta”或名字，不要用“他/她”。
- 每条的 sources 填依据的记忆编号（方括号里的 8 位编号）；保留现有档案里仍然成立的条目时，text 原样照抄、sources 可留空。
- 新记忆推翻了旧认识的，用新的；没有依据的不要写。“只对 ta 使用的说法”是你和 ta 之间专属的称呼、梗或说话方式。
- 每部分最多 8 条，宁缺毋滥。
json 格式：{{"偏好": [{{"text": "...", "sources": ["ab12cd34"]}}], ...}}"""

SELF_PROMPT = """你是 {name}，正在做每周一次的自我反思。请输出 json。
根据【种子】【现有的自我认知】和【这段时间的记忆】，重写自我认知的这几个部分：{sections}。
- 用第一人称，每条一句话。种子是你的核心，绝不违背它；变化要小步、有理由。
- 提到别人时用名字或 ta，不知道性别时不要用 他/她。
- 新增或改变的认识至少要有两条不同记忆作依据（sources 填编号）；保留现有条目时 text 原样照抄、sources 可留空。
- 每部分最多 6 条。
json 格式：{{"我是谁": [{{"text": "...", "sources": ["ab12cd34", "cd34ef56"]}}], ...}}"""

JOURNAL_PROMPT = """你是 {name}。根据今天的经历写一篇很短的日记（100~200 字），用第一人称，
语气符合你的性格，写下今天和谁做了什么、自己做了什么、心情有什么起伏。
提到别人时用名字或 ta，不知道性别时不要用 他/她。只输出日记正文。"""


@dataclass
class SleepReport:
    extracted: int = 0
    faded: int = 0
    dormant: int = 0
    merged: int = 0
    people_reflected: list[str] = field(default_factory=list)
    identity_reflected: bool = False
    journal: str | None = None
    planned: bool = False  # 新一天的安排已经有了
    errors: list[str] = field(default_factory=list)


class Sleeper:
    def __init__(self, companion: Companion) -> None:
        self.c = companion

    def due(self, now: datetime | None = None) -> bool:
        """这一天（从睡眠钟点算起）还没睡过。记在索引里，服务重启也不会让她一天睡好几次。"""
        now = now or datetime.now(UTC)
        last = self._last(LAST_SLEEP)
        return last is None or last < day_start(now, self.c.config.sleep.hour)

    def run(self, now: datetime | None = None) -> SleepReport:
        now = now or datetime.now(UTC)
        report = SleepReport()
        last = self._last(LAST_SLEEP) or now - timedelta(days=1)

        try:
            report.extracted = len(self.c.extract_pending(now=now))
        except ModelError as e:
            report.errors.append(f"整理会话：{e}")
        report.faded, report.dormant = self._forget(now)
        report.merged = self._merge_duplicates(now)

        new_episodes = self.c.index.episodes_created_since(last)
        new_facts = self.c.index.facts_created_since(last)
        touched = {f.subject for f in new_facts if f.subject != SELF_ID}
        for ep in new_episodes:
            touched |= {p for p in ep.participants if p != SELF_ID}
        for pid in sorted(touched):
            try:
                if self._reflect_person(pid, new_episodes, new_facts):
                    report.people_reflected.append(pid)
            except ModelError as e:
                report.errors.append(f"反思 {pid}：{e}")

        last_identity = self._last(LAST_IDENTITY)
        every = timedelta(days=self.c.config.sleep.identity_every_days)
        if last_identity is None or now - last_identity >= every:
            since = last_identity or now - every
            try:
                report.identity_reflected = self._reflect_self(since)
                self.c.index.set_meta(LAST_IDENTITY, now.isoformat())
            except ModelError as e:
                report.errors.append(f"自我反思：{e}")

        # 日记写的是“上次睡到现在”这段时间，记在这段时间中点所在的那一天
        key = day_key(last + (now - last) / 2, self.c.config.sleep.hour).isoformat()
        today = [
            ep
            for ep in new_episodes
            if ep.state != "dormant" and not ep.channel.startswith("self:")  # 自己做的事见 notes
        ]
        notes = self.c.life.day_notes(key)
        if today or notes:
            try:
                report.journal = self._journal(today, notes, key, now)
            except ModelError as e:
                report.errors.append(f"日记：{e}")

        report.planned = self.c.life.ensure_plan(now) is not None
        self.c.index.set_meta(LAST_SLEEP, now.isoformat())
        commit_memory(self.c.layout, self.c.name, _summary(report, now))
        return report

    # ---- 遗忘 ----

    def _forget(self, now: datetime) -> tuple[int, int]:
        faded = dormant = 0
        for eid in self.c.index.episode_ids(("vivid", "faded")):
            strength = self.c.index.strength("episode", eid)
            r = strength.retrievability(now)
            ep = self.c.index.get_episode(eid)
            assert ep is not None
            if r < forgetting.DORMANT_BELOW:
                self.c.index.set_episode_state(eid, "dormant")
                dormant += 1
            elif r < forgetting.FADED_BELOW and ep.state == "vivid":
                self.c.index.set_episode_state(eid, "faded")
                faded += 1
        return faded, dormant

    # ---- 合并重复事实 ----

    def _merge_duplicates(self, now: datetime) -> int:
        model = self.c.embedder.model
        if not model:
            return 0
        keys, matrix = self.c.index.load_vectors(model)
        facts = [(k[1], i) for i, k in enumerate(keys) if k[0] == "fact"]
        by_subject: dict[tuple[str, str], list[tuple[Fact, np.ndarray]]] = {}
        for fid, row in facts:
            fact = self.c.index.get_fact(fid)
            if fact is not None and fact.valid_to is None:
                by_subject.setdefault((fact.subject, fact.predicate), []).append(
                    (fact, matrix[row])
                )
        merged = 0
        for group in by_subject.values():
            group.sort(key=lambda x: x[0].valid_from)
            alive = [True] * len(group)
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if (
                        alive[i]
                        and alive[j]
                        and float(group[i][1] @ group[j][1]) >= DUPLICATE_COSINE
                    ):
                        self.c.index.supersede(group[i][0].id, group[j][0].id, now)
                        alive[i] = False
                        merged += 1
        return merged

    # ---- 反思 ----

    def _reflect_person(self, pid: str, episodes: list[Episode], facts: list[Fact]) -> bool:
        person = self.c.people.get(pid)
        if person is None:
            return False
        who = person.names[0] if person.names else pid
        mine_eps = [e for e in episodes if pid in e.participants]
        mine_facts = [f for f in self.c.index.current_facts(pid)]
        new_ids = {f.id for f in facts}
        memories = [(e.id, e.summary) for e in mine_eps] + [
            (f.id, f.statement + ("（新）" if f.id in new_ids else "")) for f in mine_facts
        ]
        if not memories:
            return False
        data = self._ask(
            PERSON_PROMPT.format(name=self.c.name, who=who, sections="、".join(PERSON_SECTIONS)),
            f"【现有档案】\n{person.body.strip()}\n\n【新的记忆】\n" + _list_memories(memories),
            _sections_schema(PERSON_SECTIONS),
        )
        sections = _validated(data, PERSON_SECTIONS, memories, person.body, min_sources=1)
        header = render_template("person.md", display_name=who).split("## ")[0].rstrip()
        person.body = header + "\n\n" + _render_sections(sections) + "\n"
        path = self.c.layout.person_path(pid)
        path.write_text(dump_person(person), encoding="utf-8", newline="\n")
        self.c.people.reload()
        return True

    def _reflect_self(self, since: datetime) -> bool:
        layout = self.c.layout
        identity_path = layout.self_dir / "identity.md"
        current = identity_path.read_text("utf-8") if identity_path.exists() else ""
        seed = (layout.self_dir / "seed.md").read_text("utf-8")
        episodes = self.c.index.episodes_created_since(since)
        facts = self.c.index.current_facts(SELF_ID)
        memories = [(e.id, e.summary) for e in episodes] + [(f.id, f.statement) for f in facts]
        if len(memories) < 2:
            return False
        data = self._ask(
            SELF_PROMPT.format(name=self.c.name, sections="、".join(SELF_SECTIONS)),
            f"【种子】\n{seed}\n\n【现有的自我认知】\n{current}\n\n【这段时间的记忆】\n"
            + _list_memories(memories),
            _sections_schema(SELF_SECTIONS),
        )
        sections = _validated(data, SELF_SECTIONS, memories, current, min_sources=2)
        if not any(sections.values()):
            return False
        header = render_template("identity.md", name=self.c.name).split("## ")[0].rstrip()
        identity_path.write_text(
            header + "\n\n" + _render_sections(sections) + "\n", encoding="utf-8", newline="\n"
        )
        return True

    def _journal(self, episodes: list[Episode], notes: str, key: str, now: datetime) -> str:
        lines = "\n".join(f"- {e.started_at.astimezone():%H:%M} {e.summary}" for e in episodes)
        user = f"今天的经历：\n{lines or '（今天没怎么和人聊天）'}"
        if notes:
            user += f"\n\n今天自己的状态：\n{notes}"
        result = self.c.gateway.chat(
            [
                ChatMessage("system", JOURNAL_PROMPT.format(name=self.c.name)),
                ChatMessage("user", user),
            ],
            purpose="journal",
        )
        text = result.text.strip()
        path = self.c.layout.journal_dir / f"{key}.md"
        if path.exists():  # 这一天已经写过（如手动整理过一次）：接在后面，不覆盖
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(f"\n（{now.astimezone():%H:%M} 补记）\n\n{text}\n")
        else:
            path.write_text(f"# {key}\n\n{text}\n", encoding="utf-8", newline="\n")
        return str(path)

    def _ask(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        data, _ = self.c.gateway.chat_json(
            [ChatMessage("system", system), ChatMessage("user", user)], schema, purpose="reflect"
        )
        return data

    def _last(self, key: str) -> datetime | None:
        raw = self.c.index.get_meta(key)
        return datetime.fromisoformat(raw) if raw else None


def _list_memories(memories: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{mid[:8]}] {text}" for mid, text in memories)


def _validated(
    data: dict[str, Any],
    names: list[str],
    memories: list[tuple[str, str]],
    existing: str,
    *,
    min_sources: int,
) -> dict[str, list[str]]:
    """只留下有出处、或原本就在档案里的条目；出处写成〔编号〕附在句末。"""
    known = {mid[:8] for mid, _ in memories}
    out: dict[str, list[str]] = {}
    for name in names:
        lines: list[str] = []
        for item in data.get(name) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            sources = [s for s in item.get("sources") or [] if s in known]
            if text in existing or _strip_sources(text) in existing:
                lines.append(text)  # 原样保留的旧条目
            elif len(set(sources)) >= min_sources:
                lines.append(f"{_strip_sources(text)}〔{'、'.join(dict.fromkeys(sources))}〕")
            else:
                log.info("反思时丢弃没有依据的条目：%s", text)
        out[name] = lines
    return out


def _strip_sources(text: str) -> str:
    return text.split("〔")[0].strip()


def _render_sections(sections: dict[str, list[str]]) -> str:
    parts = []
    for name, lines in sections.items():
        body = "\n".join(f"- {line}" for line in lines)
        parts.append(f"## {name}\n\n{body}" if body else f"## {name}")
    return "\n\n".join(parts)


def _summary(report: SleepReport, now: datetime) -> str:
    bits = [f"睡眠 {now.astimezone():%m-%d}"]
    if report.extracted:
        bits.append(f"整理 {report.extracted} 段会话")
    if report.faded or report.dormant:
        bits.append(f"淡忘 {report.faded}、沉睡 {report.dormant}")
    if report.merged:
        bits.append(f"合并 {report.merged} 条重复事实")
    if report.people_reflected:
        bits.append(f"更新了 {len(report.people_reflected)} 个人的档案")
    if report.identity_reflected:
        bits.append("更新了自我认知")
    if report.planned:
        bits.append("排好了今天的安排")
    return "：".join([bits[0], "，".join(bits[1:]) or "没有变化"])
