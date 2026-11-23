from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, body TEXT, channel TEXT,
 name TEXT, status TEXT, kp TEXT, version INTEGER, state TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_campaign ON campaigns(body,channel)
 WHERE status='active';
CREATE TABLE IF NOT EXISTS checks(id TEXT PRIMARY KEY,campaign TEXT,actor TEXT,
 status TEXT,spec TEXT,result TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS rolls(id TEXT PRIMARY KEY,campaign TEXT,check_id TEXT UNIQUE,
 data TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,campaign TEXT,
 actor TEXT,kind TEXT,visibility TEXT,text TEXT,data TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS snapshots(campaign TEXT,version INTEGER,state TEXT,
 PRIMARY KEY(campaign,version));
CREATE TABLE IF NOT EXISTS receipts(scope TEXT,key TEXT,request TEXT,response TEXT,
 PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,campaign TEXT,visibility TEXT,
 owner TEXT,name TEXT,sha TEXT,page INTEGER,text TEXT,source TEXT);
CREATE TABLE IF NOT EXISTS narratives(key TEXT PRIMARY KEY,campaign TEXT,text TEXT);
CREATE TABLE IF NOT EXISTS metrics(id TEXT PRIMARY KEY,campaign TEXT,day TEXT,
 calls INTEGER,input_chars INTEGER,input_tokens INTEGER,output_tokens INTEGER,
 cached_tokens INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,campaign TEXT,body TEXT,
 channel TEXT,text TEXT,state TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS host_requests(scope TEXT,key TEXT,request TEXT,version INTEGER,
 plan TEXT,response TEXT,PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS host_prompts(campaign TEXT,person TEXT,kind TEXT,
 version INTEGER,spec TEXT,PRIMARY KEY(campaign,person));
"""


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def uid():
    return uuid4().hex[:16]


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self, *, write=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("跑团数据库版本不兼容，拒绝覆盖")
            conn.executescript(SCHEMA)
            if version < 2:
                conn.execute("PRAGMA user_version=2")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    @staticmethod
    def event(conn, cid, actor, kind, text, data=None, visibility="public"):
        conn.execute(
            "INSERT INTO events(campaign,actor,kind,visibility,text,data,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (cid, actor, kind, visibility, text, dump(data), now()),
        )

    @staticmethod
    def save(conn, campaign):
        cid = campaign["id"]
        previous = conn.execute("SELECT state FROM campaigns WHERE id=?", (cid,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO snapshots VALUES(?,?,?)",
            (cid, campaign["version"], previous["state"] if previous else dump(campaign["state"])),
        )
        campaign["version"] += 1
        conn.execute(
            "UPDATE campaigns SET name=?,status=?,version=?,state=? WHERE id=?",
            (
                campaign["name"],
                campaign["status"],
                campaign["version"],
                dump(campaign["state"]),
                cid,
            ),
        )

    @staticmethod
    def load(row):
        return {**dict(row), "state": json.loads(row["state"])}

    def listing(self):
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id,body,channel,name,status,version FROM campaigns ORDER BY rowid DESC"
                )
            ]

    def metrics(self, cid=None):
        with self.connect() as conn:
            where, params = ("WHERE campaign=?", (cid,)) if cid else ("", ())
            rows = conn.execute(
                "SELECT day,SUM(calls) calls,SUM(input_chars) input_chars,"
                "SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,"
                "SUM(cached_tokens) cached_tokens FROM metrics "
                + where
                + " GROUP BY day ORDER BY day DESC LIMIT 30",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
