"""会话抽取：把一段结束的对话变成情景摘要和事实。

模型输出的每个 id（人物、出处事件、要取代的旧事实）都要校验，对不上的条目直接丢弃，
宁可少记，不可记错。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from companion.memory.events import Event
from companion.memory.index import Episode, Fact, MemoryIndex
from companion.memory.people import SELF_ID, PeopleRegistry
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage

log = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "episode": {
            "type": ["object", "null"],
            "properties": {
                "summary": {"type": "string"},
                "importance": {"type": "number"},
                "emotion": {"type": "number"},
            },
            "required": ["summary", "importance", "emotion"],
            "additionalProperties": False,
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "statement": {"type": "string"},
                    "confidence": {"type": "number"},
                    "importance": {"type": "number"},
                    "pin": {"type": "boolean"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "supersedes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "subject",
                    "predicate",
                    "statement",
                    "confidence",
                    "importance",
                    "pin",
                    "sources",
                    "supersedes",
                ],  # fmt: skip
                "additionalProperties": False,
            },
        },
    },
    "required": ["episode", "facts"],
    "additionalProperties": False,
}

INSTRUCTIONS = """你在帮 {name} 整理记忆。下面是一段刚结束的对话，请输出 json。

episode：用第三人称、一两句话概括这段经历（谁、做了什么、聊了什么、气氛如何），
  像日记一样写给将来的 {name} 看；纯寒暄、没有内容的对话填 null。
  importance 0~1：这段经历对 {name} 和对方关系的重要程度；emotion 0~1：情绪强度。
facts：对话中明确出现的、值得长期记住的事实（偏好、经历、计划、身份、人际关系、约定）。
  - subject 只能是参与者表里的 id（{name} 自己用 "self"）。
  - predicate 用英文小写加下划线的主题键，如 coffee_preference、pet、travel_plan；
    同一主题的新事实请沿用已有事实的 predicate。
  - statement 是一句完整的中文陈述，主语写名字，如“小明喜欢不加糖、加奶的咖啡”。
  - sources 填依据的消息编号（如 "E3"）；没有明确依据的不要写。
  - 如果新事实推翻或更新了“已知事实”里的某条，把那条的 id 写进 supersedes。
  - 对方明确要求“记住”的、承诺约定、过敏等安全信息，pin 设为 true。
  - 不要把玩笑、假设、反问当成事实；不确定就降低 confidence 或不写。
  - 不知道对方性别时，不要用“他”“她”，用名字或“ta”。
  - 不要记录名字本身（如“祥的名字是祥”），名字已经在参与者表里。

json 格式示例：
{{"episode": {{"summary": "...", "importance": 0.4, "emotion": 0.3}},
  "facts": [{{"subject": "p1", "predicate": "coffee_preference", "statement": "...",
             "confidence": 0.9, "importance": 0.5, "pin": false, "sources": ["E2"],
             "supersedes": []}}]}}
