from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from companion.memory import Actor, Event, EventLog


def make_event(ts: datetime, content: str = "你好") -> Event:
    return Event(
        ts=ts,
        body="qq",
        channel="group:42",
        kind="message",
        actor=Actor(platform="qq", platform_id="10001", display_name="小明"),
        content=content,
    )


def test_append_and_read_back(tmp_path):
    log = EventLog(tmp_path)
    event = make_event(datetime(2026, 10, 22, 12, 0, tzinfo=UTC), "我喜欢不加糖、加奶的咖啡")
    path = log.append(event)

    assert path.name == "2026-10.jsonl"
    assert "不加糖" in path.read_text("utf-8")  # 中文按 UTF-8 原样存储
    assert list(log.iter_events()) == [event]


def test_files_split_by_utc_month(tmp_path):
    log = EventLog(tmp_path)
    beijing = timezone(timedelta(hours=8))
    # 北京时间 11 月 1 日 07:00 = UTC 10 月 31 日 23:00
    log.append(make_event(datetime(2026, 11, 1, 7, 0, tzinfo=beijing)))
    assert (tmp_path / "2026-10.jsonl").exists()


def test_since_filter_and_order(tmp_path):
    log = EventLog(tmp_path)
    old = make_event(datetime(2026, 9, 30, tzinfo=UTC), "九月")
    new = make_event(datetime(2026, 10, 2, tzinfo=UTC), "十月")
    log.append(new)
    log.append(old)
    assert [e.content for e in log.iter_events()] == ["九月", "十月"]
    assert [e.content for e in log.iter_events(since=datetime(2026, 10, 1, tzinfo=UTC))] == ["十月"]


def test_corrupted_line_is_skipped(tmp_path, caplog):
    log = EventLog(tmp_path)
    good = make_event(datetime(2026, 10, 22, tzinfo=UTC))
    path = log.append(good)
    with path.open("a", encoding="utf-8") as f:
        f.write('{"schema_version": 1, "id": "half-writ')
    assert list(log.iter_events()) == [good]
    assert "跳过损坏的事件行" in caplog.text


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValidationError):
        make_event(datetime(2026, 10, 22))


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Event.model_validate(
            {
                "body": "qq",
                "channel": "c",
                "kind": "message",
                "actor": {"platform": "qq", "platform_id": "1"},
                "content": "x",
                "mood": "?",
            }
        )
