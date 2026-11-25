"""每日“睡眠”：整理会话 → 遗忘 → 合并重复事实 → 反思（人物档案、自我认知、次核心喜好、表达风格、熟络度、日记）
→ 生成新一天的安排 → 提交记忆仓库。

反思写下的每一条认识都要附出处（记忆 id 的前 8 位），或者是档案里原本就有的内容；
模型编出来、找不到依据的条目一律丢弃。次核心喜好、全局表达风格、人物熟络度与对 ta 的说话方式
按同一周期（默认 7 天）同步审视，且默认允许“本次不更新”。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from companion.life import day_key, day_start
from companion.memory import forgetting
from companion.memory.index import Episode, Fact
from companion.memory.layout import commit_memory, render_template
from companion.memory.people import (
    FAMILIARITY_LEVELS,
    SELF_ID,
    Person,
    dump_person,
    familiarity_rank,
)
from companion.models.openai_compat import ChatMessage, ModelError

if TYPE_CHECKING:
    from companion.core.companion import Companion

log = logging.getLogger(__name__)

LAST_SLEEP = "sleep:last"
LAST_IDENTITY = "sleep:last_identity"
DUPLICATE_COSINE = 0.95

PERSON_SECTIONS = ["偏好", "关系与称呼", "共同经历", "只对 ta 使用的说法"]
PERSON_STYLE_SECTION = "对 ta 的说话方式"
ALL_PERSON_SECTIONS = ["偏好", "关系与称呼", "共同经历", PERSON_STYLE_SECTION, "只对 ta 使用的说法"]
SELF_SECTIONS = ["我是谁", "我在意的事", "最近的我"]
PREFERENCE_SECTIONS = ["穿搭与随身物", "读书与品味", "喜欢与不喜欢", "小习惯与熟悉的事", "闲下来喜欢做的事"]
STYLE_SECTIONS = ["口头禅与常用说法", "句式与语气", "熟络后的破壳与打趣", "从大家那里学到的说法"]


def _sections_schema(names: list[str], *, optional_change: bool = False) -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text", "sources"],
        "additionalProperties": False,
    }
    props: dict[str, Any] = {n: {"type": "array", "items": item} for n in names}
    req = list(names)
    if optional_change:
        props = {"changed": {"type": "boolean"}, **props}
        req = ["changed", *req]
    return {
        "type": "object",
        "properties": props,
        "required": req,
        "additionalProperties": False,
    }


def _person_style_schema() -> dict[str, Any]:
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
        "properties": {
            "changed": {"type": "boolean"},
            "familiarity": {"type": "string", "enum": list(FAMILIARITY_LEVELS)},
            PERSON_STYLE_SECTION: {"type": "array", "items": item},
        },
        "required": ["changed", "familiarity", PERSON_STYLE_SECTION],
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

PREFERENCES_PROMPT = """你是 {name}，正在做每周一次的“喜好与习惯（次核心层）”审视。请输出 json。
根据【核心种子】【现有的喜好与习惯】和【这段时间的记忆】，审视是否需要微调：{sections}。
- 这是你的长期品味、日常穿搭随身物、小习惯与闲暇兴趣，不是近期动态流水账。
- 如果这段时间没有真正形成新的稳定喜好、穿搭变化或兴趣拓展，请直接设 "changed": false（各部分可填 []），保持原样。
- 绝不违背【核心种子】里的身份、外貌底色（银白色长发、白色系、优雅气质）、说话硬规则与底线；不要因为群里一句玩笑就推翻原有品味。
- 若确实需要微调，设 "changed": true：本次最多新增或改动 1～2 条，其余现有条目原样保留（text 原样照抄，sources 留空）。
- 新增或改动的条目至少要有两条不同记忆作依据（sources 填 8 位编号）；用第一人称，每部分最多 6 条。
json 格式：{{"changed": false, "穿搭与随身物": [], ...}}"""

STYLE_PROMPT = """你是 {name}，正在做每周一次的“全局表达风格”审视。请输出 json。
根据【核心种子】【现有的表达风格】和【这段时间的记忆与实际发言片段】，审视是否需要更新：{sections}。
- 你的核心底色始终是温柔、安静、有书卷气的文学少女；种子里的硬规则（不用叠词开头、引诗与“～”和颜文字最多十条一条、简短自然）不可违背。
- 严禁把“（）”这类句末括号符号、标点口癖或某一次具体的网络梗/群内旧梗写进全局表达风格（写进常驻层会导致每句机械复读）。
- “熟络后的破壳与打趣”写你在熟人面前偶尔（十句里最多一两句）“端不住文学少女形象”时的温和反差方式（如怎么轻巧嘴硬、怎么岔开话题），不能写成句句耍贫嘴或杠精式吐槽。
- 只对某一个特定朋友私下使用的专属称呼或私密梗不要写进这里（那些属于个人档案）。
- 如果没有新的稳定表达习惯，直接设 "changed": false（各部分可填 []），保持原样。
- 若有更新，设 "changed": true：保留仍然成立的现有条目（text 原样照抄、sources 留空），新增或改动条目至少要有两个不同编号作依据（sources 填 8 位编号），每部分最多 4 条。
json 格式：{{"changed": false, "口头禅与常用说法": [], ...}}"""

PERSON_STYLE_PROMPT = """你是 {name}，正在做每周一次的人际熟络度与说话方式审视（对象：“{who}”）。请输出 json。
根据【核心种子】【你的表达风格】【现有档案】【互动统计】和【这段时间的共同记忆】，审视你对“{who}”的熟络度档位与说话方式。
- 熟络度档位只能从“初识、相熟、熟络、亲近”四档中选，每次最多升降一档，且不能超过【互动统计】给出的客观上限“{ceiling}”：
  - 初识：刚认识或很少直接对话，温和有礼、守住文学少女的端庄分寸；
  - 相熟：聊过几次，依然温和安静，语气自然不客套，偶尔接轻巧玩笑；
  - 熟络：很熟悉的朋友，保留温柔安静底色，说话放松不客套，被逗时十句里有一两句轻轻吐槽或嘴硬；
  - 亲近：深厚信任与默契的挚友/主人，保留温柔底色同时完全卸下拘谨，既能安静聊心事，也会自然打趣或露一点小得意。