"""


@dataclass
class Extraction:
    episode: Episode | None
    facts: list[tuple[Fact, list[str]]] = field(default_factory=list)  # (事实, 要取代的旧事实 id)
    dropped: list[str] = field(default_factory=list)  # 被校验丢弃的条目说明


class SessionExtractor:
    def __init__(
        self,
        *,
        persona_name: str,
        gateway: ModelGateway,
        index: MemoryIndex,
        people: PeopleRegistry,
    ) -> None:
        self.name = persona_name
        self.gateway = gateway
        self.index = index
        self.people = people

    def extract(self, events: list[Event]) -> Extraction:
        if not events:
            return Extraction(None)
        refs = {f"E{i}": e for i, e in enumerate(events, 1)}
        participants: dict[str, str] = {SELF_ID: self.name}
        for e in events:
            pid = e.actor.person_id
            if pid and pid != SELF_ID:
                pid = self.people.canonical(pid)
                person = self.people.get(pid)
                participants[pid] = (
                    (person.names[0] if person and person.names else None)
                    or e.actor.display_name
                    or pid
                )
        known: dict[str, Fact] = {}
        for pid in participants:
            for f in self.index.current_facts(pid):
                known[f.id] = f

        transcript = "\n".join(
            f"[{ref}] {e.ts.astimezone():%m-%d %H:%M} "
            f"{participants.get(self._pid(e), e.actor.display_name or '?')}：{e.content}"
            for ref, e in refs.items()
        )
        user = "\n\n".join(
            [
                "参与者（id：名字）：\n"
                + "\n".join(f"- {pid}：{name}" for pid, name in participants.items()),
                "已知事实（id：内容）：\n"
                + (
                    "\n".join(
                        f"- {fid}：{f.statement}（predicate={f.predicate}）"
                        for fid, f in known.items()
                    )
                    or "（无）"
                ),
                "对话：\n" + transcript,
            ]
        )
        data, _ = self.gateway.chat_json(
            [
                ChatMessage("system", INSTRUCTIONS.format(name=self.name)),
                ChatMessage("user", user),
            ],
            SCHEMA,
            purpose="extract",
        )
        return self._validate(data, events, refs, participants, known)

    def _pid(self, e: Event) -> str:
        pid = e.actor.person_id or ""
        return pid if pid == SELF_ID else self.people.canonical(pid)

    def _validate(
        self,
        data: dict[str, Any],
        events: list[Event],
        refs: dict[str, Event],
        participants: dict[str, str],
        known: dict[str, Fact],
    ) -> Extraction:
        out = Extraction(None)
        ep = data.get("episode")
        if isinstance(ep, dict) and str(ep.get("summary", "")).strip():
            out.episode = Episode(
                started_at=events[0].ts,
                ended_at=events[-1].ts,
                body=events[0].body,
                channel=events[0].channel,
                summary=str(ep["summary"]).strip(),
                participants=list(participants),
                event_ids=[e.id for e in events],
                importance=_unit(ep.get("importance"), 0.5),
                emotion=_unit(ep.get("emotion"), 0.0),
            )
        superseded: set[str] = set()
        for raw in data.get("facts") or []:
            if not isinstance(raw, dict):
                continue
            statement = str(raw.get("statement", "")).strip()
            subject = str(raw.get("subject", ""))
            sources = [refs[s].id for s in raw.get("sources") or [] if s in refs]
            if not statement or subject not in participants:
                out.dropped.append(f"主语不在参与者中：{subject} {statement}")
                continue
            if not sources:
                out.dropped.append(f"没有出处：{statement}")
                continue
            supersedes = []
            for fid in raw.get("supersedes") or []:
                old = known.get(fid)
                if old is None or old.subject != subject or fid in superseded:
                    out.dropped.append(f"无效的 supersedes：{fid}")
                    continue
                supersedes.append(fid)
                superseded.add(fid)
            source_events = [e for e in events if e.id in sources]
            fact = Fact(
                subject=subject,
                predicate=_predicate(raw.get("predicate")),
                statement=statement,
                valid_from=source_events[-1].ts,
                event_ids=sources,
                confidence=_unit(raw.get("confidence"), 0.7),
                importance=_unit(raw.get("importance"), 0.5),
                pinned=bool(raw.get("pin")),
            )
            out.facts.append((fact, supersedes))
        for note in out.dropped:
            log.info("抽取时丢弃：%s", note)
        return out


def split_sessions(events: list[Event], idle: timedelta) -> list[list[Event]]:
    """同一频道内，相邻消息间隔超过 idle 就切成新的一段会话。"""
    sessions: list[list[Event]] = []
    for e in events:
        if sessions and e.ts - sessions[-1][-1].ts <= idle:
            sessions[-1].append(e)
        else:
            sessions.append([e])
    return sessions


def ended(session: list[Event], idle: timedelta, now: datetime) -> bool:
    return now - session[-1].ts > idle


def _unit(value: Any, default: float) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


def _predicate(value: Any) -> str:
    text = "".join(c if c.isalnum() else "_" for c in str(value or "misc").lower()).strip("_")
    return text or "misc"
