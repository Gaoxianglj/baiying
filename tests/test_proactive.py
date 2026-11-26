"""E-05 主动找主人、E-06 额度充足时自主活动。"""

import asyncio
import json
import random
from datetime import datetime, timedelta

import pytest

from companion.bodies.qq.bot import QQBot
from companion.config import ActivityConfig, OwnerChatConfig, QQConfig, load_config
from companion.core.companion import Companion
from companion.life.activities import ActivityPrep, ActivityRunner, quota_check
from companion.life.owner import in_quiet
from companion.memory import MemoryLayout, init_memory
from companion.models.codex_app_server import RateLimits, RateWindow
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatResult
from tests.fakes import FakeEmbedder

OWNER, BOT = "2000", "1000"


def local(*args) -> datetime:
    return datetime(*args).astimezone()


NOW = local(2026, 10, 27, 15, 0)


def limits(used=5.0, minutes_left=60.0, weekly=10.0, now=NOW) -> RateLimits:
    reset = now + timedelta(minutes=minutes_left)
    return RateLimits(
        (RateWindow(used, 300, reset), RateWindow(weekly, 10080, now + timedelta(days=3)))
    )


class FakeSubscription:
    """假的 GPT 订阅：给出额度读数，按 handler 回复。"""

    name = "subscription"

    def __init__(self, handler) -> None:
        self.handler = handler
        self.limits = limits()
        self.last_rate_limits = None
        self.requests = []

    def read_rate_limits(self):
        self.last_rate_limits = self.limits
        return self.limits

    def chat(self, messages, *, json_schema=None, max_tokens=None):
        self.requests.append(messages)
        return ChatResult(text=self.handler(messages), backend="subscription", model="fake")

    def close(self):
        pass


class FakeLife:
    """假的做事进程：挑事情、查资料、写东西、画画都按脚本来。"""

    def __init__(self, tmp_path, choice) -> None:
        self.choice = choice
        self.calls = []
        self.image = tmp_path / "drawn.png"
        from PIL import Image

        Image.new("RGB", (8, 8), "white").save(self.image)

    def chat(self, messages, *, json_schema=None):
        system = messages[0].content
        self.calls.append(system)
        if "空闲时间" in system:
            return ChatResult(json.dumps(self.choice, ensure_ascii=False), "life", "fake")
        if "联网搜索" in system:
            data = {"title": "查了雪花为什么是六角形", "text": "因为水分子结晶时是六方晶系。",
                    "sources": ["https://example.org/snow"], "share": True,
                    "mood": "开心｜原来是这样"}  # fmt: skip
        else:
            data = {"title": "初雪", "text": "雪落在书页上，\n像一个没说完的字。", "sources": [],
                    "share": True, "mood": ""}  # fmt: skip
        return ChatResult(json.dumps(data, ensure_ascii=False), "life", "fake")

    def generate_image(self, prompt, references=()):
        self.calls.append("画：" + prompt)
        return self.image, "窗边的雪"

    def close(self):
        pass


class Brain:
    def __init__(self) -> None:
        self.reply = "早呀，今天想把《人间草木》读完。"

    def __call__(self, messages):
        return self.reply


