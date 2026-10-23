import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper
from companion.memory import Episode, Fact, MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway

T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)


class Brain:
    def __init__(self) -> None:
        self.invent = False

    def __call__(self, messages, json_mode):
        system, user = messages[0].content, messages[-1].content
        ids = re.findall(r"^\[(\w{8})\] (.+)$", user, re.M)
        if not json_mode:
            return "今天和大家聊了很多，有点开心。"
        if "自我反思" in system:
            two = [i for i, _ in ids[:2]]
            return json.dumps({"我是谁": [{"text": "我喜欢安静地读诗", "sources": two},
                                          {"text": "我其实很会打游戏", "sources": two[:1]}],
                               "我在意的事": [], "最近的我": []}, ensure_ascii=False)  # fmt: skip
        items = [{"text": text.replace("（新）", ""), "sources": [i]} for i, text in ids]
        if self.invent:
            items.append({"text": "小明是外星人", "sources": ["zzzzzzzz"]})
        return json.dumps({"偏好": items, "关系与称呼": [], "共同经历": [],
                           "只对 ta 使用的说法": []}, ensure_ascii=False)  # fmt: skip


@pytest.fixture
def c(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = Brain()
    gateway, _ = scripted_gateway(brain)
    comp = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    comp.brain = brain
    comp.people.create("xiaoming", "小明")
    yield comp
    comp.close()


def add_episode(c, summary, start, **kw):
    ep = Episode(
        started_at=start,
        ended_at=start + timedelta(hours=1),
        body="qq",
        channel="group:1",
        summary=summary,
        participants=["xiaoming", "self"],
        **kw,
    )
    c.index.add_episode(ep, now=start)
    return ep


def add_fact(c, statement, start, subject="xiaoming", predicate="p"):
    f = Fact(subject=subject, predicate=predicate, statement=statement, valid_from=start)
    c.index.add_fact(f, now=start)
    return f


def test_forgetting_moves_episodes_through_states(c):
    old = add_episode(c, "很久以前一起钓鱼", T0 - timedelta(days=60), importance=0.1)
    pinned = add_episode(c, "约好的事", T0 - timedelta(days=60), importance=0.1, pinned=True)
    fresh = add_episode(c, "昨天一起挖矿", T0 - timedelta(days=1), importance=0.5)
    c.sync_vectors()
    report = Sleeper(c).run(now=T0)
    assert c.index.get_episode(old.id).state == "dormant"
    assert c.index.get_episode(pinned.id).state == "vivid"
    assert c.index.get_episode(fresh.id).state == "vivid"
    assert report.dormant == 1


def test_duplicate_facts_merged(c):
    a = add_fact(c, "小明喜欢喝拿铁", T0 - timedelta(days=3))
    b = add_fact(c, "小明喜欢喝拿铁", T0 - timedelta(days=1))
    c.sync_vectors()
    report = Sleeper(c).run(now=T0)
    assert report.merged == 1
    assert [f.id for f in c.index.current_facts("xiaoming")] == [b.id]
    assert c.index.get_fact(a.id).invalidated_by == b.id


def test_person_reflection_needs_sources(c):
    c.brain.invent = True
    f = add_fact(c, "小明喜欢后摇", T0 - timedelta(hours=2))
    Sleeper(c).run(now=T0)
    body = c.layout.person_path("xiaoming").read_text("utf-8")
    assert f"小明喜欢后摇〔{f.id[:8]}〕" in body
    assert "外星人" not in body  # 编造的出处被丢弃
    assert body.startswith("---\n")  # frontmatter 保留


def test_self_reflection_requires_two_sources(c):
    add_episode(c, "和小明聊诗", T0 - timedelta(hours=3))
    add_fact(c, "白璎喜欢海子的诗", T0 - timedelta(hours=2), subject="self")
    report = Sleeper(c).run(now=T0)
    identity = (c.layout.self_dir / "identity.md").read_text("utf-8")
    assert report.identity_reflected
    assert "我喜欢安静地读诗" in identity
    assert "很会打游戏" not in identity  # 只有一条依据，不采纳


def test_journal_and_meta(c):
    add_episode(c, "下午和小明在群里聊天", T0 - timedelta(hours=5))
    report = Sleeper(c).run(now=T0)
    assert report.journal and "有点开心" in Path(report.journal).read_text("utf-8")
    # 第二次睡眠：没有新东西，不再反思
    again = Sleeper(c).run(now=T0 + timedelta(days=1))
    assert again.people_reflected == [] and again.journal is None