- “对 ta 的说话方式”写 1～3 条简洁的语气与分寸指引（如对方不喜欢什么腔调、和 ta 相处偏安静还是偏轻快），不要把具体某一个旧梗写进这里（具体梗归“只对 ta 使用的说法”且只在贴合时偶用），也不要鼓励每句都吐槽破壳。
- 如果当前熟络度和“对 ta 的说话方式”已经准确贴切，直接设 "changed": false，保持原样。
- 若需调整，设 "changed": true：保留的旧条目原样照抄（sources 可留空），新增或改写的条目需填依据的记忆编号（至少 1 个），用“ta”或名字。
json 格式：{{"changed": false, "familiarity": "{current_fam}", "对 ta 的说话方式": []}}"""

JOURNAL_PROMPT = """你是 {name}。根据今天的经历写一篇很短的日记（100~200 字），用第一人称，
语气符合你的性格，写下今天和谁做了什么、自己做了什么、心情有什么起伏。
提到别人时用名字或 ta，不知道性别时不要用 他/她。只输出日记正文。"""


def familiarity_ceiling(
    *,
    active_days: int,
    episodes_count: int,
    facts_count: int,
    is_owner: bool = False,
    is_peer_bot: bool = False,
) -> str:
    """按客观互动沉淀计算允许达到的最高熟络度，防止一天刷屏直接跳满。"""
    if is_owner and episodes_count >= 10:
        return "亲近"
    if not is_peer_bot and active_days >= 14 and episodes_count >= 40 and facts_count >= 15:
        return "亲近"
    if active_days >= 7 and episodes_count >= 15:
        return "熟络"
    if active_days >= 2 and episodes_count >= 3:
        return "相熟"
    return "初识"


def clamp_familiarity(current: str, proposed: str, ceiling: str) -> str:
    """每次审视最多升降 1 档；升级不超过客观上限（主人手动设高过上限的不强行降级）。"""
    if proposed not in FAMILIARITY_LEVELS:
        return current if current in FAMILIARITY_LEVELS else "初识"
    cur = familiarity_rank(current)
    prop = familiarity_rank(proposed)
    ceil = familiarity_rank(ceiling)
    stepped = max(0, min(len(FAMILIARITY_LEVELS) - 1, min(cur + 1, max(cur - 1, prop))))
    if stepped > cur:
        stepped = min(stepped, max(cur, ceil))
    return FAMILIARITY_LEVELS[stepped]


def parse_markdown_sections(text: str) -> dict[str, list[str]]:
    """提取 Markdown 二级标题下的列表条目（不含前导 '- '）。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif current is not None and line.startswith("- "):
            item = line[2:].strip()
            if item:
                sections[current].append(item)
    return sections


