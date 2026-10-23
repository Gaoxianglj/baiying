"""SQLite 派生层：情景、带有效期的事实、记忆强度、全文索引。

情景和事实由抽取模型从事件日志生成，可以随时删库重建（`reset`）。
注意：记忆强度（稳定度、回忆次数）只存在这里，重建后回到初始值。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from companion.memory import forgetting
from companion.memory.layout import MemoryLayout
from companion.memory.textsearch import index_text, match_query

SCHEMA_VERSION = 1

MemoryKind = Literal["episode", "fact"]
EpisodeState = Literal["vivid", "faded", "compressed", "dormant"]

_SCHEMA = """
CREATE TABLE episodes (
    id               TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    body             TEXT NOT NULL,
    channel          TEXT NOT NULL,
    summary          TEXT NOT NULL,
    event_ids        TEXT NOT NULL,             -- JSON 数组：出处
    importance       REAL NOT NULL,
    emotion          REAL NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('vivid', 'faded', 'compressed', 'dormant')),
    stability_days   REAL NOT NULL,
    last_recalled_at TEXT NOT NULL,
    recall_count     INTEGER NOT NULL DEFAULT 0,
    pinned           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX episodes_time ON episodes (started_at);

CREATE TABLE episode_people (
    episode_id TEXT NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    person_id  TEXT NOT NULL,
    PRIMARY KEY (episode_id, person_id)
);
CREATE INDEX episode_people_person ON episode_people (person_id);

