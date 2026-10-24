import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from websockets.asyncio.server import serve

from companion.bodies.qq.bot import QQBot
from companion.bodies.qq.message import parse_segments, text_segments
from companion.bodies.qq.onebot import OneBotClient
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"
T0 = datetime(2026, 10, 23, 12, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        return {"message_id": 9000 + len(self.sent)}

    def texts(self, action=None):
        return [
            "".join(s["data"].get("text", "") for s in p["message"] if s["type"] == "text")
            for a, p in self.sent
            if action is None or a == action
        ]


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    gateway, chat = scripted_gateway(lambda messages, json_mode: "嗯嗯，我在呢。")
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=[GROUP])
    clock = {"now": T0}
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1),
              clock=lambda: clock["now"])  # fmt: skip
    b.chat, b.clock = chat, clock
    yield b
    b.close()
    companion.close()


def msg(text=None, *, user=FRIEND, group=None, at_bot=False, reply=None, mid=1, name="小明"):
    segs = []
    if reply:
        segs.append({"type": "reply", "data": {"id": reply}})
    if at_bot:
        segs.append({"type": "at", "data": {"qq": BOT}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    ev = {
        "post_type": "message", "self_id": int(BOT), "user_id": int(user), "message_id": mid,
        "message_type": "group" if group else "private", "message": segs,
        "sender": {"nickname": name},
    }  # fmt: skip
    if group:
        ev["group_id"] = int(group)
    return ev


def run(bot, *events):
    async def go():
        for ev in events:
            await bot.handle_event(ev)

    asyncio.run(go())


def test_private_message_gets_reply(bot):
    run(bot, msg("在吗"))
    assert bot.client.sent[0][0] == "send_private_msg"
    assert bot.client.texts() == ["嗯嗯，我在呢。"]
    contents = [e.content for e in bot.c.log.iter_events()]
    assert contents == ["在吗", "嗯嗯，我在呢。"]


def test_group_chatter_is_remembered_but_not_answered(bot):
    bot.cfg.unsolicited_probability = 0
    run(bot, msg("今天好热", group=GROUP))
    assert bot.client.sent == []
    assert [e.content for e in bot.c.log.iter_events()] == ["今天好热"]


def test_group_mention_alias_and_reply_to_her(bot):
    run(bot, msg("早上好", group=GROUP, at_bot=True, mid=11))
    action, params = bot.client.sent[0]
    assert action == "send_group_msg" and params["group_id"] == int(GROUP)
    assert params["message"][0] == {"type": "reply", "data": {"id": "11"}}
    run(bot, msg("白璎你看这个", group=GROUP, mid=12))
    her_id = str(9000 + len(bot.client.sent))
    run(bot, msg("哈哈", group=GROUP, reply=her_id, mid=13))
    assert len(bot.client.sent) == 3


def test_other_groups_and_self_ignored(bot):
    run(bot, msg("hi", group="999"), msg("hi", user=BOT))
    assert bot.client.sent == [] and list(bot.c.log.iter_events()) == []


def test_rate_limit(bot):
    bot.cfg.max_replies_per_minute = 2
    run(bot, *(msg(f"第{i}条", mid=i) for i in range(4)))
    assert len(bot.client.sent) == 2
    assert len(list(bot.c.log.iter_events())) == 4 + 2  # 超额的也都记下了


def test_chime_in_only_when_topic_matches_memory(bot):
    bot.cfg.unsolicited_probability = 1.0
    bot.cfg.unsolicited_min_cosine = 0.5
    run(bot, msg("毫不相干的话题啊", group=GROUP))
    assert bot.client.sent == []
    # 让她先有一段关于红石的记忆
    run(bot, msg("/记住 我在做红石自动门", group=GROUP))
    sent_before = len(bot.client.sent)
    bot.clock["now"] = T0 + timedelta(hours=1)
    run(bot, msg("我在做红石自动门呢", group=GROUP))
    assert len(bot.client.sent) == sent_before + 1
    # 插话之后有冷却
    run(bot, msg("我在做红石自动门呢", group=GROUP))
    assert len(bot.client.sent) == sent_before + 1


def test_remember_command_pins_fact(bot):
    run(bot, msg("/记住 我对花生过敏"))
    assert bot.client.texts() == ["嗯，记下了，不会忘的。"]
    pid = bot.c.people.resolve("qq", FRIEND)
    (fact,) = bot.c.index.current_facts(pid)
    assert fact.pinned and "花生" in fact.statement


def test_forget_flow_deletes_memory_and_messages(bot):
    run(bot, msg("/记住 我的生日是五月三号"), msg("/忘掉 生日"))
    offer = bot.client.texts()[-1]
    assert "1. " in offer and "生日" in offer
    run(bot, msg("/确认忘掉 1"))
    assert "已经忘掉了" in bot.client.texts()[-1]
    pid = bot.c.people.resolve("qq", FRIEND)
    assert bot.c.index.current_facts(pid) == []
    assert not any("五月三号" in e.content for e in bot.c.log.iter_events())


def test_bind_needs_owner_confirmation(bot):
    run(bot, msg("/绑定 minecraft Steve"))
    request = next(p for a, p in bot.client.sent if p.get("user_id") == int(OWNER))
    code = request["message"][0]["data"]["text"].split("/确认 ")[1]
    assert bot.c.people.resolve("minecraft", "Steve") is None
    run(bot, msg(f"/确认 {code}", user=OWNER, name="主人"))
    assert bot.c.people.resolve("minecraft", "Steve") == bot.c.people.resolve("qq", FRIEND)


def test_non_owner_cannot_confirm(bot):
    run(bot, msg("/绑定 minecraft Steve"), msg("/确认 0000", user="5555"))
    assert bot.c.people.resolve("minecraft", "Steve") is None


def test_group_notice_sent_once(bot):
    lifecycle = {"post_type": "meta_event", "meta_event_type": "lifecycle", "self_id": int(BOT)}
    run(bot, lifecycle, lifecycle)
    notices = bot.client.texts("send_group_msg")
    assert len(notices) == 1 and "我是白璎，一个 AI" in notices[0] and "/忘掉" in notices[0]


def test_parse_segments():
    inc = parse_segments(
        [
            {"type": "reply", "data": {"id": "77"}},
            {"type": "at", "data": {"qq": BOT}},
            {"type": "text", "data": {"text": "看 "}},
            {"type": "at", "data": {"qq": "3000"}},
            {"type": "image", "data": {}},
        ],
        BOT,
        {"3000": "小明"},
    )
    assert inc.mentioned_self and inc.reply_to == "77" and inc.at == ["3000"]
    assert inc.text == "看 @小明 ⟦图0⟧" and len(inc.images) == 1
    assert inc.fill_images(["[图片：猫]"]) == "看 @小明 [图片：猫]"
    assert text_segments("好", "5")[0]["type"] == "reply"


def test_onebot_client_keeps_retrying_when_rejected(caplog):
    async def reject(connection, request):
        return connection.respond(401, "Unauthorized")

    async def main():
        async def on_event(ev):
            pass

        async with serve(lambda ws: None, "127.0.0.1", 0, process_request=reject) as srv:
            port = srv.sockets[0].getsockname()[1]
            client = OneBotClient(f"ws://127.0.0.1:{port}", "wrong", on_event)
            task = asyncio.create_task(client.run_forever(max_backoff=0.05))
            await asyncio.sleep(0.5)
            assert not task.done()  # 没有因为 401 崩溃
            task.cancel()

    asyncio.run(main())
    assert "HTTP 401" in caplog.text and "可能被别的程序占用" in caplog.text


def test_onebot_client_round_trip():
    received = []

    async def server(ws):
        await ws.send(json.dumps({"post_type": "message", "raw": "hello"}))
        req = json.loads(await ws.recv())
        await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 5},
                                  "echo": req["echo"]}))  # fmt: skip
        await asyncio.sleep(0.2)

    async def main():
        async def on_event(ev):
            received.append(ev)

        async with serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            client = OneBotClient(f"ws://127.0.0.1:{port}", "t", on_event)
            task = asyncio.create_task(client.run_once())
            await client.connected.wait()
            data = await client.call("send_private_msg", {"user_id": 1, "message": []})
            assert data == {"message_id": 5}
            await task

    asyncio.run(main())
    assert received == [{"post_type": "message", "raw": "hello"}]
