from datetime import UTC, datetime

import pytest

from companion.core import Ingest
from companion.memory import MemoryFormatError, MemoryLayout, init_memory


@pytest.fixture
def ingest(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎", git=False)
    return Ingest(layout)


def test_same_person_across_bodies(ingest):
    ingest.people.create("xiaoming", "小明")
    ingest.people.bind("qq", "10001", "xiaoming")
    ingest.people.bind("minecraft", "Steve", "xiaoming")

    qq = ingest.record(
        body="qq", channel="group:42", kind="message", account="10001", content="周末去下界吧"
    )
    mc = ingest.record(
        body="minecraft", channel="world:main", kind="action", account="Steve", content="放置了木门"
    )
    assert qq.actor.person_id == mc.actor.person_id == "xiaoming"
    assert [e.content for e in ingest.log.iter_events()] == ["周末去下界吧", "放置了木门"]


def test_stranger_is_registered_on_first_event(ingest):
    event = ingest.record(
        body="qq",
        channel="group:42",
        kind="message",
        account="20002",
        display_name="路人",
        content="你好",
    )
    assert ingest.people.get(event.actor.person_id).names == ["路人"]


def test_self_events(ingest):
    event = ingest.record_self(body="qq", channel="group:42", content="大家好，我是白璎")
    assert event.actor.person_id == "self"
    assert ingest.people.list() == []  # 自己不进人物表


def test_explicit_timestamp_and_meta(ingest):
    ts = datetime(2026, 7, 3, 12, tzinfo=UTC)
    event = ingest.record(
        body="qq",
        channel="private:1",
        kind="message",
        account="1",
        content="x",
        ts=ts,
        meta={"reply_to": "abc"},
    )
    assert event.ts == ts and event.meta == {"reply_to": "abc"}
    assert ingest.log.file_for(ts).name == "2026-07.jsonl"


def test_refuses_uninitialized_folder(tmp_path):
    with pytest.raises(MemoryFormatError):
        Ingest(MemoryLayout(tmp_path / "nothing"))
