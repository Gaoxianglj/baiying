import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from companion.memory import (
    Episode,
    Fact,
    MemoryIndex,
    MemoryIndexError,
    MemoryLayout,
    init_memory,
)

T0 = datetime(2026, 7, 3, 12, tzinfo=UTC)


@pytest.fixture
def layout(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎", git=False)
    return layout


@pytest.fixture
def index(layout):
    with MemoryIndex(layout) as index:
        yield index


def house_episode(**kw) -> Episode:
    fields = {
        "started_at": T0,
        "ended_at": T0 + timedelta(hours=2),
        "body": "minecraft",
        "channel": "world:main",
        "summary": "和小明在 MC 里盖了一座木屋，他掉进岩浆两次",
        "participants": ["xiaoming", "self"],
        "event_ids": ["e1", "e2"],
        "importance": 0.6,
        "emotion": 0.5,
    }
    return Episode(**(fields | kw))


def test_episode_round_trip_and_person_filter(index):
    ep = house_episode()
    index.add_episode(ep)
    assert index.get_episode(ep.id) == ep
    assert [e.id for e in index.episodes_with("xiaoming")] == [ep.id]
    assert index.episodes_with("nobody") == []


def test_chinese_two_char_search(index):
    house = house_episode()
    index.add_episode(house)
    index.add_episode(house_episode(summary="一起去下界找了很久要塞"))
    hits = index.search_text("木屋")
    assert [h.memory_id for h in hits] == [house.id]
    assert index.search_text("岩浆")[0].memory_id == house.id


def test_preference_change_keeps_history(index):
    old = Fact(
        subject="xiaoming",
        predicate="coffee_preference",
        statement="小明喜欢加糖的咖啡",
        valid_from=T0 - timedelta(days=90),
        event_ids=["e0"],
    )
    new = Fact(
        subject="xiaoming",
        predicate="coffee_preference",
        statement="小明喜欢不加糖、加奶的咖啡",
        valid_from=T0,
        event_ids=["e9"],
    )
    index.add_fact(old)
    index.add_fact(new, supersedes=[old.id])

    assert [f.statement for f in index.current_facts("xiaoming")] == [new.statement]
    history = index.fact_history("xiaoming", "coffee_preference")
    assert [f.id for f in history] == [old.id, new.id]
    assert history[0].valid_to == T0 and history[0].invalidated_by == new.id
    # 失效的事实不再被检索到，但没有被删除
    assert [h.memory_id for h in index.search_text("咖啡")] == [new.id]
    assert index.get_fact(old.id) is not None


def test_cannot_supersede_twice(index):
    a = Fact(subject="s", predicate="p", statement="甲", valid_from=T0)
    b = Fact(subject="s", predicate="p", statement="乙", valid_from=T0 + timedelta(days=1))
    c = Fact(subject="s", predicate="p", statement="丙", valid_from=T0 + timedelta(days=2))
    index.add_fact(a)
    index.add_fact(b, supersedes=[a.id])
    with pytest.raises(MemoryIndexError):
        index.add_fact(c, supersedes=[a.id])
    assert index.get_fact(c.id) is None  # 整笔回滚


def test_strength_decays_and_recall_strengthens(index):
    ep = house_episode()
    index.add_episode(ep)
    s0 = index.strength("episode", ep.id)
    end = ep.ended_at
    assert s0.retrievability(end) == pytest.approx(1.0)
    assert s0.retrievability(end + timedelta(days=30)) < 0.01

    index.mark_recalled("episode", ep.id, now=end + timedelta(days=1))
    s1 = index.strength("episode", ep.id)
    assert s1.stability_days > s0.stability_days
    assert s1.recall_count == 1
    assert s1.retrievability(end + timedelta(days=1)) == pytest.approx(1.0)


def test_pinned_never_fades(index):
    fact = Fact(subject="xiaoming", predicate="allergy", statement="小明对花生过敏", valid_from=T0)
    index.add_fact(fact)
    index.set_pinned("fact", fact.id)
    assert index.strength("fact", fact.id).retrievability(T0 + timedelta(days=3650)) == 1.0


def test_dormant_episode_is_not_searched(index):
    ep = house_episode()
    index.add_episode(ep)
    index.set_episode_state(ep.id, "dormant")
    assert index.search_text("木屋") == []


def test_persists_and_resets(layout):
    with MemoryIndex(layout) as index:
        index.add_episode(house_episode())
    with MemoryIndex(layout) as index:
        assert len(index.search_text("木屋")) == 1
        index.reset()
        assert index.search_text("木屋") == []


def test_wrong_schema_version_is_refused(layout):
    conn = sqlite3.connect(layout.index_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(MemoryIndexError, match="重建"):
        MemoryIndex(layout)


def test_naive_datetime_rejected(index):
    with pytest.raises(ValueError):
        index.add_episode(house_episode().model_copy(update={"started_at": datetime(2026, 1, 1)}))
