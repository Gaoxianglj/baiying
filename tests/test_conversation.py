"""C-01 消息合并、C-02 允许沉默、C-03 群聊对话延续、C-04 引用解析。"""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest

from companion.bodies.qq.bot import QQBot
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, ME, FRIEND, OTHER, GROUP = "1000", "2000", "3000", "3001", "4000"
T0 = datetime(2026, 10, 24, 22, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.messages: dict[int, dict] = {}

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        if action == "get_msg":
            return self.messages.get(params["message_id"])
        return {"message_id": 9000 + len(self.sent)}

    def sends(self):
        return [p for a, p in self.sent if a.startswith("send_")]


class Brain:
    def __init__(self) -> None:
        self.reply = "好呀"
        self.prompts: list[str] = []
        self.users: list[str] = []

    def __call__(self, messages, json_mode):
        self.prompts.append(messages[0].content)
        self.users.append("\n".join(m.content for m in messages[1:] if m.role == "user"))
        return self.reply


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = Brain()
    gateway, _ = scripted_gateway(brain)
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    clock = {"now": T0}
    cfg = QQConfig(
        bot_account=BOT,
        groups=[GROUP],
        unsolicited_probability=0,
        debounce_private_seconds=0.15,
        debounce_group_seconds=0.15,
    )
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1),
              clock=lambda: clock["now"])  # fmt: skip
    b.brain, b.clock = brain, clock
    yield b
    b.close()
    companion.close()


def msg(text, *, user=ME, group=None, at_bot=False, reply=None, mid=1, name="祥", sticker=False):
    segs = []
    if reply:
        segs.append({"type": "reply", "data": {"id": reply}})
    if at_bot:
        segs.append({"type": "at", "data": {"qq": BOT}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    if sticker:
        segs.append({"type": "face", "data": {"id": "1"}})
    ev = {"post_type": "message", "self_id": int(BOT), "user_id": int(user), "message_id": mid,
          "message_type": "group" if group else "private", "message": segs,
          "sender": {"nickname": name}}  # fmt: skip
    if group:
        ev["group_id"] = int(group)
    return ev


def run(bot, *events, gap=0.0):
    async def go():
        for ev in events:
            await bot.handle_event(ev)
            if gap:
                await asyncio.sleep(gap)
        await bot.drain()

    asyncio.run(go())


# ---------- C-01 消息合并 ----------


def test_text_then_sticker_gets_one_reply(bot):
    run(bot, msg("可以可以，听歌，记住了", mid=1), msg(None, sticker=True, mid=2), gap=0.05)
    assert len(bot.client.sends()) == 1
    assert len(bot.brain.prompts) == 1
    # 两条都在这一轮的上下文里
    assert "听歌" in bot.brain.users[0] and "[表情]" in bot.brain.users[0]
    assert "当成一轮，只回一次" in bot.brain.prompts[0]


def test_separate_turns_after_pause(bot):
    run(bot, msg("第一句", mid=1))
    run(bot, msg("第二句", mid=2))
    assert len(bot.client.sends()) == 2


# ---------- C-02 允许沉默 ----------


def test_silent_reply_sends_nothing(bot):
    bot.brain.reply = "【不回复】"
    run(bot, msg("嗯嗯", mid=1))
    assert bot.client.sends() == []
    mine = [e for e in bot.c.log.iter_events() if e.actor.person_id == "self"]
    assert mine == []  # 沉默不记成她说了话


def test_silence_does_not_use_rate_slot(bot):
    bot.cfg.max_replies_per_minute = 1
    bot.brain.reply = "【不回复】"
    run(bot, msg("嗯", mid=1))
    bot.brain.reply = "好呀"
    run(bot, msg("在吗", mid=2))
    assert len(bot.client.sends()) == 1


# ---------- C-03 群聊对话延续 ----------


def test_conversation_continues_without_at(bot):
    run(bot, msg("白璎在吗", group=GROUP, at_bot=True, user=FRIEND, mid=1))
    assert len(bot.client.sends()) == 1
    # 同一个人不再 @，接着说：交给模型（可沉默）
    bot.clock["now"] = T0 + timedelta(seconds=30)
    run(bot, msg("刚才那首歌你听了吗", group=GROUP, user=FRIEND, mid=2))
    assert len(bot.client.sends()) == 2
    assert "你们刚刚还在聊" in bot.brain.prompts[-1]
    # 别人插话：更严格的提示
    bot.clock["now"] = T0 + timedelta(seconds=60)
    run(bot, msg("我也想听", group=GROUP, user=OTHER, mid=3, name="小红"))
    assert "不是冲你来的" in bot.brain.prompts[-1]


def test_window_refreshes_and_expires(bot):
    run(bot, msg("白璎", group=GROUP, at_bot=True, user=FRIEND, mid=1))
    bot.clock["now"] = T0 + timedelta(seconds=80)
    run(bot, msg("还在吗", group=GROUP, user=FRIEND, mid=2))  # 窗口内，回复后刷新
    bot.clock["now"] = T0 + timedelta(seconds=160)  # 距上次回复 80 秒，仍在窗口内
    run(bot, msg("嘿", group=GROUP, user=FRIEND, mid=3))
    assert len(bot.client.sends()) == 3
    bot.clock["now"] = T0 + timedelta(seconds=300)  # 超过 90 秒没说话
    run(bot, msg("人呢", group=GROUP, user=FRIEND, mid=4))
    assert len(bot.client.sends()) == 3


def test_others_can_be_excluded(bot):
    bot.cfg.others_join_conversation = False
    run(bot, msg("白璎", group=GROUP, at_bot=True, user=FRIEND, mid=1))
    run(bot, msg("路过", group=GROUP, user=OTHER, mid=2))
    assert len(bot.client.sends()) == 1


# ---------- C-04 引用解析 ----------


def test_quote_of_someone_else(bot):
    bot.client.messages[77] = {
        "sender": {"user_id": int(OTHER), "nickname": "小红"},
        "message": [{"type": "text", "data": {"text": "周末去爬山吗"}}],
    }
    run(bot, msg("你觉得呢", reply="77", mid=5))
    theirs = [e.content for e in bot.c.log.iter_events() if e.actor.person_id != "self"]
    assert theirs == ["你觉得呢（引用了小红的话：周末去爬山吗）"]


def test_quote_of_her_own_message(bot):
    bot.client.messages[88] = {
        "sender": {"user_id": int(BOT), "nickname": "白璎"},
        "message": [{"type": "text", "data": {"text": "我更喜欢雨天"}}],
    }
    run(bot, msg("为什么", reply="88", mid=6))
    theirs = [e.content for e in bot.c.log.iter_events() if e.actor.person_id != "self"]
    assert theirs == ["为什么（引用了你说的：我更喜欢雨天）"]