CREATE TABLE facts (
    id               TEXT PRIMARY KEY,
    subject          TEXT NOT NULL,             -- person_id / self / world
    predicate        TEXT NOT NULL,             -- 如 coffee_preference
    statement        TEXT NOT NULL,             -- 完整的一句话
    valid_from       TEXT NOT NULL,
    valid_to         TEXT,                      -- NULL 表示仍然有效
    invalidated_by   TEXT REFERENCES facts (id),
    event_ids        TEXT NOT NULL,             -- JSON 数组：出处
    confidence       REAL NOT NULL,
    importance       REAL NOT NULL,
    stability_days   REAL NOT NULL,
    last_recalled_at TEXT NOT NULL,
    recall_count     INTEGER NOT NULL DEFAULT 0,
    pinned           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX facts_current ON facts (subject, predicate) WHERE valid_to IS NULL;

CREATE VIRTUAL TABLE memory_fts USING fts5 (tokens, kind UNINDEXED, memory_id UNINDEXED);

CREATE TABLE vectors (
    kind      TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    model     TEXT NOT NULL,                    -- 由哪个 embedding 模型算出；换模型即重算
    dim       INTEGER NOT NULL,
    vec       BLOB NOT NULL,                    -- float32，已归一化
    PRIMARY KEY (kind, memory_id)
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class MemoryIndexError(RuntimeError):
    pass


class Episode(BaseModel):
    """一段经历的摘要。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    body: str
    channel: str
    summary: str
    participants: list[str] = Field(default_factory=list)  # person_id
    event_ids: list[str] = Field(default_factory=list)
    importance: float = Field(0.5, ge=0, le=1)
    emotion: float = Field(0.0, ge=0, le=1)
    state: EpisodeState = "vivid"
    pinned: bool = False


class Fact(BaseModel):
    """关于人或世界的事实，带有效期和出处。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    subject: str
    predicate: str
    statement: str
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    invalidated_by: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(0.8, ge=0, le=1)
    importance: float = Field(0.5, ge=0, le=1)
    pinned: bool = False


@dataclass(frozen=True)
class TextHit:
    kind: MemoryKind
    memory_id: str
    score: float  # bm25 取反：越大越相关


@dataclass(frozen=True)
class Strength:
    stability_days: float
    last_recalled_at: datetime
    recall_count: int
    pinned: bool

    def retrievability(self, now: datetime) -> float:
        if self.pinned:
            return forgetting.PINNED_RETRIEVABILITY
        return forgetting.retrievability(self.stability_days, self.last_recalled_at, now)


class MemoryIndex:
    def __init__(self, layout: MemoryLayout) -> None:
        self._path = layout.index_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._ensure_schema()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> MemoryIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 结构 ----

    def _ensure_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self._tx() as c:
                c.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
        elif version != SCHEMA_VERSION:
            raise MemoryIndexError(
                f"index.sqlite 结构版本 {version} 与程序的 {SCHEMA_VERSION} 不一致；"
                "它是派生层，可删除后重建"
            )

    def reset(self) -> None:
        """清空派生层，准备从事件日志重建。"""
        with self._tx() as c:
            for table in ("memory_fts", "vectors", "meta", "episode_people", "facts", "episodes"):
                c.execute(f"DELETE FROM {table}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # ---- 情景 ----

    def add_episode(self, ep: Episode, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        with self._tx() as c:
            c.execute(
                "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    ep.id,
                    _iso(ep.started_at),
                    _iso(ep.ended_at),
                    ep.body,
                    ep.channel,
                    ep.summary,
                    json.dumps(ep.event_ids),
                    ep.importance,
                    ep.emotion,
                    ep.state,
                    forgetting.initial_stability(ep.importance, ep.emotion),
                    _iso(ep.ended_at),  # 刚发生的事，从结束时刻开始遗忘
                    int(ep.pinned),
                    _iso(now),
                ),
            )
            c.executemany(
                "INSERT INTO episode_people VALUES (?, ?)",
                [(ep.id, pid) for pid in dict.fromkeys(ep.participants)],
            )
            self._index_text(c, "episode", ep.id, ep.summary)

    def get_episode(self, episode_id: str) -> Episode | None:
        row = self._conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return None if row is None else self._episode(row)

    def episodes_with(self, person_id: str, limit: int = 20) -> list[Episode]:
        """某人参与过的情景，新的在前。"""
        rows = self._conn.execute(
            "SELECT e.* FROM episodes e JOIN episode_people p ON p.episode_id = e.id "
            "WHERE p.person_id = ? ORDER BY e.started_at DESC LIMIT ?",
            (person_id, limit),
        ).fetchall()
        return [self._episode(r) for r in rows]

    def set_episode_state(self, episode_id: str, state: EpisodeState) -> None:
        with self._tx() as c:
            c.execute("UPDATE episodes SET state = ? WHERE id = ?", (state, episode_id))

    def _episode(self, row: sqlite3.Row) -> Episode:
        people = [
            r[0]
            for r in self._conn.execute(
                "SELECT person_id FROM episode_people WHERE episode_id = ? ORDER BY rowid",
                (row["id"],),
            )
        ]
        return Episode(
            id=row["id"],
            started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            body=row["body"],
            channel=row["channel"],
            summary=row["summary"],
            participants=people,
            event_ids=json.loads(row["event_ids"]),
            importance=row["importance"],
            emotion=row["emotion"],
            state=row["state"],
            pinned=bool(row["pinned"]),
        )

    # ---- 事实 ----

    def add_fact(
        self, fact: Fact, *, supersedes: list[str] | None = None, now: datetime | None = None
    ) -> None:
        """新增事实。`supersedes` 中的旧事实标记为失效（不删除），失效时间为新事实的生效时间。"""
        now = now or datetime.now(UTC)
        with self._tx() as c:
            c.execute(
                "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    fact.id,
                    fact.subject,
                    fact.predicate,
                    fact.statement,
                    _iso(fact.valid_from),
                    _iso(fact.valid_to) if fact.valid_to else None,
                    fact.invalidated_by,
                    json.dumps(fact.event_ids),
                    fact.confidence,
                    fact.importance,
                    forgetting.initial_stability(fact.importance, 0.0),
                    _iso(fact.valid_from),
                    int(fact.pinned),
                    _iso(now),
                ),
            )
            for old_id in supersedes or []:
                updated = c.execute(
                    "UPDATE facts SET valid_to = ?, invalidated_by = ? "
                    "WHERE id = ? AND valid_to IS NULL",
                    (_iso(fact.valid_from), fact.id, old_id),
                ).rowcount
                if not updated:
                    raise MemoryIndexError(f"要取代的事实不存在或已失效：{old_id}")
            self._index_text(c, "fact", fact.id, fact.statement)

    def get_fact(self, fact_id: str) -> Fact | None:
        row = self._conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return None if row is None else _fact(row)

    def current_facts(self, subject: str, predicate: str | None = None) -> list[Fact]:
        sql = "SELECT * FROM facts WHERE subject = ? AND valid_to IS NULL"
        args: list[str] = [subject]
        if predicate is not None:
            sql += " AND predicate = ?"
            args.append(predicate)
        rows = self._conn.execute(sql + " ORDER BY valid_from", args).fetchall()
        return [_fact(r) for r in rows]

    def fact_history(self, subject: str, predicate: str) -> list[Fact]:
        """同一主题的全部事实（含已失效的），旧的在前：用来说出“你以前……后来……”。"""
        rows = self._conn.execute(
            "SELECT * FROM facts WHERE subject = ? AND predicate = ? ORDER BY valid_from",
            (subject, predicate),
        ).fetchall()
        return [_fact(r) for r in rows]

    # ---- 强度 ----

    def strength(self, kind: MemoryKind, memory_id: str) -> Strength:
        row = self._conn.execute(
            f"SELECT stability_days, last_recalled_at, recall_count, pinned "
            f"FROM {_table(kind)} WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return Strength(row[0], _dt(row[1]), row[2], bool(row[3]))

    def mark_recalled(self, kind: MemoryKind, memory_id: str, now: datetime | None = None) -> None:
        """被想起一次：稳定度按重要度增长，遗忘时钟从现在重新开始。"""
        now = now or datetime.now(UTC)
        table = _table(kind)
        with self._tx() as c:
            row = c.execute(
                f"SELECT stability_days, importance FROM {table} WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            c.execute(
                f"UPDATE {table} SET stability_days = ?, last_recalled_at = ?, "
                f"recall_count = recall_count + 1 WHERE id = ?",
                (forgetting.stability_after_recall(row[0], row[1]), _iso(now), memory_id),
            )

    def set_pinned(self, kind: MemoryKind, memory_id: str, pinned: bool = True) -> None:
        with self._tx() as c:
            c.execute(
                f"UPDATE {_table(kind)} SET pinned = ? WHERE id = ?", (int(pinned), memory_id)
            )

    # ---- 全文检索 ----

    def search_text(
        self, query: str, *, kinds: tuple[MemoryKind, ...] = ("episode", "fact"), limit: int = 20
    ) -> list[TextHit]:
        """关键词检索。只返回仍有效的事实和未沉睡的情景。"""
        expr = match_query(query)
        if expr is None or not kinds:
            return []
        for kind in kinds:
            _table(kind)  # 校验
        placeholders = ",".join("?" * len(kinds))
        rows = self._conn.execute(
            "SELECT f.kind, f.memory_id, -bm25(memory_fts) AS score FROM memory_fts f "
            "LEFT JOIN episodes e ON f.kind = 'episode' AND e.id = f.memory_id "
            "LEFT JOIN facts t ON f.kind = 'fact' AND t.id = f.memory_id "
            f"WHERE memory_fts MATCH ? AND f.kind IN ({placeholders}) "
            "AND ((f.kind = 'episode' AND e.state != 'dormant') "
            "  OR (f.kind = 'fact' AND t.valid_to IS NULL)) "
            "ORDER BY score DESC LIMIT ?",
            (expr, *kinds, limit),
        ).fetchall()
        return [TextHit(r[0], r[1], r[2]) for r in rows]

    # ---- 向量 ----

    def texts_needing_vectors(self, model: str) -> list[tuple[MemoryKind, str, str]]:
        """还没有用 `model` 算过向量的记忆：(kind, id, 文本)。"""
        rows = self._conn.execute(
            "SELECT 'episode', e.id, e.summary FROM episodes e "
            "LEFT JOIN vectors v ON v.kind = 'episode' AND v.memory_id = e.id "
            "WHERE v.model IS NULL OR v.model != ? "
            "UNION ALL "
            "SELECT 'fact', f.id, f.statement FROM facts f "
            "LEFT JOIN vectors v ON v.kind = 'fact' AND v.memory_id = f.id "
            "WHERE v.model IS NULL OR v.model != ?",
            (model, model),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def put_vectors(
        self, model: str, items: list[tuple[MemoryKind, str]], matrix: np.ndarray
    ) -> None:
        if len(items) != len(matrix):
            raise ValueError("向量数与记忆数不一致")
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?, ?)",
                [
                    (kind, mid, model, int(vec.shape[0]), np.asarray(vec, np.float32).tobytes())
                    for (kind, mid), vec in zip(items, matrix, strict=True)
                ],
            )

    def load_vectors(
        self, model: str, allowed: set[tuple[MemoryKind, str]] | None = None
    ) -> tuple[list[tuple[MemoryKind, str]], np.ndarray]:
        """可检索记忆（有效事实、未沉睡情景）的向量。`allowed` 进一步限定范围。"""
        rows = self._conn.execute(
            "SELECT v.kind, v.memory_id, v.vec FROM vectors v "
            "LEFT JOIN episodes e ON v.kind = 'episode' AND e.id = v.memory_id "
            "LEFT JOIN facts f ON v.kind = 'fact' AND f.id = v.memory_id "
            "WHERE v.model = ? AND ((v.kind = 'episode' AND e.state != 'dormant') "
            "  OR (v.kind = 'fact' AND f.valid_to IS NULL))",
            (model,),
        ).fetchall()
        keys: list[tuple[MemoryKind, str]] = []
        vecs: list[np.ndarray] = []
        for kind, mid, blob in rows:
            if allowed is None or (kind, mid) in allowed:
                keys.append((kind, mid))
                vecs.append(np.frombuffer(blob, dtype=np.float32))
        return keys, (np.vstack(vecs) if vecs else np.zeros((0, 0), np.float32))

    def allowed_keys(
        self,
        *,
        person_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> set[tuple[MemoryKind, str]]:
        """按人物和时间过滤出的记忆。情景看参与者与开始时间，事实看主语与生效时间。"""
        ep_sql = "SELECT DISTINCT e.id FROM episodes e"
        ep_where: list[str] = []
        fact_where: list[str] = []
        ep_args: list[str] = []
        fact_args: list[str] = []
        if person_id is not None:
            ep_sql += " JOIN episode_people p ON p.episode_id = e.id"
            ep_where.append("p.person_id = ?")
            ep_args.append(person_id)
            fact_where.append("subject = ?")
            fact_args.append(person_id)
        if since is not None:
            ep_where.append("e.started_at >= ?")
            ep_args.append(_iso(since))
            fact_where.append("valid_from >= ?")
            fact_args.append(_iso(since))
        if until is not None:
            ep_where.append("e.started_at < ?")
            ep_args.append(_iso(until))
            fact_where.append("valid_from < ?")
            fact_args.append(_iso(until))
        if ep_where:
            ep_sql += " WHERE " + " AND ".join(ep_where)
        fact_sql = "SELECT id FROM facts" + (
            " WHERE " + " AND ".join(fact_where) if fact_where else ""
        )
        keys: set[tuple[MemoryKind, str]] = {
            ("episode", r[0]) for r in self._conn.execute(ep_sql, ep_args)
        }
        keys |= {("fact", r[0]) for r in self._conn.execute(fact_sql, fact_args)}
        return keys

    def importance(self, kind: MemoryKind, memory_id: str) -> float:
        row = self._conn.execute(
            f"SELECT importance FROM {_table(kind)} WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return float(row[0])

    def text_of(self, kind: MemoryKind, memory_id: str) -> str:
        column = "summary" if kind == "episode" else "statement"
        row = self._conn.execute(
            f"SELECT {column} FROM {_table(kind)} WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return str(row[0])

    def delete(self, kind: MemoryKind, memory_id: str) -> list[str]:
        """彻底删除一条记忆（含向量与全文索引），返回它的出处事件 id。"""
        table = _table(kind)
        with self._tx() as c:
            row = c.execute(f"SELECT event_ids FROM {table} WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(memory_id)
            if kind == "fact":
                # 被它取代的旧事实恢复为“仍有效”之外的状态没有意义；只断开引用
                c.execute(
                    "UPDATE facts SET invalidated_by = NULL WHERE invalidated_by = ?", (memory_id,)
                )
            c.execute(f"DELETE FROM {table} WHERE id = ?", (memory_id,))
            c.execute("DELETE FROM vectors WHERE kind = ? AND memory_id = ?", (kind, memory_id))
            c.execute("DELETE FROM memory_fts WHERE kind = ? AND memory_id = ?", (kind, memory_id))
        return list(json.loads(row[0]))

    def memories_from_events(self, event_ids: set[str]) -> list[tuple[MemoryKind, str]]:
        """出处里含有这些事件的记忆。"""
        out: list[tuple[MemoryKind, str]] = []
        for kind in ("episode", "fact"):
            for mid, raw in self._conn.execute(f"SELECT id, event_ids FROM {_table(kind)}"):
                if event_ids & set(json.loads(raw)):
                    out.append((kind, mid))  # type: ignore[arg-type]
        return out

    def episodes_created_since(self, since: datetime) -> list[Episode]:
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE created_at >= ? ORDER BY started_at", (_iso(since),)
        ).fetchall()
        return [self._episode(r) for r in rows]

    def facts_created_since(self, since: datetime) -> list[Fact]:
        rows = self._conn.execute(
            "SELECT * FROM facts WHERE created_at >= ? ORDER BY valid_from", (_iso(since),)
        ).fetchall()
        return [_fact(r) for r in rows]

    def episode_ids(self, states: tuple[EpisodeState, ...]) -> list[str]:
        marks = ",".join("?" * len(states))
        rows = self._conn.execute(f"SELECT id FROM episodes WHERE state IN ({marks})", states)
        return [r[0] for r in rows]

    def supersede(self, old_id: str, new_id: str, at: datetime) -> None:
        """把仍有效的旧事实标记为被新事实取代（合并重复事实时用）。"""
        with self._tx() as c:
            updated = c.execute(
                "UPDATE facts SET valid_to = ?, invalidated_by = ? "
                "WHERE id = ? AND valid_to IS NULL",
                (_iso(at), new_id, old_id),
            ).rowcount
        if not updated:
            raise MemoryIndexError(f"要取代的事实不存在或已失效：{old_id}")

    def counts(self) -> dict[str, int]:
        c = self._conn
        return {
            "episodes": c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "facts": c.execute("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL").fetchone()[0],
            "superseded_facts": c.execute(
                "SELECT COUNT(*) FROM facts WHERE valid_to IS NOT NULL"
            ).fetchone()[0],
        }

    # ---- 元数据（如抽取进度）----

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    @staticmethod
    def _index_text(c: sqlite3.Connection, kind: MemoryKind, memory_id: str, text: str) -> None:
        c.execute(
            "INSERT INTO memory_fts (tokens, kind, memory_id) VALUES (?, ?, ?)",
            (index_text(text), kind, memory_id),
        )


def _table(kind: MemoryKind) -> str:
    if kind == "episode":
        return "episodes"
    if kind == "fact":
        return "facts"
    raise ValueError(kind)


def _fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        subject=row["subject"],
        predicate=row["predicate"],
        statement=row["statement"],
        valid_from=_dt(row["valid_from"]),
        valid_to=_dt(row["valid_to"]) if row["valid_to"] else None,
        invalidated_by=row["invalidated_by"],
        event_ids=json.loads(row["event_ids"]),
        confidence=row["confidence"],
        importance=row["importance"],
        pinned=bool(row["pinned"]),
    )


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)
