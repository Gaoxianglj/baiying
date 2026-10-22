"""记忆存储层：事件日志（JSONL）、SQLite 派生索引、自我与人物档案（Markdown + git）。"""

from companion.memory.events import Actor, Event, EventLog
from companion.memory.index import Episode, Fact, MemoryIndex, MemoryIndexError, TextHit
from companion.memory.layout import (
    MemoryFormatError,
    MemoryLayout,
    init_memory,
    load_manifest,
)
from companion.memory.people import SELF_ID, PeopleError, PeopleRegistry, Person

__all__ = [
    "Episode",
    "Fact",
    "MemoryIndex",
    "MemoryIndexError",
    "TextHit",
    "SELF_ID",
    "PeopleError",
    "PeopleRegistry",
    "Person",
    "Actor",
    "Event",
    "EventLog",
    "MemoryFormatError",
    "MemoryLayout",
    "init_memory",
    "load_manifest",
]
