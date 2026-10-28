"""补离线群消息、表情回应、戳一戳、语音、图里的字、长内容打包成聊天记录。"""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest

from companion.bodies.qq.bot import QQBot, split_long
from companion.config import QQConfig, load_config
from companion.core.actions import emoji_id, split_actions
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway
from tests.test_qq import FakeClient

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"
T0 = datetime(2026, 10, 28, 12, tzinfo=UTC)


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = {"reply": "嗯嗯"}
    gateway, chat = scripted_gateway(lambda messages, json_mode: brain["reply"])
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    clock = {"now": T0}
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=[GROUP],
                   debounce_private_seconds=0, debounce_group_seconds=0,
                   unsolicited_probability=0)  # fmt: skip
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1),
              clock=lambda: clock["now"])  # fmt: skip
    b.brain, b.chat, b.clock = brain, chat, clock
    yield b
    b.close()
    companion.close()


def ev(segs, *, user=FRIEND, group=None, mid=1, name="小明", time=None):
    e = {"post_type": "message", "self_id": int(BOT), "user_id": int(user), "message_id": mid,
         "message_type": "group" if group else "private", "message": segs,
         "sender": {"nickname": name}}  # fmt: skip
    if group:
        e["group_id"] = int(group)
    if time:
        e["time"] = int(time.timestamp())
    return e


def text(t):
    return {"type": "text", "data": {"text": t}}


def run(bot, *events):
    async def go():
        for e in events:
            await bot.handle_event(e)
            await bot.drain()

    asyncio.run(go())


def actions(bot):
    return [a for a, _ in bot.client.sent]


def contents(bot, mine=False):
    return [e.content for e in bot.c.log.iter_events() if (e.actor.person_id == "self") == mine]


# ---- 标记 ----


def test_split_actions():
    t, a = split_actions("好呀【回应：😂】【戳一戳】【语音：晚安，做个好梦】")
    assert t == "好呀" and a.reaction == "😂" and a.poke and a.voice == "晚安，做个好梦"
    t, a = split_actions("【回应：好】嗯")  # 不是表情符号的不算回应
    assert t == "嗯" and a.reaction is None and not a
    assert emoji_id("👍") == "128077" and emoji_id("❤️") == "10084" and emoji_id("a") is None


def test_split_long():
    parts = split_long("第一段\n" + "长" * 450 + "\n短")
    assert parts[0] == "第一段" and len(parts[1]) == 400 and parts[-1].endswith("短")
    assert all(len(p) <= 400 for p in parts)


def test_qq_rules_only_on_qq(bot):
    def system(body, channel):
        return bot.c.context.build(person_id="x", body=body, channel=channel, query="在吗",
                                   now=T0).system_prompt  # fmt: skip

    assert "【回应：" in system("qq", "private:1") and "【语音：" in system("qq", "group:4000")
    assert "【语音：" not in system("qq", "private:1")  # 语音只能发在群里
    assert "【回应：" not in system("cli", "cli:x")
    bot.c.context.qq.voice_out = False
    assert "【语音：" not in system("qq", "group:4000")


# ---- 表情回应 ----


def test_reaction_only(bot):
    bot.brain["reply"] = "【不回复】【回应：😂】"
    run(bot, ev([text("哈哈哈笑死")], mid=77))
    assert actions(bot) == ["set_msg_emoji_like"]
    params = bot.client.sent[0][1]
    assert params["message_id"] == 77 and params["emoji_id"] == "128514"
    assert contents(bot, mine=True) == ["[给这条消息点了 😂]"]


def test_reply_with_reaction(bot):
    bot.brain["reply"] = "太好了【回应：🎉】"
    run(bot, ev([text("我考过了")], mid=5))
    assert actions(bot) == ["send_private_msg", "set_msg_emoji_like"]


# ---- 戳一戳 ----


def poke(user=FRIEND, group=None, target=BOT):
    e = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "self_id": int(BOT),
         "user_id": int(user), "target_id": int(target), "time": int(T0.timestamp())}  # fmt: skip
    if group:
        e["group_id"] = int(group)
    return e


def test_poked_she_answers_and_pokes_back(bot):
    bot.brain["reply"] = "干嘛戳我【戳一戳】"
    run(bot, poke(group=GROUP))
    assert actions(bot) == ["send_group_msg", "send_poke"]
    assert bot.client.sent[1][1] == {"user_id": FRIEND, "group_id": GROUP}
    assert "（戳了戳你）" in contents(bot)
    run(bot, poke(group=GROUP))  # 冷却中：不理
    assert len(actions(bot)) == 2
    bot.clock["now"] = T0 + timedelta(minutes=2)
    run(bot, poke(group=GROUP))
    assert len(actions(bot)) == 4


def test_poke_on_someone_else_ignored(bot):
    run(bot, poke(target="5555", group=GROUP), poke(group="9999"))
    assert actions(bot) == [] and contents(bot) == []


# ---- 语音 ----


def test_voice_message_is_transcribed(bot):
    bot.client.replies["fetch_ptt_text"] = {"text": "今天好冷啊"}
    run(bot, ev([{"type": "record", "data": {"file": "a.amr"}}], mid=9))
    assert ("fetch_ptt_text", {"message_id": 9}) in bot.client.sent
    assert contents(bot) == ["[语音：今天好冷啊]"]


