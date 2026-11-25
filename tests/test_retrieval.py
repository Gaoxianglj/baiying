from datetime import UTC, datetime, timedelta

import pytest

from companion.core.retrieval import HybridRetriever, sync_vectors
from companion.memory import Episode, Fact, MemoryIndex, MemoryLayout, init_memory
from tests.fakes import FakeEmbedder

T0 = datetime(2026, 7, 3, 12, tzinfo=UTC)


@pytest.fixture
def index(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎", git=False)
    with MemoryIndex(layout) as index:
        yield index


def episode(summary, people, start=T0):
    return Episode(
        started_at=start,
        ended_at=start + timedelta(hours=1),
        body="qq",
        channel="group:1",
        summary=summary,
        participants=people,
    )


def fact(subject, statement, start=T0):
    return Fact(subject=subject, predicate="p", statement=statement, valid_from=start)


def test_sync_vectors_only_embeds_missing(index):
    emb = FakeEmbedder()
    index.add_episode(episode("和小明盖了木屋", ["xiaoming"]))
    index.add_fact(fact("xiaoming", "小明喜欢不加糖的咖啡"))
    assert sync_vectors(index, emb) == 2
    assert sync_vectors(index, emb) == 0
    # 换模型：全部重算
    assert sync_vectors(index, FakeEmbedder(model="other")) == 2


def test_hybrid_finds_by_text_and_vector(index):
    emb = FakeEmbedder()
    house = episode("和小明在 MC 里盖了一座木屋", ["xiaoming"])
    coffee = fact("xiaoming", "小明喜欢不加糖、加奶的咖啡")
    index.add_episode(house)
    index.add_fact(coffee)
    index.add_episode(episode("小红推荐了一本诗集", ["xiaohong"]))
    sync_vectors(index, emb)

    hits = HybridRetriever(index, emb).search("小明的咖啡口味")
    assert hits[0].memory_id == coffee.id
    assert hits[0].in_text and hits[0].cosine is not None
    assert 0 < hits[0].relevance <= 1


def test_person_and_time_filters(index):
    emb = FakeEmbedder()
    old = episode("和小明一起钓鱼", ["xiaoming"], start=T0 - timedelta(days=100))
    new = episode("和小明一起挖矿", ["xiaoming"])
    other = episode("和小红一起挖矿", ["xiaohong"])
    for ep in (old, new, other):
        index.add_episode(ep)
    sync_vectors(index, emb)
    r = HybridRetriever(index, emb)

    xiaoming_hits = [c.memory_id for c in r.search("挖矿", person_id="xiaoming")]
    assert xiaoming_hits[0] == new.id and other.id not in xiaoming_hits
    recent = r.search("一起", since=T0 - timedelta(days=1))
    assert old.id not in {c.memory_id for c in recent}
    assert r.search("挖矿", person_id="nobody") == []


def test_min_cosine_drops_vector_only_noise(index):
    emb = FakeEmbedder()
    index.add_episode(episode("小红推荐了一本诗集", ["xiaohong"]))
    sync_vectors(index, emb)
    assert HybridRetriever(index, emb, min_cosine=0.9).search("今天天气不错") == []


def test_falls_back_to_text_when_embedder_down(index):
    emb = FakeEmbedder()
    house = episode("和小明盖了木屋", ["xiaoming"])
    index.add_episode(house)
    sync_vectors(index, emb)
    emb.broken = True
    hits = HybridRetriever(index, emb).search("木屋")
    assert [h.memory_id for h in hits] == [house.id]
    assert hits[0].cosine is None


def test_superseded_fact_not_retrieved(index):
    emb = FakeEmbedder()
    old = fact("xiaoming", "小明喜欢加糖的咖啡")
    new = fact("xiaoming", "小明喜欢不加糖的咖啡", start=T0 + timedelta(days=1))
    index.add_fact(old)
    index.add_fact(new, supersedes=[old.id])
    sync_vectors(index, emb)
    assert [h.memory_id for h in HybridRetriever(index, emb).search("咖啡加糖")] == [new.id]


def test_zero_text_weight_does_not_leak_low_cosine_fts_noise(index):
    emb = FakeEmbedder()
    ep = episode("小明今天在公园看见一只白猫", ["xiaoming"])
    index.add_episode(ep)
    sync_vectors(index, emb)
    # 词面沾边（“今天”）但向量余弦远低于 0.99：text_weight=0.0 时绝不能放行零分噪音
    strict = HybridRetriever(index, emb, text_weight=0.0, min_cosine=0.99)
    assert strict.search("今天怎么算账") == []
    # 但显式指定 text_weight=0.5（如 /忘掉 关键词查找）仍可按词面命中
    assert [h.memory_id for h in strict.search("今天怎么算账", text_weight=0.5)] == [ep.id]
