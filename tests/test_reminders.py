"""定时提醒：只限私聊，到点她用自己的话提醒；重启不丢，发不出去会重试。"""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from companion.bodies.qq.bot import QQBot
from companion.bodies.qq.onebot import OneBotError
from companion.config import QQConfig, RemindersSkillConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.skills import SkillContext
from companion.skills.base import SkillError
from companion.skills.reminder import ReminderBook, Reminders, parse_when
from tests.fakes import FakeEmbedder, scripted_gateway
from tests.test_qq import FakeClient

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"


def local(*args) -> datetime:
    return datetime(*args).astimezone()


NOW = local(2026, 11, 2, 15, 0)  # 周一


# ---- 时间解析 ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-02 18:30", local(2026, 11, 2, 18, 30)),
        ("2026/11/3 8:05", local(2026, 11, 3, 8, 5)),
        ("2026年11月3日 08：05", local(2026, 11, 3, 8, 5)),
        ("11-03 09:00", local(2026, 11, 3, 9, 0)),
        ("01-05 09:00", local(2027, 1, 5, 9, 0)),  # 今年这天已过
        ("18:30", local(2026, 11, 2, 18, 30)),
        ("09:00", local(2026, 11, 3, 9, 0)),  # 今天这个点已过
        ("明天 07:30", local(2026, 11, 3, 7, 30)),
        ("30分钟后", NOW + timedelta(minutes=30)),
        ("2小时后", NOW + timedelta(hours=2)),
        ("1个小时30分钟后", NOW + timedelta(minutes=90)),
        ("3天后", NOW + timedelta(days=3)),
    ],
)
def test_parse_one_off(text, expected):
    assert parse_when(text, NOW) == (expected, None)


@pytest.mark.parametrize(
    ("text", "first", "label"),
    [
        ("每天 08:00", local(2026, 11, 3, 8, 0), "每天 08:00"),
        ("每天 18:00", local(2026, 11, 2, 18, 0), "每天 18:00"),
        ("每周一 09:00", local(2026, 11, 9, 9, 0), "每周一 09:00"),  # 今天周一，这个点已过
        ("每周日 21:00", local(2026, 11, 8, 21, 0), "每周日 21:00"),
        ("工作日 08:00", local(2026, 11, 3, 8, 0), "工作日 08:00"),
    ],
)
def test_parse_repeating(text, first, label):
    due, repeat = parse_when(text, NOW)
    assert due == first and repeat.label() == label


def test_workdays_skip_the_weekend():
    _, repeat = parse_when("工作日 08:00", NOW)
    assert repeat.next_after(local(2026, 11, 6, 9, 0)) == local(2026, 11, 9, 8, 0)  # 周五 → 周一


@pytest.mark.parametrize(
    "bad", ["2026-11-01 08:00", "2026-02-30 08:00", "25:00", "过一会儿", "", "0分钟后", "后"]
)
def test_parse_rejects(bad):
    with pytest.raises(SkillError):
        parse_when(bad, NOW)


# ---- 提醒本 ----


def test_book_survives_restart_and_only_cancels_own(tmp_path):
    path = tmp_path / "reminders.json"
    book = ReminderBook(path)
    due, repeat = parse_when("每天 18:00", NOW)
    r = book.add("private:1", "p1", "吃药", due, repeat)
    other = book.add("private:2", "p2", "开会", NOW + timedelta(hours=1), None)

    again = ReminderBook(path)
    assert [x.what for x in again.pending("private:1")] == ["吃药"]
    assert again.pending("private:1")[0].repeat.label() == "每天 18:00"
    assert again.cancel("private:1", other.id) is None  # 不能取消别人的
    assert again.cancel("private:2", other.id).what == "开会"
    assert ReminderBook(path).add("private:1", "p1", "x", due, None).id > r.id  # 编号不重复