@dataclass
class SleepReport:
    extracted: int = 0
    faded: int = 0
    dormant: int = 0
    merged: int = 0
    people_reflected: list[str] = field(default_factory=list)
    identity_reflected: bool = False
    preferences_reflected: bool = False
    style_reflected: bool = False
    familiarity_updated: list[str] = field(default_factory=list)
    journal: str | None = None
    planned: bool = False  # 新一天的安排已经有了
    threads_rotated: int = 0
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
            report.extracted = len(self.c.extract_pending(force=True, now=now))
        except ModelError as e:
            report.errors.append(f"整理会话：{e}")
            # 当前仍在聊的会话也必须先落入长期记忆，才能安全开启新一代线程。
            # 不标记睡眠完成，维护循环会重试，已有抽取游标保证不重复保存。
            return report
        report.faded, report.dormant = self._forget(now)
        report.merged = self._merge_duplicates(now)

        new_episodes = self.c.index.episodes_created_since(last)
        new_facts = self.c.index.facts_created_since(last)
        touched = {
            self.c.people.canonical(f.subject) for f in new_facts if f.subject != SELF_ID
        }
        for ep in new_episodes:
            touched |= {
                self.c.people.canonical(p) for p in ep.participants if p != SELF_ID
            }
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
            weekly_ok = True
            try:
                report.identity_reflected = self._reflect_self(since)
            except ModelError as e:
                weekly_ok = False
                report.errors.append(f"自我反思：{e}")
            try:
                report.preferences_reflected = self._reflect_preferences(since)
            except ModelError as e:
                weekly_ok = False
                report.errors.append(f"喜好审视：{e}")
            try:
                report.style_reflected = self._reflect_style(since)
            except ModelError as e:
                weekly_ok = False
                report.errors.append(f"表达风格审视：{e}")
            before_errors = len(report.errors)
            report.familiarity_updated = self._reflect_people_weekly(
                since, errors=report.errors
            )
            if len(report.errors) > before_errors:
                weekly_ok = False
            if weekly_ok:
                self.c.index.set_meta(LAST_IDENTITY, now.isoformat())

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
        report.threads_rotated = self.c.rotate_chat_sessions("sleep")
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
        canonical_id = person.person_id
        who = person.names[0] if person.names else canonical_id
        mine_eps = [
            e
            for e in episodes
            if any(self.c.people.canonical(p) == canonical_id for p in e.participants)
        ]
        mine_facts = list(self.c.index.current_facts(canonical_id))
        if canonical_id != pid:
            seen_fids = {f.id for f in mine_facts}
            mine_facts += [f for f in self.c.index.current_facts(pid) if f.id not in seen_fids]
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
        existing_map = parse_markdown_sections(person.body)
        updated = _validated(data, PERSON_SECTIONS, memories, person.body, min_sources=1)
        if not any(updated.values()) and any(existing_map.get(s) for s in PERSON_SECTIONS):
            return False
        merged: dict[str, list[str]] = {}
        for sec in ALL_PERSON_SECTIONS:
            if sec == PERSON_STYLE_SECTION:
                merged[sec] = existing_map.get(sec, [])
            else:
                # 若本节原有内容非空，而本次校验后整个小节被清空，保留原有条目防误抹除
                merged[sec] = updated.get(sec) or existing_map.get(sec, [])
        header = render_template("person.md", display_name=who).split("## ")[0].rstrip()
        person.body = header + "\n\n" + _render_sections(merged) + "\n"
        path = self.c.layout.person_path(canonical_id)
        path.write_text(dump_person(person), encoding="utf-8", newline="\n")
        self.c.people.reload()
        return True

    def _reflect_self(self, since: datetime) -> bool:
        layout = self.c.layout
        identity_path = layout.self_dir / "identity.md"
        current = identity_path.read_text("utf-8") if identity_path.exists() else ""
        seed = self._read_self("seed.md")
        memories = self._self_memories(since)
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
        new_text = header + "\n\n" + _render_sections(sections) + "\n"
        if new_text == current:
            return False
        identity_path.write_text(new_text, encoding="utf-8", newline="\n")
        return True

    def _reflect_preferences(self, since: datetime) -> bool:
        layout = self.c.layout
        pref_path = layout.self_dir / "preferences.md"
        current = pref_path.read_text("utf-8") if pref_path.exists() else ""
        seed = self._read_self("seed.md")
        memories = self._self_memories(since)
        if len(memories) < 2:
            return False
        data = self._ask(
            PREFERENCES_PROMPT.format(name=self.c.name, sections="、".join(PREFERENCE_SECTIONS)),
            f"【核心种子】\n{seed}\n\n【现有的喜好与习惯】\n{current or '（尚未填写）'}\n\n"
            "【这段时间的记忆】\n" + _list_memories(memories),
            _sections_schema(PREFERENCE_SECTIONS, optional_change=True),
        )
        if not data.get("changed"):
            return False
        existing_map = parse_markdown_sections(current)
        sections = _validated(data, PREFERENCE_SECTIONS, memories, current, min_sources=2)
        sections = _cap_section_changes(sections, existing_map, max_new=2, per_section_limit=6)
        if not any(sections.values()):
            return False
        header = render_template("preferences.md", name=self.c.name).split("## ")[0].rstrip()
        new_text = header + "\n\n" + _render_sections(sections) + "\n"
        if new_text == current:
            return False
        pref_path.write_text(new_text, encoding="utf-8", newline="\n")
        return True

    def _reflect_style(self, since: datetime) -> bool:
        layout = self.c.layout
        style_path = layout.self_dir / "style.md"
        current = style_path.read_text("utf-8") if style_path.exists() else ""
        seed = self._read_self("seed.md")
        memories = self._self_memories(since) + self._style_samples(since)
        if len(memories) < 2:
            return False
        data = self._ask(
            STYLE_PROMPT.format(name=self.c.name, sections="、".join(STYLE_SECTIONS)),
            f"【核心种子】\n{seed}\n\n【现有的表达风格】\n{current or '（尚未填写）'}\n\n"
            "【这段时间的记忆与实际发言片段】\n" + _list_memories(memories),
            _sections_schema(STYLE_SECTIONS, optional_change=True),
        )
        if not data.get("changed"):
            return False
        existing_map = parse_markdown_sections(current)
        sections = _validated(data, STYLE_SECTIONS, memories, current, min_sources=2)
        sections = _cap_section_changes(sections, existing_map, max_new=3, per_section_limit=5)
        if not any(sections.values()):
            return False
        header = render_template("style.md", name=self.c.name).split("## ")[0].rstrip()
        new_text = header + "\n\n" + _render_sections(sections) + "\n"
        if new_text == current:
            return False
        style_path.write_text(new_text, encoding="utf-8", newline="\n")
        return True

    def _reflect_people_weekly(
        self, since: datetime, *, errors: list[str] | None = None
    ) -> list[str]:
        episodes = self.c.index.episodes_created_since(since)
        facts = self.c.index.facts_created_since(since)
        touched = {f.subject for f in facts if f.subject != SELF_ID}
        for ep in episodes:
            touched |= {p for p in ep.participants if p != SELF_ID}
        if not touched:
            return []

        active_days = self._active_days_by_person()
        seed = self._read_self("seed.md")
        style = self._read_self("style.md")
        owners = set(self.c.config.qq.owner_accounts)
        peer_bots = set(self.c.config.qq.peer_bots)
        updated: list[str] = []

        for raw_pid in sorted(touched):
            person = self.c.people.get(raw_pid)
            if person is None or person.merged_into:
                continue
            pid = person.person_id
            if pid in updated:
                continue
            try:
                changed = self._reflect_one_person_weekly(
                    person,
                    since=since,
                    active_days=len(active_days.get(pid, ())),
                    owners=owners,
                    peer_bots=peer_bots,
                    seed=seed,
                    style=style,
                )
            except ModelError as e:
                log.warning("熟络度审视 %s 失败：%s", pid, e)
                if errors is not None:
                    errors.append(f"熟络度审视 {pid}：{e}")
                continue
            if changed:
                updated.append(pid)
        if updated:
            self.c.people.reload()
        return updated

    def _reflect_one_person_weekly(
        self,
        person: Person,
        *,
        since: datetime,
        active_days: int,
        owners: set[str],
        peer_bots: set[str],
        seed: str,
        style: str,
    ) -> bool:
        pid = person.person_id
        who = person.names[0] if person.names else pid
        all_eps = self.c.index.episodes_with(pid, limit=500)
        all_facts = self.c.index.current_facts(pid)
        recent_eps = [e for e in all_eps if e.started_at >= since] or all_eps[:8]
        memories = [(e.id, e.summary) for e in recent_eps[:20]] + [
            (f.id, f.statement) for f in all_facts[:20]
        ]
        if not memories:
            return False

        qq_accs = set(person.accounts.get("qq") or [])
        is_owner = bool(qq_accs & owners)
        is_peer_bot = bool(qq_accs & peer_bots)
        ceiling = familiarity_ceiling(
            active_days=max(active_days, 1 if all_eps else 0),
            episodes_count=len(all_eps),
            facts_count=len(all_facts),
            is_owner=is_owner,
            is_peer_bot=is_peer_bot,
        )
        stats_note = (
            f"互动天数：{active_days} 天；共同经历：{len(all_eps)} 段；已知事实：{len(all_facts)} 条；"
            f"当前熟络度：{person.familiarity}；客观允许上限：{ceiling}"
            + ("（主人）" if is_owner else "")
            + ("（同伴机器人）" if is_peer_bot else "")
        )
        data = self._ask(
            PERSON_STYLE_PROMPT.format(
                name=self.c.name,
                who=who,
                ceiling=ceiling,
                current_fam=person.familiarity,
            ),
            f"【核心种子】\n{seed}\n\n【你的表达风格】\n{style or '（暂无）'}\n\n"
            f"【互动统计】\n{stats_note}\n\n【现有档案】\n{person.body.strip()}\n\n"
            "【这段时间的共同记忆】\n" + _list_memories(memories),
            _person_style_schema(),
        )
        if not data.get("changed"):
            return False

        new_fam = clamp_familiarity(
            person.familiarity, str(data.get("familiarity") or person.familiarity), ceiling
        )
        existing_map = parse_markdown_sections(person.body)
        old_lines = existing_map.get(PERSON_STYLE_SECTION, [])
        validated = _validated(
            {PERSON_STYLE_SECTION: data.get(PERSON_STYLE_SECTION)},
            [PERSON_STYLE_SECTION],
            memories,
            person.body,
            min_sources=1,
        )[PERSON_STYLE_SECTION][:5]
        new_lines = validated if validated else old_lines

        if new_fam == person.familiarity and new_lines == old_lines:
            return False

        person.familiarity = new_fam
        merged = {sec: existing_map.get(sec, []) for sec in ALL_PERSON_SECTIONS}
        merged[PERSON_STYLE_SECTION] = new_lines
        header = render_template("person.md", display_name=who).split("## ")[0].rstrip()
        person.body = header + "\n\n" + _render_sections(merged) + "\n"
        self.c.layout.person_path(pid).write_text(
            dump_person(person), encoding="utf-8", newline="\n"
        )
        return True

    def _self_memories(self, since: datetime) -> list[tuple[str, str]]:
        episodes = self.c.index.episodes_created_since(since)
        facts = self.c.index.current_facts(SELF_ID)
        return [(e.id, e.summary) for e in episodes] + [(f.id, f.statement) for f in facts]

    def _style_samples(self, since: datetime) -> list[tuple[str, str]]:
        """取这段时间实际发生过的简短对话片段（含编号），让表达风格学习能看到真实措辞。"""
        mine: list[tuple[str, str]] = []
        others: list[tuple[str, str]] = []
        for e in self.c.log.iter_events(since=since):
            if e.kind != "message" or e.content.startswith("[") or e.meta.get("fiction"):
                continue
            text = " ".join(e.content.split())
            if not text or len(text) > 160:
                continue
            if e.actor.person_id == SELF_ID:
                mine.append((e.id, f"（我说过）{text}"))
            elif e.channel.startswith("group:"):
                who = e.actor.display_name or e.actor.platform_id
                others.append((e.id, f"（群聊·{who}）{text}"))
        return mine[-20:] + others[-12:]

    def _active_days_by_person(self) -> dict[str, set[str]]:
        days: dict[str, set[str]] = defaultdict(set)
        for e in self.c.log.iter_events():
            pid = e.actor.person_id
            if pid and pid != SELF_ID and e.kind == "message":
                days[self.c.people.canonical(pid)].add(e.ts.astimezone().date().isoformat())
        return days

    def _read_self(self, name: str) -> str:
        path = self.c.layout.self_dir / name
        return path.read_text("utf-8") if path.exists() else ""

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


