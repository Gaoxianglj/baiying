"""记忆存储层：事件日志（JSONL）、SQLite 派生索引、自我与人物档案（Markdown + git）。"""

from companion.memory.events import Actor, Event, EventLog
from companion.memory.layout import (
    MemoryFormatError,
    MemoryLayout,
    init_memory,
    load_manifest,
)

__all__ = [
    "Actor",
    "Event",
    "EventLog",
    "MemoryFormatError",
    "MemoryLayout",
    "init_memory",
    "load_manifest",
]