@pytest.fixture
def c(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = Brain()
    sub = FakeSubscription(brain)
    comp = Companion(config, gateway=ModelGateway(subscription=sub), embedder=FakeEmbedder())
    comp.brain, comp.sub = brain, sub
    comp.fake_life = FakeLife(tmp_path, {"kind": "explore", "topic": "雪花为什么是六角形",
                                         "why": "今天在读写雪的文章"})  # fmt: skip
    comp.activities = ActivityRunner(lambda: comp.fake_life)
    comp.owner_id = comp.people.ensure("qq", OWNER, "祥")
    yield comp
    comp.close()


CHANNEL = f"private:{OWNER}"


def owner_says(c, text, when):
    return c.observe(body="qq", channel=CHANNEL, account=OWNER, text=text, now=when,
                     display_name="祥")  # fmt: skip


# ---- 早安、晚安 ----


def test_greeting_time_is_jittered_but_stable(c):
    policy = c.life.owner
    a = policy.greeting_time(NOW, "morning", "08:30")
    assert a == policy.greeting_time(NOW + timedelta(hours=1), "morning", "08:30")
    assert abs(a - local(2026, 10, 27, 8, 30)) <= timedelta(minutes=20)
    late = policy.greeting_time(NOW, "night", "00:30")  # 过了午夜的晚安仍属于这一天
    assert abs(late - local(2026, 10, 28, 0, 30)) <= timedelta(minutes=20)


def test_quiet_hours_wrap_midnight():
    assert in_quiet(local(2026, 10, 27, 2, 0), "00:30", "08:00")
    assert not in_quiet(local(2026, 10, 27, 9, 0), "00:30", "08:00")
    assert in_quiet(local(2026, 10, 27, 23, 30), "23:00", "07:00")
    assert in_quiet(local(2026, 10, 28, 6, 0), "23:00", "07:00")


def test_morning_greeting_once(c):
    policy = c.life.owner
    t = policy.greeting_time(NOW, "morning", "08:30") + timedelta(minutes=1)
    assert policy.greeting_due(c.owner_id, CHANNEL, t - timedelta(minutes=5)) is None  # 还没到
    assert policy.greeting_due(c.owner_id, CHANNEL, t) == "morning"
    reply = c.greet(person_id=c.owner_id, body="qq", channel=CHANNEL, kind="morning", now=t)
    assert reply.text == "早呀，今天想把《人间草木》读完。"
    messages = c.sub.requests[-1]
    assert messages[-1].role == "system" and "早安" in messages[-1].content
    assert not any(m.role == "user" for m in messages)  # 没有编一句“对方说的话”
    assert "这次是你主动开口" in messages[0].content
    mine = [e for e in c.log.iter_events() if e.channel == CHANNEL]
    assert mine[-1].meta["proactive"] == "morning"
    assert policy.greeting_due(c.owner_id, CHANNEL, t + timedelta(minutes=10)) is None


def test_greeting_skipped_after_recent_chat(c):
    policy = c.life.owner
    t = policy.greeting_time(NOW, "morning", "08:30") + timedelta(minutes=1)
    owner_says(c, "早上好", t - timedelta(minutes=10))
    assert policy.greeting_due(c.owner_id, CHANNEL, t) is None
    assert policy.greeting_due(c.owner_id, CHANNEL, t + timedelta(minutes=70)) is None  # 当天不再试


def test_declined_greeting_is_not_retried(c):
    c.brain.reply = "【不回复】"
    t = c.life.owner.greeting_time(NOW, "night", "23:30") + timedelta(minutes=1)
    assert c.greet(person_id=c.owner_id, body="qq", channel=CHANNEL, kind="night", now=t) is None
    assert c.life.owner.greeting_due(c.owner_id, CHANNEL, t + timedelta(minutes=1)) is None


# ---- 分享的规矩 ----


def test_share_rules(c):
    policy = c.life.owner
    assert policy.can_share(c.owner_id, CHANNEL, NOW) == (True, "")
    assert not policy.can_share(c.owner_id, CHANNEL, local(2026, 10, 28, 3, 0))[0]  # 安静时段
    policy.record(NOW, "share", "我刚查到雪花……")
    assert (
        policy.can_share(c.owner_id, CHANNEL, NOW + timedelta(minutes=30))[1] == "离上次主动找太近"
    )
    later = NOW + timedelta(hours=3)
    assert policy.can_share(c.owner_id, CHANNEL, later)[1] == "上一条主人还没回"
    owner_says(c, "哇真的吗", NOW + timedelta(minutes=5))
    assert policy.can_share(c.owner_id, CHANNEL, later)[0]
    for i in range(3):
        policy.record(later + timedelta(minutes=i), "share", "x")
    assert (
        policy.can_share(c.owner_id, CHANNEL, later + timedelta(hours=5))[1]
        == "今天主动找的次数用完了"
    )


# ---- 额度 ----


def test_quota_gate():
    cfg = ActivityConfig()
    assert quota_check(limits(minutes_left=200), None, cfg, NOW)[0] is None  # 离重置还早
    assert quota_check(limits(used=40), None, cfg, NOW)[0] is None  # 已经用了不少
    assert quota_check(limits(weekly=70), None, cfg, NOW)[0] is None  # 7 天额度紧
    session, _ = quota_check(limits(used=5), None, cfg, NOW)
    assert session is not None and session.start_used == 5
    # 自主时间开始后，用量超过 15% 的触发线也继续，直到花满 15%
    assert quota_check(limits(used=18), session, cfg, NOW)[0] is session
    assert quota_check(limits(used=20), session, cfg, NOW) == (
        None,
        "这个窗口的自主活动已经花了 15%",
    )
    session.count = cfg.max_per_window
    assert quota_check(limits(used=6), session, cfg, NOW)[0] is None
    # 换了窗口：重新判断
    fresh = limits(used=1, minutes_left=290)
    assert quota_check(fresh, session, cfg, NOW)[0] is None


def test_prepare_activity_only_on_subscription(c):
    prep = c.prepare_activity(NOW, owner_id=c.owner_id, owner_channel=CHANNEL)
    assert prep is not None and "chat_owner" in prep.kinds and prep.owner_name == "祥"
    c.gateway.prefer = "fallback"  # 在用 DeepSeek：不做
    assert c.prepare_activity(NOW) is None
    c.gateway.prefer = "auto"
    c.sub.limits = limits(minutes_left=200)
    c.life._limits = None
    assert c.prepare_activity(NOW) is None


def test_prepare_activity_respects_gap(c):
    assert c.prepare_activity(NOW, owner_id=c.owner_id, owner_channel=CHANNEL) is not None
    c.finish_activity(None, NOW)
    assert c.prepare_activity(
        NOW + timedelta(minutes=5), owner_id=c.owner_id, owner_channel=CHANNEL
    ) is None
    assert c.prepare_activity(
        NOW + timedelta(minutes=25), owner_id=c.owner_id, owner_channel=CHANNEL
    ) is not None


# ---- 做事 ----


def prep(kinds=("explore", "write", "draw", "chat_owner"), owner="祥"):
    return ActivityPrep("白璎", owner, list(kinds), "【你的兴趣】\n读书", NOW)


def test_runner_explore(c):
    result = c.activities.perform(prep())
    assert result.kind == "explore" and result.share
    assert result.title == "查了雪花为什么是六角形"
    assert result.mood == ("开心", "原来是这样")


def test_runner_rejects_kind_not_allowed(c):
    c.fake_life.choice = {"kind": "draw", "topic": "窗边的雪", "why": "想画"}
    assert c.activities.perform(prep(kinds=["explore"])).kind == "rest"


def test_runner_bad_json_is_none(c):
    c.fake_life.chat = lambda messages, json_schema=None: ChatResult("不是 JSON", "life", "f")
    assert c.activities.perform(prep()) is None


def test_finish_explore_records_and_shares(c):
    c.brain.reply = "诶，我刚查到雪花是六角形是因为水分子，好神奇【心情：开心｜查到了】"
    result = c.activities.perform(prep())
    reply, images = c.finish_activity(result, NOW, owner_id=c.owner_id, owner_channel=CHANNEL)
    assert reply.text.startswith("诶，我刚查到") and images == []
    assert "水分子" in c.sub.requests[-1][-1].content  # 分享时她知道查到了什么
    acts = c.life.days.entries(NOW, "activities")
    assert acts[-1]["title"] == "查了雪花为什么是六角形"
    life_events = [e for e in c.log.iter_events() if e.channel == "self:life"]
    assert life_events[-1].kind == "action" and "六方晶系" in life_events[-1].content
    assert c.life.mood(NOW).word == "开心"
    # 今天的状态里有这件事
    assert "（你自己）查了雪花为什么是六角形" in c.life.sections(c.owner_id, CHANNEL, NOW)[0]


def test_finish_write_saves_piece(c):
    c.fake_life.choice = {"kind": "write", "topic": "初雪", "why": "想写"}
    result = c.activities.perform(prep())
    c.finish_activity(result, NOW)
    saved = list((c.layout.self_dir / "writings").glob("*.md"))
    assert len(saved) == 1 and "没说完的字" in saved[0].read_text("utf-8")
    assert c.life.days.entries(NOW, "activities")[-1]["title"] == "写了《初雪》"


def test_finish_draw_sends_image_to_owner(c):
    c.fake_life.choice = {"kind": "draw", "topic": "窗边的雪", "why": "想画"}
    c.brain.reply = "画了一张窗边的雪，给你看看"
    result = c.activities.perform(prep())
    reply, images = c.finish_activity(result, NOW, owner_id=c.owner_id, owner_channel=CHANNEL)
    assert reply is not None and len(images) == 1 and images[0].exists()
    owner_events = [e for e in c.log.iter_events() if e.channel == CHANNEL]
    assert owner_events[-1].meta.get("images")


def test_no_share_when_owner_busy(c):
    owner_says(c, "我在忙", NOW - timedelta(minutes=5))
    result = c.activities.perform(prep())
    reply, _ = c.finish_activity(result, NOW, owner_id=c.owner_id, owner_channel=CHANNEL)
    assert reply is None
    assert c.life.days.entries(NOW, "activities")  # 事情照样记下


# ---- QQ ----


class FakeClient:
    def __init__(self) -> None:
        self.sent = []

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        return {"message_id": 1}


def test_bot_greets_and_shares_with_owner_only(c):
    clock = {"now": c.life.owner.greeting_time(NOW, "morning", "08:30") + timedelta(minutes=1)}
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=["4000"])
    bot = QQBot(c, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: clock["now"])
    c.config.life.activities.enabled = False
    asyncio.run(_tick(bot))
    sends = [p for a, p in bot.client.sent if a.startswith("send_")]
    assert [a for a, _ in bot.client.sent] == ["send_private_msg"]
    assert sends[0]["user_id"] == int(OWNER)

    c.config.life.activities.enabled = True
    c.config.life.activities.draw = False
    c.config.life.activities.write = False
    c.config.life.activities.chat_owner = False
    clock["now"] = NOW
    c.brain.reply = "我刚查到雪花为什么是六角形，好神奇"
    asyncio.run(_tick(bot))
    actions = [a for a, _ in bot.client.sent]
    assert actions == ["send_private_msg", "send_private_msg"]  # 只发给主人，不发群
    bot.close()


async def _tick(bot):
    await bot.life_tick()
    await bot.drain()


def test_owner_chat_config_validates_clock():
    with pytest.raises(ValueError):
        OwnerChatConfig(morning="8点")
    assert OwnerChatConfig(morning="8:05").morning == "08:05"
