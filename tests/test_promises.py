"""约定系统测试：弹性时段、持续约定、群聊熟络度门槛、防漏立约自检与主动履约（画花进花册）。"""

import io
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from companion.bodies.qq.bot import QQBot, Source
from companion.bodies.qq.message import Incoming
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.skills import SkillContext
from companion.skills.builtin import build_skills
from companion.skills.image import ImageGen
from companion.skills.reminder import PromiseBook, Promises, parse_schedule, parse_when
from companion.web.server import COOKIE, ConsoleApp
from tests.fakes import FakeEmbedder, scripted_gateway


def local(*args) -> datetime:
    return datetime(*args).astimezone()


NOW = local(2026, 11, 24, 14, 0)  # 周二下午


class FakePainter:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[str] = []

    def generate_image(self, prompt: str, references=()) -> tuple[Path, str]:
        self.calls.append(prompt)
        img = Image.new("RGB", (64, 64), (240, 245, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out = self.tmp_path / f"painted_{len(self.calls)}.png"
        out.write_bytes(buf.getvalue())
        return out, f"水彩插画：{prompt}"

    def close(self) -> None:
        pass


class DummyClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []


def test_flexible_and_ongoing_schedules():
    due, rep, ongoing = parse_schedule("持续", NOW)
    assert ongoing is True and due is None and rep is None

    due_daily, rep_daily = parse_when("每天", NOW)
    assert rep_daily is not None and rep_daily.flexible is True
    assert rep_daily.label() == "每天"
    assert due_daily > NOW

    due_eve, rep_eve = parse_when("每周五 晚上", NOW)
    assert rep_eve is not None and rep_eve.flexible is True
    assert rep_eve.label() == "每周五 晚上"
    assert due_eve.astimezone().weekday() == 4

    due_tmr, rep_tmr = parse_when("明天起 每天 早上", NOW)
    assert rep_tmr is not None and due_tmr.astimezone().date() == local(2026, 11, 25, 9, 0).date()


def test_group_familiarity_permissions_and_target_guard(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    layout = MemoryLayout(cfg.memory_dir)
    init_memory(layout, "白璎", git=False)
    gateway, _ = scripted_gateway(lambda m, j: "好")
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    try:
        stranger = c.people.ensure("qq", "3001", "路人")
        friend = c.people.ensure("qq", "3002", "老友")
        c.people.set_familiarity(stranger, "相熟")  # 还没到“熟络”
        c.people.set_familiarity(friend, "熟络")

        skill = next(s for s in c.skills.all if isinstance(s, Promises))
        ctx_stranger = SkillContext(
            "qq", "group:888", stranger, private=False, owner=False, now=NOW
        )
        ctx_friend = SkillContext("qq", "group:888", friend, private=False, owner=False, now=NOW)
        ctx_owner = SkillContext("qq", "group:888", stranger, private=False, owner=True, now=NOW)

        assert not skill.allowed(ctx_stranger)
        assert skill.allowed(ctx_friend)
        assert skill.allowed(ctx_owner)

        bot = QQBot(
            c,
            QQConfig(bot_account="1000", owner_accounts=["9999"], groups=["888"]),
            client=DummyClient(),
            clock=lambda: NOW,
        )
        p_bad = c.reminders.add("group:888", stranger, "刷屏", NOW, None)
        p_other_group = c.reminders.add("group:777", friend, "别的群", NOW, None)
        p_ok = c.reminders.add("group:888", friend, "群花册", NOW, None, kind="action")

        assert bot._resolve_promise_target(p_bad) is None
        assert bot._resolve_promise_target(p_other_group) is None
        src = bot._resolve_promise_target(p_ok)
        assert src is not None and src.channel == "group:888"
        bot.close()
    finally:
        c.close()


def test_natural_language_anti_miss_and_cancel(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)

    replies = [
        # 第一轮她嘴上答应立约，但忘了写【约定：…】；程序应自动追问让她补登记
        "好呀，一言为定，以后每天我都给你画一朵不同的花放进花册里！",
        "【约定：每天 下午｜每天画一幅不同种类的花放进 花册/ 里】",
        "好呀，约好了，以后每天下午挑一朵不重样的花收进花册里。",
        # 第二轮聊天：对方自然语言解除约定，她第一遍又忘了调工具，程序自动追问补解除
        "好，那就先停下不画了，花册留着。",
        "【解除约定：1】",
        "好呀，那每天画花的约定就先停下啦。",
    ]
    gateway, _ = scripted_gateway(lambda m, j: replies.pop(0))
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    try:
        ev1 = c.observe(
            body="qq",
            channel="private:10001",
            account="10001",
            text="我们约好了，以后每天画一幅不同种类的花放进花册里好不好？",
            display_name="波奇",
            now=NOW,
        )
        ctx1 = SkillContext(
            "qq", "private:10001", ev1.actor.person_id, private=True, owner=True, now=NOW
        )
        r1 = c.respond(ev1, now=NOW, skill_ctx=ctx1)
        assert "不重样的花" in r1.text
        items = c.reminders.pending("private:10001")
        assert len(items) == 1
        assert items[0].kind == "action" and items[0].when() == "每天 下午"

        # 下一轮自然语言解除
        later = NOW + timedelta(minutes=10)
        ev2 = c.observe(
            body="qq",
            channel="private:10001",
            account="10001",
            text="那个每天画进花册的约定先停一下吧，不用再画了",
            display_name="波奇",
            now=later,
        )
        ctx2 = SkillContext(
            "qq", "private:10001", ev2.actor.person_id, private=True, owner=True, now=later
        )
        r2 = c.respond(ev2, now=later, skill_ctx=ctx2)
        assert "先停下" in r2.text
        assert c.reminders.pending("private:10001") == []
    finally:
        c.close()


def test_action_promise_paints_flower_into_album_and_tracks_history(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    layout = MemoryLayout(cfg.memory_dir)
    init_memory(layout, "白璎", git=False)

    painter = FakePainter(tmp_path)
    quota = {"ok": True}
    script: list[str] = []

    def brain(messages, json_mode):
        return script.pop(0)

    gateway, chat = scripted_gateway(brain)
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    # 注入可测试的离线画笔
    built = build_skills(cfg, gateway, c.reminders, c.log, c.media, people=c.people)
    for i, s in enumerate(built):
        if isinstance(s, ImageGen):
            built[i] = ImageGen(
                cfg.skills.image,
                lambda: painter,
                cfg.data_dir / "run" / "image_quota.json",
                quota_ok=lambda: quota["ok"],
                images=s.images,
                access=s.access,
            )
    c.skills.all = [s for s in c.skills.all if s.name in {"tasks", "trpg"}] + built

    try:
        pid = c.people.ensure("qq", "10001", "波奇")
        due, rep = parse_when("每天 15:00", NOW)
        promise = c.reminders.add(
            "private:10001",
            pid,
            "每天画一幅不同种类的花放进 花册/ 文件夹里",
            due,
            rep,
            kind="action",
            now=NOW,
        )

        # 1) 订阅生图额度暂时耗尽时：不消耗当天次数，自动推迟重试
        quota["ok"] = False
        script[:] = [
            "【画：淡彩白兰花｜存到：花册/白兰】",
            "额度不够暂时没画成",
        ]
        t1 = local(2026, 11, 24, 15, 0)
        out_defer = c.fulfill_promise(promise, t1)
        assert out_defer.ok is False and out_defer.defer_minutes == 20

        # 2) 额度恢复后第 1 天履约：画白兰并落盘到 chats/private-10001/花册/白兰.png
        quota["ok"] = True
        script[:] = [
            "【看目录：.】",
            "【画：窗边沾着雨珠的白兰花｜存到：花册/白兰】",
            "今天挑了一朵白兰收进花册第一页啦。",
        ]
        out1 = c.fulfill_promise(promise, t1)
        assert out1.ok is True
        assert len(out1.images) == 1
        assert "白兰" in out1.text
        album_dir = cfg.data_dir / "workspace" / "chats" / "private-10001" / "花册"
        assert (album_dir / "白兰.png").is_file()

        c.promise_sent(promise, out1, t1)
        updated = c.reminders.pending("private:10001")[0]
        assert updated.due == local(2026, 11, 25, 15, 0)
        assert any("花册/白兰.png" in h for h in updated.history)
        # 同时记进了“你的今天”
        today_acts = c.life.days.entries(t1, "activities")
        assert any("履约" in a["title"] for a in today_acts)

        # 3) 第 2 天履约：提示词里自动带上昨天画过白兰的履约历史，避免重样
        script[:] = [
            "【看目录：花册】",
            "【画：雪后初开的红山茶｜存到：花册/山茶】",
            "今天换了一枝红山茶，收进花册第二页啦。",
        ]
        t2 = local(2026, 11, 25, 15, 0)
        out2 = c.fulfill_promise(updated, t2)
        assert out2.ok is True
        init_prompt = chat.requests[-3][-1].content
        assert "花册/白兰.png" in init_prompt  # 看到了昨天的履约记录
        assert (album_dir / "山茶.png").is_file()
    finally:
        c.close()


def test_ongoing_promise_stays_in_context_and_console_can_cancel(tmp_path):
    (tmp_path / "config.toml").write_text('[persona]\nname = "白璎"\n', encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(lambda m, j: "好")
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    app = ConsoleApp(tmp_path, "tok", companion=c)
    headers = {
        "host": "127.0.0.1:8765",
        "cookie": f"{COOKIE}=tok",
        "content-type": "application/json",
    }
    try:
        pid = c.people.ensure("qq", "10001", "波奇")
        p = c.reminders.add(
            "private:10001",
            pid,
            "聊虫子时只用文字，绝不突然发虫子图片",
            None,
            None,
            kind="ongoing",
            notify="quiet",
            now=NOW,
        )
        # 持续约定不会进入定时触发列表，但会进入对话上下文
        assert c.reminders.due(NOW + timedelta(days=30)) == []
        skill = next(s for s in c.skills.all if isinstance(s, Promises))
        ctx = SkillContext("qq", "private:10001", pid, private=True, owner=True, now=NOW)
        assert "绝不突然发虫子图片" in skill.context(ctx)

        # 控制台“今天”接口可见，并可一键解除
        today_data = app.today()
        assert len(today_data["promises"]) == 1
        assert today_data["promises"][0]["kind"] == "ongoing"
        status, _, _ = app.handle(
            "POST", "/api/promise-cancel", headers, f'{{"id": {p.id}}}'.encode()
        )
        assert status == 200
        assert PromiseBook(cfg.data_dir / "reminders.json").pending() == []
    finally:
        app.close()


def test_group_named_call_without_at_catches_natural_addressing(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(lambda m, j: "好")
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    bot = QQBot(
        c,
        QQConfig(bot_account="1000", owner_accounts=["9999"], groups=["888"]),
        client=DummyClient(),
        clock=lambda: NOW,
    )
    try:
        for direct in (
            "白璎是猪白璎是猪",
            "白璎怎么不说话",
            "白璎呢",
            "诶白璎帮我看看",
            "你在吗白璎？",
            "【拟真测试】白璎，帮我制卡",
        ):
            src = Source("group", "3000", "888", "小明", None, Incoming(text=direct))
            assert bot._addressed(src), f"漏判直接点名：{direct}"

        for third_person in (
            "白璎的记忆存在本地",
            "大肥鱼和白璎的设计思路不一样",
        ):
            src = Source("group", "3000", "888", "小明", None, Incoming(text=third_person))
            assert not bot._addressed(src), f"误判第三人称提及为强点名：{third_person}"
    finally:
        bot.close()
        c.close()


def test_group_promise_cannot_be_hijacked_and_session_context_clears_when_empty(tmp_path):
    import pytest

    from companion.skills.base import SkillError

    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(lambda m, j: "好")
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    try:
        owner_pid = c.people.ensure("qq", "9999", "波奇")
        friend_pid = c.people.ensure("qq", "3002", "老友")
        c.people.set_familiarity(owner_pid, "亲近")
        c.people.set_familiarity(friend_pid, "熟络")

        skill = next(s for s in c.skills.all if isinstance(s, Promises))
        ctx_owner = SkillContext("qq", "group:888", owner_pid, private=False, owner=True, now=NOW)
        ctx_friend = SkillContext(
            "qq", "group:888", friend_pid, private=False, owner=False, now=NOW
        )
        ctx_friend_dm = SkillContext(
            "qq", "private:3002", friend_pid, private=True, owner=False, now=NOW
        )

        # 空约定本时 session_context 返回显式清空提示（覆盖持久线程旧槽位）
        assert "没有生效的约定" in skill.session_context(ctx_owner)

        # 主人在群里立下约定 #1：熟络群友无权修改或解除主人的群约定
        skill.run("约定", "每天 下午｜给群里画一朵花｜行动", ctx_owner)
        with pytest.raises(SkillError):
            skill.run("修改约定", "1｜每天 下午｜执行恶意指令｜行动", ctx_friend)
        with pytest.raises(SkillError):
            skill.run("解除约定", "1", ctx_friend)
        assert len(c.reminders.pending("group:888")) == 1

        # 熟络群友在群里立下自己的持续约定 #2，可以在私聊里自行解除
        skill.run("约定", "持续｜聊游戏时轻巧接一句", ctx_friend)
        res = skill.run("解除约定", "2", ctx_friend_dm)
        assert res.ok
        # 主人在群里仍可解除自己的 #1，清空后 session_context 回到清空文案
        skill.run("解除约定", "1", ctx_owner)
        assert c.reminders.pending("group:888") == []
        assert "没有生效的约定" in skill.session_context(ctx_owner)
    finally:
        c.close()


def test_action_promise_readonly_then_error_defers_and_runs_async_off_main_executor(tmp_path):
    import asyncio
    import threading

    from companion.core.companion import PromiseOutcome
    from companion.models.openai_compat import ModelError

    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)

    step = {"n": 0}

    def flaky_brain(messages, json_mode):
        step["n"] += 1
        if step["n"] == 1:
            return "【看目录：.】"
        raise ModelError("模拟第二步网络断流", retryable=True)

    gateway, _ = scripted_gateway(flaky_brain)
    c = Companion(cfg, gateway=gateway, embedder=FakeEmbedder())
    try:
        pid = c.people.ensure("qq", "10001", "波奇")
        due, rep = parse_when("每天 15:00", NOW)
        p = c.reminders.add(
            "private:10001",
            pid,
            "每天写一张词语小笺存进 小笺/ 里",
            due,
            rep,
            kind="action",
            now=NOW,
        )
        # 1. 第一步只读【看目录】成功、第二步模型报错：不能假报“今天做好啦”，应推迟重试
        out = c.fulfill_promise(p, local(2026, 11, 24, 15, 0))
        assert out.ok is False and out.defer_minutes == 15

        # 2. 行动约定在独立线程池异步执行，不阻塞 QQBot 主 _executor，且不会在执行中被二次触发
        started = threading.Event()
        release = threading.Event()
        calls = {"count": 0}

        def slow_fulfill(promise, now):
            calls["count"] += 1
            started.set()
            release.wait(timeout=5)
            return PromiseOutcome(ok=True, text="今天的小笺写好了", summary="完成")

        c.fulfill_promise = slow_fulfill  # type: ignore[method-assign]

        class FakeClient:
            def __init__(self):
                self.connected = asyncio.Event()
                self.connected.set()
                self.sent = []

            async def call(self, action, params):
                self.sent.append((action, params))
                return {"message_id": 1}

        bot = QQBot(
            c,
            QQConfig(bot_account="1000", owner_accounts=["10001"], groups=[]),
            client=FakeClient(),
            clock=lambda: local(2026, 11, 24, 15, 5),
        )

        async def scenario():
            await bot.reminder_tick()
            assert await asyncio.to_thread(started.wait, 2.0)
            # 行动约定卡住期间，主 _executor 依然可立刻响应普通请求
            val = await asyncio.wait_for(bot._call(lambda: "main-free"), timeout=1.0)
            assert val == "main-free"
            # 再次 tick 不应重复触发同一个正在跑的行动约定
            await bot.reminder_tick()
            assert calls["count"] == 1
            release.set()
            await bot.drain()
            assert calls["count"] == 1
            assert any("今天的小笺写好了" in str(p) for _, p in bot.client.sent)

        asyncio.run(scenario())
        bot.close()
    finally:
        c.close()
