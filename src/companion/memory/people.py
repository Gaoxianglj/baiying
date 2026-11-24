"""人物表与跨平台身份绑定。

每个人一个 `people/<person_id>.md`：YAML frontmatter 记录名字和各平台账号，正文是对 ta 的了解。
陌生账号第一次出现时自动成为一个新的人；把账号绑到已有的人（`bind`）是一次显式确认。
被绑走全部账号的旧人物记为 `merged_into`，这样旧事件里的 person_id 仍能解析到同一个人。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import yaml

from companion.memory.layout import MemoryLayout, render_template

SELF_ID = "self"  # 伙伴自己
PERSON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_MERGE_DEPTH = 32
FAMILIARITY_LEVELS: tuple[str, ...] = ("初识", "相熟", "熟络", "亲近")
DEFAULT_FAMILIARITY = "初识"


def familiarity_rank(level: str) -> int:
    return FAMILIARITY_LEVELS.index(level) if level in FAMILIARITY_LEVELS else 0


class PeopleError(ValueError):
    pass


@dataclass
class Person:
    person_id: str
    names: list[str] = field(default_factory=list)
    accounts: dict[str, list[str]] = field(default_factory=dict)
    familiarity: str = DEFAULT_FAMILIARITY
    merged_into: str | None = None
    created_at: str = ""
    body: str = ""

    def frontmatter(self) -> dict[str, Any]:
        fam = self.familiarity if self.familiarity in FAMILIARITY_LEVELS else DEFAULT_FAMILIARITY
        data: dict[str, Any] = {
            "person_id": self.person_id,
            "names": self.names,
            "accounts": self.accounts,
            "familiarity": fam,
            "created_at": self.created_at,
        }
        if self.merged_into:
            data["merged_into"] = self.merged_into
        return data


def parse_person(text: str) -> Person:
    if not text.startswith("---\n"):
        raise PeopleError("人物档案缺少 frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise PeopleError("人物档案的 frontmatter 没有结束")
    meta = yaml.safe_load(text[4:end]) or {}
    raw_fam = str(meta.get("familiarity") or DEFAULT_FAMILIARITY)
    fam = raw_fam if raw_fam in FAMILIARITY_LEVELS else DEFAULT_FAMILIARITY
    return Person(
        person_id=str(meta["person_id"]),
        names=[str(n) for n in meta.get("names") or []],
        accounts={
            str(p): [str(a) for a in accs or []] for p, accs in (meta.get("accounts") or {}).items()
        },
        familiarity=fam,
        merged_into=meta.get("merged_into"),
        created_at=str(meta.get("created_at") or ""),
        body=text[end + 5 :],
    )


def dump_person(person: Person) -> str:
    meta = yaml.safe_dump(person.frontmatter(), allow_unicode=True, sort_keys=False)
    return f"---\n{meta}---\n{person.body}"


class PeopleRegistry:
    def __init__(self, layout: MemoryLayout) -> None:
        self._layout = layout
        self._lock = threading.RLock()
        self._people: dict[str, Person] = {}
        self._index: dict[tuple[str, str], str] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._people.clear()
            for path in sorted(self._layout.people_dir.glob("*.md")):
                person = parse_person(path.read_text("utf-8"))
                if person.person_id != path.stem:
                    raise PeopleError(f"{path.name} 里的 person_id 与文件名不一致")
                self._people[person.person_id] = person
            self._rebuild_index()

    def get(self, person_id: str) -> Person | None:
        return self._people.get(self.canonical(person_id))

    def list(self) -> list[Person]:
        return [p for p in self._people.values() if not p.merged_into]

    def canonical(self, person_id: str) -> str:
        """沿 merged_into 找到当前的人。"""
        for _ in range(_MAX_MERGE_DEPTH):
            person = self._people.get(person_id)
            if person is None or not person.merged_into:
                return person_id
            person_id = person.merged_into
        raise PeopleError(f"merged_into 出现循环：{person_id}")

    def resolve(self, platform: str, account: str) -> str | None:
        return self._index.get((platform, account))

    def ensure(self, platform: str, account: str, display_name: str | None = None) -> str:
        """返回账号对应的人；陌生账号自动建档。见到新昵称时记进 names。"""
        with self._lock:
            person_id = self.resolve(platform, account)
            if person_id is None:
                person = self._new_person(None, display_name or account)
                person.accounts[platform] = [account]
                self._save(person)
                self._rebuild_index()
                return person.person_id
            person = self._people[person_id]
            if display_name and display_name not in person.names:
                person.names.append(display_name)
                self._save(person)
            return person_id

    def create(self, person_id: str, name: str) -> Person:
        with self._lock:
            person = self._new_person(person_id, name)
            self._save(person)
            return person

    def bind(self, platform: str, account: str, person_id: str) -> None:
        """把账号绑到某人（显式确认）。原持有者若因此没有任何账号，就并入目标。"""
        with self._lock:
            target = self._people.get(person_id)
            if target is None or target.merged_into:
                raise PeopleError(f"没有这个人：{person_id}")
            previous_id = self.resolve(platform, account)
            if previous_id == person_id:
                return
            if previous_id is not None:
                previous = self._people[previous_id]
                previous.accounts[platform].remove(account)
                if not previous.accounts[platform]:
                    del previous.accounts[platform]
                if not previous.accounts:
                    previous.merged_into = person_id
                    target.names.extend(n for n in previous.names if n not in target.names)
                    if familiarity_rank(previous.familiarity) > familiarity_rank(
                        target.familiarity
                    ):
                        target.familiarity = previous.familiarity
                self._save(previous)
            target.accounts.setdefault(platform, []).append(account)
            self._save(target)
            self._rebuild_index()

    def set_familiarity(self, person_id: str, familiarity: str) -> Person:
        if familiarity not in FAMILIARITY_LEVELS:
            raise PeopleError(f"熟络度只能是 {'/'.join(FAMILIARITY_LEVELS)}：{familiarity}")
        with self._lock:
            target = self.get(person_id)
            if target is None or target.merged_into:
                raise PeopleError(f"没有这个人：{person_id}")
            target.familiarity = familiarity
            self._save(target)
            return target

    def _new_person(self, person_id: str | None, name: str) -> Person:
        if person_id is None:
            person_id = f"p{uuid4().hex[:8]}"
        if not PERSON_ID_RE.fullmatch(person_id) or person_id == SELF_ID:
            raise PeopleError(
                f"person_id 只能用小写字母、数字、- 和 _（且不能是 self）：{person_id}"
            )
        if person_id in self._people:
            raise PeopleError(f"已经有这个人：{person_id}")
        return Person(
            person_id=person_id,
            names=[name],
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            body=render_template("person.md", display_name=name),
        )

    def _save(self, person: Person) -> None:
        self._layout.people_dir.mkdir(parents=True, exist_ok=True)
        path = self._layout.person_path(person.person_id)
        path.write_text(dump_person(person), encoding="utf-8", newline="\n")
        self._people[person.person_id] = person

    def _rebuild_index(self) -> None:
        index: dict[tuple[str, str], str] = {}
        for person in self.list():
            for platform, accounts in person.accounts.items():
                for account in accounts:
                    key = (platform, account)
                    if key in index:
                        raise PeopleError(
                            f"账号 {platform}:{account} 同时属于 {index[key]} 和 {person.person_id}"
                        )
                    index[key] = person.person_id
        self._index = index