def _cap_section_changes(
    sections: dict[str, list[str]],
    existing_map: dict[str, list[str]],
    *,
    max_new: int,
    per_section_limit: int,
) -> dict[str, list[str]]:
    """防漂移护栏：已有内容时限制单次新增/改写条数，且避免某节因出处校验全空而意外丢失。"""
    had_any = any(existing_map.values())
    existing_plain = {_strip_sources(x) for items in existing_map.values() for x in items}
    budget = max_new if had_any else max_new * 4
    used = 0
    out: dict[str, list[str]] = {}
    for name, lines in sections.items():
        kept: list[str] = []
        for line in lines:
            if _strip_sources(line) in existing_plain:
                kept.append(line)
            elif used < budget:
                kept.append(line)
                used += 1
        if not kept and existing_map.get(name):
            kept = list(existing_map[name])
        out[name] = kept[:per_section_limit]
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
    if report.preferences_reflected:
        bits.append("更新了喜好与习惯")
    if report.style_reflected:
        bits.append("更新了表达风格")
    if report.familiarity_updated:
        bits.append(f"校准了 {len(report.familiarity_updated)} 个人的熟络度与说话方式")
    if report.planned:
        bits.append("排好了今天的安排")
    return "：".join([bits[0], "，".join(bits[1:]) or "没有变化"])
