import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper, clamp_familiarity, familiarity_ceiling
from companion.memory import Episode, Fact, MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway

T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)


class Brain:
    def __init__(self) -> None:
        self.invent = False
        self.update_preferences = False
        self.update_style = False
        self.update_person_style = False
        self.proposed_familiarity = "亲近"

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
        if "喜好与习惯（次核心层）" in system:
            two = [i for i, _ in ids[:2]]
            return json.dumps(
                {
                    "changed": self.update_preferences,
                    "穿搭与随身物": [{"text": "天冷时想象披一件米白开衫", "sources": two}],
                    "读书与品味": [{"text": "没有依据的书单", "sources": two[:1]}],
                    "喜欢与不喜欢": [],
                    "小习惯与熟悉的事": [],
                    "闲下来喜欢做的事": [],
                },
                ensure_ascii=False,
            )
        if "全局表达风格" in system:
            two = [i for i, _ in ids[:2]]
            return json.dumps(
                {
                    "changed": self.update_style,
                    "口头禅与常用说法": [],
                    "句式与语气": [],
                    "熟络后的破壳与打趣": [{"text": "和熟人打趣时句末偶尔带（）", "sources": two}],
                    "从大家那里学到的说法": [],
                },
                ensure_ascii=False,
            )
        if "人际熟络度与说话方式审视" in system:
            one = [ids[0][0]] if ids else []
            return json.dumps(
                {
                    "changed": self.update_person_style,
                    "familiarity": self.proposed_familiarity,
                    "对 ta 的说话方式": [{"text": "可以轻松接话打趣", "sources": one}],
                },
                ensure_ascii=False,
            )
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


def test_weekly_sync_updates_preferences_style_and_clamps_familiarity(c):
    # 准备跨两天的互动与至少 3 段经历，使客观上限达到“相熟”（但不到“熟络/亲近”）
    c.observe(body="qq", channel="group:1", account="1001", text="早", now=T0 - timedelta(days=2))
    pid = c.people.resolve("qq", "1001")
    c.people.bind("qq", "1001", "xiaoming")
    assert pid is not None
    c.observe(body="qq", channel="group:1", account="1001", text="晚", now=T0 - timedelta(hours=4))
    for h in (6, 4, 2):
        add_episode(c, f"和小明第{h}次聊天", T0 - timedelta(hours=h))
    add_fact(c, "白璎喜欢收集好词", T0 - timedelta(hours=1), subject="self")

    c.brain.update_preferences = True
    c.brain.update_style = True
    c.brain.update_person_style = True
    c.brain.proposed_familiarity = "亲近"  # 模型想直接跳到最高档，程序应按步长和客观上限拦在“相熟”

    report = Sleeper(c).run(now=T0)
    assert report.preferences_reflected is True
    assert report.style_reflected is True
    assert report.familiarity_updated == ["xiaoming"]

    prefs = (c.layout.self_dir / "preferences.md").read_text("utf-8")
    assert "天冷时想象披一件米白开衫" in prefs
    assert "没有依据的书单" not in prefs

    style = (c.layout.self_dir / "style.md").read_text("utf-8")
    assert "和熟人打趣时句末偶尔带（）" in style

    person = c.people.get("xiaoming")
    assert person.familiarity == "相熟"
    assert "可以轻松接话打趣" in person.body

    # 次日普通睡眠只跑每日人物反思：不应冲掉 7 天审视写下的“对 ta 的说话方式”与熟络度
    add_fact(c, "小明爱吃牛肉面", T0 + timedelta(hours=20))
    Sleeper(c).run(now=T0 + timedelta(days=1))
    after = c.people.get("xiaoming")
    assert after.familiarity == "相熟"
    assert "可以轻松接话打趣" in after.body
    assert "小明爱吃牛肉面" in after.body


def test_familiarity_ceiling_and_step_rules():
    assert familiarity_ceiling(active_days=1, episodes_count=10, facts_count=5) == "初识"
    assert familiarity_ceiling(active_days=2, episodes_count=3, facts_count=0) == "相熟"
    assert familiarity_ceiling(active_days=8, episodes_count=20, facts_count=2) == "熟络"
    assert (
        familiarity_ceiling(active_days=20, episodes_count=50, facts_count=20, is_peer_bot=True)
        == "熟络"
    )
    assert familiarity_ceiling(active_days=20, episodes_count=50, facts_count=20) == "亲近"
    assert clamp_familiarity("初识", "亲近", "亲近") == "相熟"  # 单次最多升 1 档
    assert clamp_familiarity("相熟", "熟络", "相熟") == "相熟"  # 不能超过客观上限
    assert clamp_familiarity("熟络", "初识", "熟络") == "相熟"  # 单次最多降 1 档


def test_reflect_person_handles_merged_pid_and_prevents_empty_wipe(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)

    # 返回不含有效 8 位编号的条目，触发 _validated 全空，应保留原有档案而不清空
    gateway, _ = scripted_gateway(
        lambda m, j: json.dumps(
            {
                "偏好": ["无编号编造条目"],
                "关系与称呼": [],
                "共同经历": [],
                "只对 ta 使用的说法": [],
            },
            ensure_ascii=False,
        )
        if j
        else "今天很安静。"
    )
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    try:
        old_pid = c.people.ensure("qq", "1111", "旧号")
        main_pid = c.people.ensure("qq", "2222", "主号")
        c.people.bind("qq", "1111", main_pid)  # old_pid -> merged_into main_pid
        main_path = c.layout.person_path(main_pid)
        main_path.write_text(
            main_path.read_text("utf-8").replace("## 偏好\n", "## 偏好\n\n- 喜欢乌龙茶。\n"),
            encoding="utf-8",
        )
        c.people.reload()

        f = Fact(
            subject=old_pid,
            predicate="drink",
            statement="主号也喜欢白桃乌龙",
            valid_from=T0,
            event_ids=["ev1"],
        )
        c.index.add_fact(f, now=T0)
        sleeper = Sleeper(c)
        # 1. 校验落空时不应清空原有“喜欢乌龙茶”，且不写坏旧 pid 文件导致 reload 崩
        assert sleeper._reflect_person(old_pid, [], [f]) is False
        c.people.reload()
        assert "喜欢乌龙茶" in c.people.get(main_pid).body

        # 2. 有合法编号时写回 canonical main_pid 文件，不触发 PeopleError
        fid8 = f.id[:8]
        c.gateway.fallback.client._http = None  # ensure scripted handler still used
        c.gateway.fallback.client.chat = lambda msgs, **kw: type(  # type: ignore[method-assign]
            "R",
            (),
            {
                "text": json.dumps(
                    {
                        "偏好": [{"text": "喜欢白桃乌龙。", "sources": [fid8]}],
                        "关系与称呼": [],
                        "共同经历": [],
                        "只对 ta 使用的说法": [],
                    },
                    ensure_ascii=False,
                ),
                "backend": "fallback",
                "model": "scripted",
                "input_tokens": 0,
                "output_tokens": 0,
                "seconds": 0.0,
                "raw": {},
            },
        )()
        assert sleeper._reflect_person(old_pid, [], [f]) is True
        c.people.reload()
        assert "喜欢白桃乌龙" in c.people.get(main_pid).body
    finally:
        c.close()