def test_book_finish_repeating_moves_to_next_time(tmp_path):
    book = ReminderBook(tmp_path / "r.json")
    due, repeat = parse_when("每天 18:00", NOW)
    r = book.add("private:1", "p", "吃药", due, repeat)
    once = book.add("private:1", "p", "开会", NOW + timedelta(minutes=1), None)
    late = local(2026, 11, 5, 20, 0)  # 错过了好几天：只补一次，排到下一个未来的时间
    assert {x.id for x in book.due(late)} == {r.id, once.id}
    book.finish(r.id, late)
    book.finish(once.id, late)
    assert [(x.id, x.due) for x in book.pending()] == [(r.id, local(2026, 11, 6, 18, 0))]


def test_unreadable_file_is_kept(tmp_path):
    path = tmp_path / "reminders.json"
    path.write_text("不是 JSON", "utf-8")
    assert ReminderBook(path).pending() == []
    assert (tmp_path / "reminders.bad.json").read_text("utf-8") == "不是 JSON"


def test_console_process_sees_changes(tmp_path):
    path = tmp_path / "r.json"
    bot_side, console_side = ReminderBook(path), ReminderBook(path)
    bot_side.add("private:1", "p", "吃药", NOW + timedelta(hours=1), None)
    assert [x.what for x in console_side.pending()] == ["吃药"]


# ---- 技能 ----


def ctx(*, channel="private:3000", body="qq", private=True, owner=False):
    return SkillContext(body, channel, "p", private=private, owner=owner, now=NOW)


def test_skill_only_in_qq_private(tmp_path):
    skill = Reminders(RemindersSkillConfig(), ReminderBook(tmp_path / "r.json"))
    assert skill.allowed(ctx())
    assert not skill.allowed(ctx(channel="group:4000", private=False))
    assert not skill.allowed(ctx(channel="cli", body="cli"))  # 命令行不会到点发消息
    owner_only = Reminders(RemindersSkillConfig(owner_only=True), skill.book)
    assert not owner_only.allowed(ctx()) and owner_only.allowed(ctx(owner=True))


def test_skill_run_confirms_time_and_limits(tmp_path):
    skill = Reminders(RemindersSkillConfig(max_per_person=1), ReminderBook(tmp_path / "r.json"))
    result = skill.run("提醒", "2026-11-02 18:30｜给妈妈打电话", ctx())
    assert "2026年11月2日（周一）18:30" in result.text and "给妈妈打电话" in result.text
    assert "给妈妈打电话" in skill.context(ctx())
    assert skill.context(ctx(channel="private:9")) == ""  # 别人看不到
    with pytest.raises(SkillError, match="先取消"):
        skill.run("提醒", "明天 08:00｜再来一条", ctx())
    with pytest.raises(SkillError, match="要写成"):
        skill.run("提醒", "明天 08:00", ctx())
    assert "给妈妈打电话" in skill.run("取消提醒", "编号 1", ctx()).text
    with pytest.raises(SkillError, match="没有编号"):
        skill.run("取消提醒", "1", ctx())


# ---- 从聊天到提醒 ----


class Brain:
    def __init__(self) -> None:
        self.replies = ["好呀"]

    def __call__(self, messages, json_mode):
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    (MemoryLayout(config.memory_dir).self_dir / "seed.md").write_text(
        "# 种子\n\n- 我叫白璎。\n", "utf-8"
    )
    brain = Brain()
    gateway, chat = scripted_gateway(brain)
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    clock = {"now": NOW}
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=[GROUP],
                   debounce_private_seconds=0, debounce_group_seconds=0,
                   unsolicited_probability=0)  # fmt: skip
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1),
              clock=lambda: clock["now"])  # fmt: skip
    b.brain, b.chat, b.clock = brain, chat, clock
    yield b
    b.close()
    companion.close()


def msg(text, *, user=FRIEND, group=None):
    segs = [{"type": "text", "data": {"text": text}}]
    if group:
        segs.insert(0, {"type": "at", "data": {"qq": BOT}})
    e = {"post_type": "message", "self_id": int(BOT), "user_id": int(user), "message_id": 1,
         "message_type": "group" if group else "private", "message": segs,
         "sender": {"nickname": "小明"}, "time": int(NOW.timestamp())}  # fmt: skip
    if group:
        e["group_id"] = int(group)
    return e


