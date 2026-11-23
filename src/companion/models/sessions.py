"""聊天线程索引与消息去重；不存提示词、不替代原始事件及长期记忆。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class ChatSessions:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                policy TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS seen (
                key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
                event_id TEXT NOT NULL, PRIMARY KEY (key, event_id)
            );
            CREATE TABLE IF NOT EXISTS retired (
                thread_id TEXT NOT NULL, reason TEXT NOT NULL,
                retired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def get(self, key: str) -> tuple[str, str, bool] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT thread_id, policy, pending FROM sessions WHERE key = ?", (key,)
            ).fetchone()
            return (row[0], row[1], bool(row[2])) if row else None

    def put(self, key: str, thread_id: str, policy: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions(key, thread_id, policy) VALUES (?, ?, ?)",
                (key, thread_id, policy),
            )

    def unseen(self, key: str, event_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM seen WHERE key = ? AND event_id = ?", (key, event_id)
            ).fetchone() is None

    def pending(self, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE sessions SET pending = 1 WHERE key = ?", (key,))

    def complete(self, key: str, thread_id: str, event_ids: list[str]) -> None:
        with self._lock, self._db:
            row = self.get(key)
            if row is None or row[0] != thread_id:
                return
            self._db.executemany(
                "INSERT OR IGNORE INTO seen(key, event_id) VALUES (?, ?)",
                [(key, eid) for eid in event_ids],
            )
            self._db.execute("UPDATE sessions SET pending = 0 WHERE key = ?", (key,))

    def retire(self, reason: str, key: str | None = None) -> list[str]:
        with self._lock, self._db:
            where, params = (" WHERE key = ?", (key,)) if key is not None else ("", ())
            ids = [r[0] for r in self._db.execute(
                "SELECT thread_id FROM sessions" + where, params
            )]
            self._db.executemany(
                "INSERT INTO retired(thread_id, reason) VALUES (?, ?)",
                [(tid, reason) for tid in ids],
            )
            self._db.execute("DELETE FROM sessions" + where, params)
            return ids

    def close(self) -> None:
        with self._lock:
            self._db.close()
