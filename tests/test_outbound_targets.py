"""发出去的东西只能到该去的地方（2026-10-28 私聊试语音时，语音被发到了群里）。

把能让她“发东西”的场景都跑一遍，记下每一次对 NapCat 的调用，检查：
- 会产生可见效果的动作（send_ / upload_ / set_），目标只能是触发它的那个聊天；
  主动开口只能是主人私聊；
- 只读动作（get_ / fetch_ / ocr_）里不能有已知会顺手发消息的（get_ai_record）；
- 补离线消息只读不发；不在允许名单里的群，什么都不发。
"""

import asyncio
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.bodies.qq.bot import QQBot
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway
from tests.test_qq import FakeClient

BOT, OWNER, FRIEND, GROUP, OTHER_GROUP = "1000", "2000", "3000", "4000", "5000"
T0 = datetime(2026, 10, 29, 12, tzinfo=UTC)

SIDE_EFFECT = re.compile(r"^(send_|upload_|set_)")
READ_ONLY = re.compile(r"^(get_|fetch_|ocr_)")
# 名字像只读、实际会发消息的动作：NapCat 的 get_ai_record 生成语音时就把它发到了群里
FORBIDDEN = {"get_ai_record"}
# 她回复里能写的所有小动作，一次全写上
EVERYTHING = "好呀【回应：😂】【戳一戳】【语音：晚安】" + "很长的一段话。" * 60
BOT_SOURCE = Path(__file__).resolve().parents[1] / "src" / "companion" / "bodies" / "qq" / "bot.py"


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = {"reply": EVERYTHING}
    gateway, _ = scripted_gateway(lambda messages, json_mode: brain["reply"])
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=[GROUP],
                   debounce_private_seconds=0, debounce_group_seconds=0,
                   unsolicited_probability=0)  # fmt: skip
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: T0)
    b.brain = brain
    companion.index.set_meta(f"announced:group:{GROUP}", "x")  # 群说明已经发过
    yield b
    b.close()
    companion.close()


def msg(text, *, user=FRIEND, group=None, at_bot=False, mid=1):
    segs = [{"type": "at", "data": {"qq": BOT}}] if at_bot else []
    segs.append({"type": "text", "data": {"text": text}})
    e = {"post_type": "message", "self_id": int(BOT), "user_id": int(user), "message_id": mid,
         "message_type": "group" if group else "private", "message": segs,
         "sender": {"nickname": "小明"}}  # fmt: skip
    if group:
        e["group_id"] = int(group)
    return e


def poke(*, user=FRIEND, group=None):
    e = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "self_id": int(BOT),
         "user_id": int(user), "target_id": int(BOT), "time": int(T0.timestamp())}  # fmt: skip
    if group:
        e["group_id"] = int(group)
    return e


def run(bot, *events):
    async def go():
        for e in events:
            await bot.handle_event(e)
            await bot.drain()

    asyncio.run(go())


def side_effects(bot):
    return [(a, p) for a, p in bot.client.sent if SIDE_EFFECT.match(a)]


def assert_only_private(bot, user):
    calls = side_effects(bot)
    assert calls, "这个场景应该发了东西"
    for action, params in calls:
        assert "group_id" not in params, (action, params)
        if "user_id" in params:
            assert str(params["user_id"]) == user, (action, params)
        else:  # 表情回应按消息编号，消息本身就在这个私聊里
            assert action == "set_msg_emoji_like", (action, params)


def assert_only_group(bot, group):
    calls = side_effects(bot)
    assert calls, "这个场景应该发了东西"
    for action, params in calls:
        if action == "set_msg_emoji_like":
            continue
        assert str(params.get("group_id")) == group, (action, params)
        assert not action.startswith("send_private") and action != "upload_private_file"


def assert_nothing_forbidden(bot):
    used = {a for a, _ in bot.client.sent}
    assert not used & FORBIDDEN
    assert all(SIDE_EFFECT.match(a) or READ_ONLY.match(a) for a in used), used


def test_code_never_calls_forbidden_actions():
    code = BOT_SOURCE.read_text("utf-8")
    for action in FORBIDDEN:
        assert not re.search(rf'call\(\s*"{action}"', code), f"bot.py 不应调用 {action}"


def test_private_chat_stays_private(bot):
    run(bot, msg("晚安呀", mid=7), poke())
    assert_only_private(bot, FRIEND)
    assert_nothing_forbidden(bot)
    assert "send_group_ai_record" not in {a for a, _ in bot.client.sent}  # 私聊的语音改发文字


def test_group_chat_stays_in_group(bot):
    run(bot, msg("晚安呀", group=GROUP, at_bot=True, mid=8), poke(group=GROUP))
    assert_only_group(bot, GROUP)
    assert_nothing_forbidden(bot)


def test_poke_only_back_at_who_poked(bot):
    run(bot, msg("在吗", group=GROUP, at_bot=True))  # 没人戳她，她写了【戳一戳】也不戳
    assert "send_poke" not in {a for a, _ in bot.client.sent}
    bot.client.sent.clear()
    run(bot, msg("hi", user="3001", group=GROUP), poke(user="3002", group=GROUP))
    pokes = [p for a, p in bot.client.sent if a == "send_poke"]
    assert pokes == [{"user_id": "3002", "group_id": GROUP}]


def test_unknown_group_gets_nothing(bot):
    run(bot, msg("白璎在吗", group=OTHER_GROUP, at_bot=True), poke(group=OTHER_GROUP))
    assert bot.client.sent == []


def test_backfill_only_reads(bot):
    when = int((T0 - timedelta(minutes=5)).timestamp())
    bot.client.replies["get_group_msg_history"] = {"messages": [{
        "message_id": 9, "user_id": int(FRIEND), "time": when,
        "message_type": "group", "group_id": int(GROUP), "sender": {"nickname": "小明"},
        "message": [{"type": "text", "data": {"text": "白璎你在吗"}}]}]}  # fmt: skip
    asyncio.run(bot.backfill())
    assert side_effects(bot) == []


def test_reminder_goes_only_back_to_who_set_it(bot):
    book = bot.c.reminders
    pid = bot.c.people.ensure("qq", FRIEND, "小明")
    book.add(f"private:{FRIEND}", pid, "开会", T0 - timedelta(minutes=1), None)
    asyncio.run(bot.reminder_tick())
    assert_only_private(bot, FRIEND)
    assert_nothing_forbidden(bot)
    assert "send_poke" not in {a for a, _ in bot.client.sent}  # 提醒里写了小动作也不做


def test_reminder_with_bad_channel_is_dropped(bot):
    pid = bot.c.people.ensure("qq", FRIEND, "小明")
    bot.c.reminders.add(f"group:{GROUP}", pid, "开会", T0 - timedelta(minutes=1), None)
    asyncio.run(bot.reminder_tick())
    assert bot.client.sent == [] and bot.c.reminders.pending() == []


def test_proactive_greeting_goes_only_to_owner(bot):
    bot.brain["reply"] = "早安【语音：早安】【回应：😂】【戳一戳】"
    bot.c.config.life.activities.enabled = False
    owner = bot.c.people.ensure("qq", OWNER, "祥")
    t = bot.c.life.owner.greeting_time(T0, "morning", "08:30") + timedelta(minutes=1)
    bot.now = lambda: t
    asyncio.run(bot.life_tick())
    assert_only_private(bot, OWNER)
    assert_nothing_forbidden(bot)
    assert not any(a in ("send_poke", "set_msg_emoji_like") for a, _ in bot.client.sent)
    assert owner