def say(bot, *events):
    async def go():
        for e in events:
            await bot.handle_event(e)
            await bot.drain()

    asyncio.run(go())


def tick(bot, when):
    bot.clock["now"] = when
    asyncio.run(bot.reminder_tick())


def sends(bot):
    return [(a, p) for a, p in bot.client.sent if a.startswith("send_")]


def set_one(bot, what="18:30｜给妈妈打电话"):
    bot.brain.replies = [f"【提醒：{what}】", "好，今晚 18:30 我叫你。"]
    say(bot, msg("今晚六点半提醒我给妈妈打电话"))
    bot.client.sent.clear()


def test_set_and_fire_in_her_own_words(bot):
    bot.brain.replies = ["【提醒：18:30｜给妈妈打电话】", "好，今晚 18:30 我叫你。"]
    say(bot, msg("今晚六点半提醒我给妈妈打电话"))
    assert bot.client.texts("send_private_msg") == ["好，今晚 18:30 我叫你。"]  # 标记不外露
    pending = bot.c.reminders.pending("private:3000")
    assert [r.what for r in pending] == ["给妈妈打电话"]
    assert pending[0].due == local(2026, 11, 2, 18, 30)
    # 写给她的说明里能看到这条提醒，下一轮她知道编号
    say(bot, msg("还有什么提醒吗"))
    assert "给妈妈打电话" in bot.chat.requests[-1][0].content

    bot.client.sent.clear()
    tick(bot, local(2026, 11, 2, 18, 29))
    assert sends(bot) == []  # 还没到
    bot.brain.replies = ["六点半啦，该给妈妈打电话了，别又拖到忘了【心情：开心｜答应的事做到了】"]
    tick(bot, local(2026, 11, 2, 18, 30))
    assert bot.client.texts("send_private_msg") == ["六点半啦，该给妈妈打电话了，别又拖到忘了"]
    assert bot.client.sent[0][1]["user_id"] == int(FRIEND)

    request = bot.chat.requests[-1]
    assert "给妈妈打电话" in request[-1].content and request[-1].role == "system"  # 交给模型写
    assert "一定要说" in request[0].content and "【不回复】" not in request[0].content.split(
        "【要不要接话】"
    )[1].split("\n\n")[0]  # fmt: skip
    assert bot.c.reminders.pending() == []
    mine = [e for e in bot.c.log.iter_events() if e.meta.get("reminder")]
    assert len(mine) == 1 and "六点半" in mine[0].content  # 记进聊天记录
    tick(bot, local(2026, 11, 2, 18, 31))
    assert len(sends(bot)) == 1  # 不重复


def test_groups_cannot_set_reminders(bot):
    bot.brain.replies = ["【提醒：18:30｜开会】好的"]
    say(bot, msg("提醒我开会", group=GROUP))
    assert bot.c.reminders.pending() == []
    assert "【提醒：" not in bot.chat.requests[-1][0].content  # 群里没告诉她有这个技能
    assert bot.client.texts("send_group_msg") == ["好的"]  # 标记不外露


def test_late_reminder_says_so(bot):
    set_one(bot)
    bot.brain.replies = ["不好意思我来晚了，该给妈妈打电话了"]
    tick(bot, local(2026, 11, 2, 21, 0))
    assert len(sends(bot)) == 1
    assert "已经迟了" in bot.chat.requests[-1][-1].content


def test_on_time_reminder_does_not_mention_lateness(bot):
    set_one(bot)
    tick(bot, local(2026, 11, 2, 18, 31))
    assert "已经迟了" not in bot.chat.requests[-1][-1].content


@pytest.mark.parametrize("output", ["【不回复】", "【心情：累】"])
def test_she_cannot_skip_a_reminder(bot, output):
    set_one(bot)
    bot.brain.replies = [output]
    tick(bot, local(2026, 11, 2, 18, 30))
    assert bot.client.texts("send_private_msg") == ["到时间啦，提醒你：给妈妈打电话"]
    assert bot.c.reminders.pending() == []


