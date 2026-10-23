import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.extract import split_sessions
from companion.memory import MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway

T0 = datetime(2026, 10, 20, 20, tzinfo=UTC)


def participants(messages) -> dict[str, str]:
    return dict(re.findall(r"^- (\w+)：(.+)$", messages[-1].content, re.M))


def known_facts(messages) -> dict[str, str]:
    block = messages[-1].content.split("已知事实（id：内容）：")[1].split("\n\n")[0]
    return dict(re.findall(r"^- (\w+)：(.+)$", block, re.M))


class Brain:
    """按场景给出回复或抽取结果。"""

    def __init__(self) -> None:
        self.facts_to_emit: list[dict] = []

    def __call__(self, messages, json_mode):
        if not json_mode:
            return "嗯，我记得哦。"
        people = participants(messages)
        other = next(pid for pid in people if pid != "self")
        facts = []
        for f in self.facts_to_emit:
            f = dict(f)
            f["subject"] = other if f["subject"] == "OTHER" else f["subject"]
            if f.get("supersedes") == ["LATEST"]:
                f["supersedes"] = list(known_facts(messages))
            facts.append(f)
        return json.dumps(
            {"episode": {"summary": "和对方聊了喝咖啡的习惯", "importance": 0.4, "emotion": 0.2},
             "facts": facts},
            ensure_ascii=False,
        )  # fmt: skip


def fact(statement, *, sources=("E1",), supersedes=(), subject="OTHER"):
    return {
        "subject": subject,
        "predicate": "coffee_preference",
        "statement": statement,
        "confidence": 0.9,
        "importance": 0.6,
        "pin": False,
        "sources": list(sources),
        "supersedes": list(supersedes),
    }


@pytest.fixture
def companion(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    layout = MemoryLayout(config.memory_dir)
    init_memory(layout, "白璎", git=False)
    (layout.self_dir / "seed.md").write_text(
        "# 种子\n\n- 我叫白璎，是一个 AI。\n", encoding="utf-8"
    )
    brain = Brain()
    gateway, chat = scripted_gateway(brain)
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    c.brain, c.chat = brain, chat
    yield c
    c.close()


def say(c, text, when, channel="cli:test"):
    return c.reply(body="cli", channel=channel, account="xiang", text=text, now=when,
                   display_name="祥")  # fmt: skip


def test_reply_records_both_sides_and_uses_seed(companion):
    r = say(companion, "晚上好", T0)
    events = list(companion.log.iter_events())
    assert [e.actor.person_id == "self" for e in events] == [False, True]
    assert events[1].content == "嗯，我记得哦。"
    system = companion.chat.requests[0][0].content
    assert "我叫白璎，是一个 AI" in system and "和祥私聊" in system
    assert r.result.backend == "fallback"


def test_extract_then_remember_in_new_channel(companion):
    companion.brain.facts_to_emit = [fact("祥喝咖啡不加糖、要加奶")]
    say(companion, "我喝咖啡不加糖，要加奶", T0)
    # 会话还没结束：不抽取
    assert companion.extract_pending(now=T0 + timedelta(minutes=5)) == []
    done = companion.extract_pending(now=T0 + timedelta(hours=1))
    assert len(done) == 1 and len(done[0].facts) == 1
    # 游标：同一段会话不会被抽取两次
    assert companion.extract_pending(now=T0 + timedelta(hours=2)) == []

    say(companion, "帮我点杯咖啡", T0 + timedelta(days=3), channel="cli:other")
    system = companion.chat.requests[-1][0].content
    assert "祥喝咖啡不加糖、要加奶" in system  # 换了频道、过了三天仍然记得


def test_preference_change_supersedes(companion):
    companion.brain.facts_to_emit = [fact("祥喝咖啡要加糖")]
    say(companion, "我喝咖啡要加糖", T0)
    companion.extract_pending(force=True, now=T0)

    companion.brain.facts_to_emit = [fact("祥现在喝咖啡不加糖了", supersedes=["LATEST"])]
    say(companion, "我现在不加糖了", T0 + timedelta(days=30))
    companion.extract_pending(force=True, now=T0 + timedelta(days=30))

    pid = companion.people.resolve("cli", "xiang")
    current = [f.statement for f in companion.index.current_facts(pid)]
    assert current == ["祥现在喝咖啡不加糖了"]
    assert len(companion.index.fact_history(pid, "coffee_preference")) == 2


def test_invalid_model_output_is_dropped(companion):
    companion.brain.facts_to_emit = [
        fact("没有出处的事实", sources=["E99"]),
        fact("主语乱写的事实", subject="nobody"),
        fact("取代不存在的事实", supersedes=["deadbeef"]),
    ]
    say(companion, "随便聊聊", T0)
    (result,) = companion.extract_pending(force=True, now=T0)
    statements = [f.statement for f, _ in result.facts]
    assert statements == ["取代不存在的事实"]
    assert result.facts[0][1] == []  # 无效的 supersedes 被去掉
    assert len(result.dropped) == 3


def test_recalled_memory_gets_stronger(companion):
    companion.brain.facts_to_emit = []
    say(companion, "我们上周一起盖了橡木小屋", T0)
    companion.extract_pending(force=True, now=T0)
    (ep,) = companion.index.episodes_with(companion.people.resolve("cli", "xiang"))
    before = companion.index.strength("episode", ep.id).recall_count
    companion.index.set_meta("noop", "1")
    # 另一个人问起相关的事：情景不是常驻的共同经历，会走检索
    companion.reply(body="cli", channel="cli:x", account="other", text="喝咖啡的习惯",
                    now=T0 + timedelta(days=1))  # fmt: skip
    assert companion.index.strength("episode", ep.id).recall_count == before + 1


def test_split_sessions():
    class E:
        def __init__(self, minutes):
            self.ts = T0 + timedelta(minutes=minutes)

    sessions = split_sessions([E(0), E(10), E(100), E(105)], timedelta(minutes=30))
    assert [len(s) for s in sessions] == [2, 2]
