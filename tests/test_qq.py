import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from websockets.asyncio.server import serve

from companion.bodies.qq.bot import QQBot
from companion.bodies.qq.message import parse_segments, render_forward, text_segments
from companion.bodies.qq.onebot import OneBotClient, OneBotError
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"
T0 = datetime(2026, 10, 23, 12, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.replies: dict[str, object] = {}  # 动作 → 返回值（函数就按参数算；是异常就抛出）

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        if action in self.replies:
            reply = self.replies[action]
            if callable(reply):
                reply = reply(params)
            if isinstance(reply, Exception):
                raise reply
            return reply
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
    cfg = QQConfig(
        bot_account=BOT,
        owner_accounts=[OWNER],
        groups=[GROUP],
        debounce_private_seconds=0,
        debounce_group_seconds=0,
    )
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
            await bot.drain()

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
    sends = [a for a, _ in bot.client.sent if a.startswith("send_")]
    assert len(sends) == 3


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
    # 对话窗口（90 秒）过后、插话冷却（15 分钟）之内：不再插话
    bot.clock["now"] = T0 + timedelta(hours=1, minutes=3)
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
    assert inc.fill(["[图片：猫]"]) == "看 @小明 [图片：猫]"
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


# ---- 合并转发的聊天记录 ----

ORIGIN = 5555  # 记录原来所在的群（不该出现在她的记忆里）


def node(text=None, *, user=FRIEND, nickname="小明", card="", segs=None, mid=1):
    """和 NapCat 实际返回的一样：每条就是一个完整的消息事件。"""
    message = segs if segs is not None else [{"type": "text", "data": {"text": text}}]
    return {
        "self_id": int(BOT), "user_id": int(user), "time": 1761600000, "message_id": mid,
        "message_seq": mid, "real_id": mid, "real_seq": str(mid), "message_type": "group",
        "sender": {"user_id": int(user), "nickname": nickname, "card": card},
        "raw_message": text or "", "font": 14, "sub_type": "normal", "message": message,
        "message_format": "array", "post_type": "message", "group_id": ORIGIN,
        "group_name": "别的群",
    }  # fmt: skip


def forward_seg(nodes=None, fid="fw1"):
    data = {"id": fid}
    if nodes is not None:
        data["content"] = nodes
    return {"type": "forward", "data": data}


def forward_msg(seg, *, group=None, mid=50):
    ev = msg(None, group=group, mid=mid)
    ev["message"] = [seg]
    return ev


def test_render_forward_text_images_and_names():
    nodes = [
        node("今晚吃什么", card="群名片小明"),
        node("火锅！\n要辣的", user="3001", nickname="小红"),
        node(segs=[{"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}},
                   {"type": "text", "data": {"text": "看这家"}}], user="3002", nickname=""),
        node(segs=[{"type": "image", "data": {"sub_type": 1}}], nickname="小蓝"),
        node("你也来吗", user=BOT, nickname="白璎"),
    ]  # fmt: skip
    text = render_forward(nodes, BOT)
    assert text == (
        "（转发了一段聊天记录，共 5 条：\n"
        "群名片小明：今晚吃什么\n"
        "小红：火锅！ 要辣的\n"
        "3002：[图片（没看）]看这家\n"
        "小蓝：[表情包（没看）]\n"
        "你：你也来吗）"
    )
    assert str(ORIGIN) not in text and "别的群" not in text


def test_render_forward_nested_expands_one_level():
    deepest = forward_seg([node("最里面")])
    inner = forward_seg([node("里面一层", nickname="小红"), node(segs=[deepest])])
    text = render_forward([node("看这个"), node(segs=[inner], nickname="小蓝")], BOT)
    assert "小蓝：（转发了一段聊天记录，共 2 条：\n小红：里面一层\n小明：[聊天记录]）" in text
    assert "最里面" not in text
    # 套着的记录没带内容：不再去要，记作“[聊天记录]”
    assert render_forward([node(segs=[forward_seg()])], BOT).endswith("小明：[聊天记录]）")


def test_render_forward_truncates():
    many = render_forward([node(f"第{i}条消息") for i in range(50)], BOT)
    assert many.count("\n") <= 32 and "……（后面还有 20 条）" in many
    long = render_forward([node("字" * 500, mid=i) for i in range(20)], BOT)
    lines = long.split("\n")
    assert len(long) <= 1600 and all(len(line) <= 200 for line in lines)
    assert lines[1] == "小明：" + "字" * 196 + "…" and lines[-1] == "……（后面还有 13 条））"


def test_forward_in_group_is_read_into_memory_and_prompt(bot):
    bot.cfg.unsolicited_probability = 0
    seg = forward_seg([node("白璎今天好可爱"), node("[哈哈]", user="3001", nickname="小红")])
    run(bot, forward_msg(seg, group=GROUP))
    # 记录里提到她的名字，不算在叫她
    assert bot.client.sent == []
    content = [e.content for e in bot.c.log.iter_events()][-1]
    assert content == "（转发了一段聊天记录，共 2 条：\n小明：白璎今天好可爱\n小红：[哈哈]）"

    run(bot, forward_msg(seg, mid=51))  # 私聊转发：她会回，记录内容进到提示词里
    assert bot.client.texts() == ["嗯嗯，我在呢。"]
    prompt = "".join(str(m.content) for m in bot.chat.requests[-1])
    assert "小明：白璎今天好可爱" in prompt
    assert all(a != "get_forward_msg" for a, _ in bot.client.sent)


def test_forward_without_content_asks_napcat(bot):
    bot.cfg.unsolicited_probability = 0
    bot.client.replies["get_forward_msg"] = {"messages": [node("补来的内容")]}
    run(bot, forward_msg(forward_seg(fid="abc"), group=GROUP))
    assert ("get_forward_msg", {"id": "abc"}) in bot.client.sent
    content = [e.content for e in bot.c.log.iter_events()][-1]
    assert content == "（转发了一段聊天记录，共 1 条：\n小明：补来的内容）"


def test_forward_unreadable(bot):
    bot.cfg.unsolicited_probability = 0
    bot.client.replies["get_forward_msg"] = OneBotError("消息不存在")
    run(bot, forward_msg(forward_seg(fid="gone"), group=GROUP))
    bot.client.replies["get_forward_msg"] = {"messages": []}
    run(bot, forward_msg(forward_seg(fid="empty"), group=GROUP, mid=51))
    assert [e.content for e in bot.c.log.iter_events()] == ["[聊天记录（没看）]"] * 2


def test_quoted_forward_stays_short(bot):
    bot.client.replies["get_msg"] = {
        "sender": {"user_id": int(FRIEND), "nickname": "小明"},
        "message": [forward_seg([node("很长的一段记录")])],
    }
    run(bot, msg("你怎么看", reply="77"))
    contents = [e.content for e in bot.c.log.iter_events()]
    assert contents[0] == "你怎么看（引用了小明的话：[聊天记录]）"


# ---- 她发的 @ 变成真正的 @ ----

FORTUNE_BOT = "2854197266"


def at(qq):
    return {"type": "at", "data": {"qq": qq}}


def txt(text):
    return {"type": "text", "data": {"text": text}}


def test_mention_segments():
    names = {"小明": "3000", "小明同学": "3001", "全体成员": "3002", "all": "3003", "自己": BOT}

    def segs(text, reply_to=None):
        return text_segments(text, reply_to, mentions=names, self_id=BOT)

    assert segs(f"@{FORTUNE_BOT} /今日运势") == [at(FORTUNE_BOT), txt(" /今日运势")]
    assert segs("@小明同学 早，@小明你好") == [at("3001"), txt(" 早，"), at("3000"), txt("你好")]
    assert segs("@小红 在吗") == [txt("@小红 在吗")]  # 不认识的名字照原样发
    assert segs("@全体成员 开会了 @all") == [txt("@全体成员 开会了 @all")]
    assert segs(f"@自己 @{BOT} 和 a@123456.com") == [txt(f"@自己 @{BOT} 和 a@123456.com")]
    assert segs("@1234") == [txt("@1234")]  # 太短，不是 QQ 号
    assert segs("@小明") == [at("3000")]
    assert segs(f"@{FORTUNE_BOT} /今日运势", "77") == [
        {"type": "reply", "data": {"id": "77"}},
        at(FORTUNE_BOT),
        txt(" /今日运势"),
    ]
    # 私聊不给 mentions：原样一段文字
    assert text_segments(f"@{FORTUNE_BOT} /今日运势") == [txt(f"@{FORTUNE_BOT} /今日运势")]


def say(bot, text):
    bot.chat.handler = lambda messages, json_mode: text


def test_group_reply_mentions_recent_speaker_by_name(bot):
    bot.cfg.unsolicited_probability = 0
    fortune = msg("今日运势已更新", group=GROUP, user=FORTUNE_BOT, name="运势机器人", mid=10)
    say(bot, "@运势机器人 /今日运势")
    run(bot, fortune, msg("帮我测下运势", group=GROUP, at_bot=True, mid=11))
    action, params = bot.client.sent[-1]
    assert action == "send_group_msg"
    assert params["message"] == [
        {"type": "reply", "data": {"id": "11"}},
        at(FORTUNE_BOT),
        txt(" /今日运势"),
    ]
    # 记下的还是她说的原话
    assert [e.content for e in bot.c.log.iter_events()][-1] == "@运势机器人 /今日运势"

    # 重启后（内存里的名字没了）也能从最近的聊天记录里认出来
    bot._speakers.clear()
    run(bot, msg("再来一次", group=GROUP, at_bot=True, mid=12))
    assert bot.client.sent[-1][1]["message"][1] == at(FORTUNE_BOT)


def test_private_reply_stays_plain_text(bot):
    say(bot, f"@{FORTUNE_BOT} /今日运势")
    run(bot, msg("在吗"))
    action, params = bot.client.sent[-1]
    assert action == "send_private_msg"
    assert params["message"] == [txt(f"@{FORTUNE_BOT} /今日运势")]


def test_mention_send_failure_falls_back_to_text(bot):
    def group_send(params):
        if any(s["type"] == "at" for s in params["message"]):
            return OneBotError("群成员不存在")
        return {"message_id": 9999}

    bot.client.replies["send_group_msg"] = group_send
    say(bot, "@99999999 你好")
    run(bot, msg("白璎", group=GROUP, mid=11))
    sends = [p["message"] for a, p in bot.client.sent if a == "send_group_msg"]
    assert sends[-2][1] == at("99999999") and sends[-1][1] == txt("@99999999 你好")


def test_group_batch_keeps_primary_speaker_and_not_for_me_blocks_at_other(bot):
    # 1. @别人 再提白璎，不应算冲我来
    other_at = msg(" 白璎画得真好", group=GROUP, mid=21)
    other_at["message"] = [at("5000"), txt(" 白璎画得真好")]
    run(bot, other_at)
    assert bot.client.sent == []

    # 2. 主人 @白璎 后，防抖窗口内非主人插话：主发言人与 owner 权限仍归主人
    bot.cfg.debounce_group_seconds = 0.05
    seen_ctx = []
    orig_respond = bot.c.respond

    def spy_respond(incoming, *args, **kwargs):
        seen_ctx.append((incoming.actor.display_name, kwargs.get("skill_ctx").owner))
        return orig_respond(incoming, *args, **kwargs)

    bot.c.respond = spy_respond

    async def scenario():
        await bot.handle_event(
            msg("帮我看看", group=GROUP, user=OWNER, name="波奇", at_bot=True, mid=31)
        )
        # 非主人 10ms 后插话（先记下活跃或 other 消息）
        await bot.handle_event(
            msg("我也看看", group=GROUP, user="3009", name="路人", mid=32)
        )
        await bot.drain()

    asyncio.run(scenario())
    assert seen_ctx and seen_ctx[-1] == ("波奇", True)


def test_group_background_task_delivery_refreshes_partner_window(bot):
    from companion.skills.base import SkillCall, SkillResult

    bot.cfg.unsolicited_probability = 0
    # 1. 下单：主人 @白璎 发起后台任务，白璎先回一句排队中
    run(bot, msg("帮我全文翻译", group=GROUP, user=OWNER, name="波奇", at_bot=True, mid=40))
    sent_after_order = len(bot.client.sent)
    assert sent_after_order == 1

    # 2. 后台任务跑了 86 秒（快接近 90 秒，交付后再过 13 秒即超过下单时的 90 秒）
    pid = bot.c.people.resolve("qq", OWNER)
    assert pid is not None
    src = bot._source(
        msg("帮我全文翻译", group=GROUP, user=OWNER, name="波奇", at_bot=True, mid=40)
    )
    assert src is not None

    async def deliver_task():
        ctx = bot._make_skill_ctx(src, pid)
        task_id = bot.c.tasks.create("后台任务", "后台任务", "翻译", ctx)
        bot.c.tasks.update(task_id, state="completed", result="已译完")

        def finish_later():
            bot.clock["now"] = T0 + timedelta(seconds=86)
            return SkillResult(text="已译完", ok=True, report=True)

        bot._followups[src.channel] = bot.cfg.conversation_followups
        call = SkillCall(
            "后台任务", "后台任务", "翻译", True, 0.1, later=finish_later, task_id=task_id
        )
        await bot._run_later(src, ctx, call)

    asyncio.run(deliver_task())
    sent_after_delivery = len(bot.client.sent)
    assert sent_after_delivery > sent_after_order

    # 3. 交稿 13 秒后（距下单已 99 秒 > 90 秒），主人说“谢谢”不 @：仍应立刻接话
    bot.clock["now"] = T0 + timedelta(seconds=99)
    run(bot, msg("谢谢", group=GROUP, user=OWNER, name="波奇", mid=41))
    assert len(bot.client.sent) == sent_after_delivery + 1


def test_group_background_image_and_retry_notification_refresh_partner_window(bot, tmp_path):
    from companion.skills.base import SkillCall, SkillResult

    bot.cfg.unsolicited_probability = 0
    pid = bot.c.people.ensure("qq", OWNER, "波奇")
    img = tmp_path / "out.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    # 1. 后台画图耗时 120 秒交付：无需之前还在 90 秒窗口内，交图后立刻刷新发起人窗口
    bot.clock["now"] = T0 + timedelta(seconds=120)
    src = bot._source(msg("画一张", group=GROUP, user=OWNER, name="波奇", at_bot=True, mid=50))
    assert src is not None

    async def deliver_image():
        ctx = bot._make_skill_ctx(src, pid)
        bot.c.deliver = lambda body, channel, result, now: [img]
        call = SkillCall(
            "画画", "画画", "一株薄荷", True, 0.1, later=lambda: SkillResult(text="画好了", ok=True)
        )
        await bot._run_later(src, ctx, call)

    asyncio.run(deliver_image())
    sent_after_img = len(bot.client.sent)

    bot.clock["now"] = T0 + timedelta(seconds=135)
    run(bot, msg("好看，谢谢", group=GROUP, user=OWNER, name="波奇", mid=51))
    assert len(bot.client.sent) == sent_after_img + 1

    # 2. 模拟重启后 _task_notification_loop 补发（src.user_id 为空），按 person_id 还原发起人 QQ
    bot._partners.clear()
    bot.clock["now"] = T0 + timedelta(seconds=400)
    loop_src = bot._source(msg("x", group=GROUP, user=OWNER, mid=52))
    assert loop_src is not None

    async def deliver_notification():
        ctx = bot._make_skill_ctx(loop_src, pid)
        task_id = bot.c.tasks.create("后台任务", "后台任务", "整理笔记", ctx)
        bot.c.tasks.update(task_id, state="completed", result="整理好了")
        loop_src.user_id = ""
        await bot._send_task_result(loop_src, task_id, "任务完成：整理好了", [])

    asyncio.run(deliver_notification())
    sent_after_notify = len(bot.client.sent)

    bot.clock["now"] = T0 + timedelta(seconds=415)
    run(bot, msg("收到，辛苦了", group=GROUP, user=OWNER, name="波奇", mid=53))
    assert len(bot.client.sent) == sent_after_notify + 1