def test_model_failure_still_reminds(bot):
    from companion.models.openai_compat import ModelError

    set_one(bot)

    def broken(messages, json_mode):
        raise ModelError("模型挂了")

    bot.chat.handler = broken
    tick(bot, local(2026, 11, 2, 18, 30))
    assert bot.client.texts("send_private_msg") == ["到时间啦，提醒你：给妈妈打电话"]


def test_voice_marker_becomes_text_in_private(bot):
    set_one(bot)
    bot.brain.replies = ["【语音：该打电话了】"]
    tick(bot, local(2026, 11, 2, 18, 30))
    assert bot.client.texts("send_private_msg") == ["该打电话了"]
    assert [a for a, _ in sends(bot)] == ["send_private_msg"]


def test_failed_send_retries_without_rewriting(bot):
    set_one(bot)
    bot.brain.replies = ["第一版"]
    bot.client.replies["send_private_msg"] = OneBotError("发不出去")
    due = local(2026, 11, 2, 18, 30)
    tick(bot, due)
    calls = len(bot.chat.requests)
    assert len(bot.c.reminders.pending()) == 1  # 没发出去就还留着
    assert not any(e.meta.get("reminder") for e in bot.c.log.iter_events())  # 也不算她说过

    sent = len(bot.client.sent)
    tick(bot, due + timedelta(seconds=30))
    assert len(bot.client.sent) == sent  # 还没到重试时间
    del bot.client.replies["send_private_msg"]
    bot.brain.replies = ["第二版"]
    tick(bot, due + timedelta(minutes=3))
    assert bot.client.texts("send_private_msg")[-1] == "第一版"  # 不重新写
    assert len(bot.chat.requests) == calls
    assert bot.c.reminders.pending() == []


def test_gives_up_after_a_day(bot):
    set_one(bot)
    bot.client.replies["send_private_msg"] = OneBotError("被删好友了")
    tick(bot, local(2026, 11, 3, 18, 31))
    assert bot.c.reminders.pending() == []


def test_waits_while_napcat_is_down(bot):
    set_one(bot)
    bot.client.connected = asyncio.Event()  # 断线
    tick(bot, local(2026, 11, 2, 18, 30))
    assert sends(bot) == [] and len(bot.c.reminders.pending()) == 1
    bot.client.connected.set()
    tick(bot, local(2026, 11, 2, 18, 31))
    assert len(sends(bot)) == 1


def test_switched_off_keeps_them_for_later(bot):
    set_one(bot)
    bot.c.config.skills.reminders.enabled = False
    tick(bot, local(2026, 11, 2, 18, 30))
    assert sends(bot) == [] and len(bot.c.reminders.pending()) == 1
    bot.c.config.skills.reminders.enabled = True
    tick(bot, local(2026, 11, 2, 18, 31))
    assert len(sends(bot)) == 1


def test_repeating_fires_again_tomorrow(bot):
    bot.brain.replies = ["【提醒：每天 18:00｜吃药】", "好"]
    say(bot, msg("每天六点提醒我吃药"))
    bot.client.sent.clear()
    bot.brain.replies = ["该吃药啦"]
    tick(bot, local(2026, 11, 2, 18, 0))
    tick(bot, local(2026, 11, 2, 18, 1))
    assert len(sends(bot)) == 1
    tick(bot, local(2026, 11, 3, 18, 0))
    assert len(sends(bot)) == 2
    saved = json.loads((bot.c.config.data_dir / "reminders.json").read_text("utf-8"))
    assert saved["items"][0]["repeat"]["clock"] == "18:00"


def test_due_is_stored_in_utc(bot):
    set_one(bot)
    saved = json.loads((bot.c.config.data_dir / "reminders.json").read_text("utf-8"))
    assert datetime.fromisoformat(saved["items"][0]["due"]).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(saved["items"][0]["due"]) == local(
        2026, 11, 2, 18, 30
    ).astimezone(UTC)