def test_group_voice_not_for_her_is_not_heard(bot):
    run(bot, ev([{"type": "record", "data": {"file": "a.amr"}}], group=GROUP))
    assert "fetch_ptt_text" not in actions(bot) and contents(bot) == ["[语音（没听）]"]


def test_she_speaks_with_voice_in_group(bot):
    bot.brain["reply"] = "【语音：晚安，做个好梦】"
    run(bot, ev([{"type": "at", "data": {"qq": BOT}}, text(" 晚安")], group=GROUP))
    assert actions(bot) == ["send_group_ai_record"]
    params = bot.client.sent[0][1]
    assert params["character"] == "lucy-voice-f37" and params["text"] == "晚安，做个好梦"
    assert contents(bot, mine=True) == ["（用语音说）晚安，做个好梦"]


def test_no_voice_in_private(bot):
    """私聊不发语音：get_ai_record 会把语音发到群里（2026-10-28 出过事），一律改发文字。"""
    bot.brain["reply"] = "【语音：早安】"
    run(bot, ev([text("早")]))
    assert actions(bot) == ["send_private_msg"]
    assert bot.client.texts("send_private_msg") == ["早安"]


def test_group_voice_failure_falls_back_to_text(bot):
    from companion.bodies.qq.onebot import OneBotError

    bot.brain["reply"] = "【语音：晚安】"
    bot.client.replies["send_group_ai_record"] = OneBotError("不行")
    run(bot, ev([{"type": "at", "data": {"qq": BOT}}, text(" 晚安")], group=GROUP))
    assert actions(bot) == ["send_group_ai_record", "send_group_msg"]
    assert bot.client.texts("send_group_msg") == ["晚安"]


# ---- 图里的字 ----


def test_unseen_group_image_gets_ocr(bot):
    bot.client.replies["ocr_image"] = {"texts": [{"text": "明天 3 点"}, {"text": "开会"}]}
    img = {"type": "image", "data": {"url": "http://img/1.jpg", "file": "1.jpg"}}
    sticker = {"type": "image", "data": {"url": "http://img/2.gif", "sub_type": "1"}}
    run(bot, ev([img, sticker], group=GROUP))
    assert [p for a, p in bot.client.sent if a == "ocr_image"] == [{"image": "http://img/1.jpg"}]
    assert contents(bot) == ["[图片（没看，图里的字：明天 3 点 开会）][表情包（没看）]"]


# ---- 长内容 ----


def test_long_reply_sent_as_forward(bot):
    bot.brain["reply"] = "这是一段很长的话。" * 40
    run(bot, ev([text("讲讲")]))
    assert actions(bot) == ["send_private_forward_msg"]
    nodes = bot.client.sent[0][1]["messages"]
    assert nodes[0]["data"]["nickname"] == "白璎" and nodes[0]["data"]["user_id"] == BOT


def test_forward_failure_falls_back(bot):
    from companion.bodies.qq.onebot import OneBotError

    bot.brain["reply"] = "长" * 400
    bot.client.replies["send_private_forward_msg"] = OneBotError("不支持")
    run(bot, ev([text("讲讲")]))
    assert actions(bot) == ["send_private_forward_msg", "send_private_msg"]


# ---- 补离线期间的群消息 ----


def history_msg(mid, uid, t, words, name="小红"):
    return {"message_id": mid, "user_id": int(uid), "time": int(t.timestamp()),
            "message_type": "group", "group_id": int(GROUP),
            "sender": {"card": name, "nickname": name}, "message": [text(words)]}  # fmt: skip


def test_backfill_records_missed_messages_without_replying(bot):
    run(bot, ev([text("我先睡了")], group=GROUP, mid=100, time=T0 - timedelta(hours=3)))
    before = len(bot.client.sent)
    bot.client.replies["get_group_msg_history"] = {"messages": [
        history_msg(100, FRIEND, T0 - timedelta(hours=3), "我先睡了"),  # 已经记过
        history_msg(99, FRIEND, T0 - timedelta(hours=4), "更早的"),  # 比最后一条记录早
        history_msg(101, BOT, T0 - timedelta(hours=2), "她自己说的"),
        history_msg(102, "3001", T0 - timedelta(hours=1), "大家明天几点出发"),
        history_msg(103, "3002", T0 - timedelta(minutes=30), "九点吧", name="小刚"),
    ]}  # fmt: skip
    n = asyncio.run(bot.backfill())
    assert n == 2
    assert contents(bot)[-2:] == ["大家明天几点出发", "九点吧"]
    events = list(bot.c.log.iter_events())
    assert events[-1].meta["backfill"] and events[-1].actor.display_name == "小刚"
    assert [a for a, _ in bot.client.sent[before:]] == ["get_group_msg_history"]  # 只记不回
    assert asyncio.run(bot.backfill()) == 0  # 再补一次不重复


def test_backfill_on_connect(bot):
    bot.client.replies["get_group_msg_history"] = {"messages": [
        history_msg(5, FRIEND, T0 - timedelta(minutes=10), "有人吗")]}  # fmt: skip
    bot.cfg.groups = [GROUP]
    bot.c.index.set_meta(f"announced:group:{GROUP}", "x")
    run(bot, {"post_type": "meta_event", "meta_event_type": "lifecycle", "self_id": int(BOT)})
    assert contents(bot) == ["有人吗"]
