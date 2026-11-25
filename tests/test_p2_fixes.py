"""P2 修复回归测试（REV-10、REV-11、REV-12）。"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from companion.bodies.qq.bot import QQBot, Source, _Pending
from companion.bodies.qq.message import Incoming
from companion.bodies.qq.onebot import OneBotClient, OneBotError
from companion.config import HistorySkillConfig, QQConfig, SkillsConfig, load_config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper
from companion.memory import Event, EventLog, MemoryLayout, PeopleRegistry, init_memory
from companion.memory.events import Actor
from companion.memory.index import Episode
from companion.models.codex_app_server import RateLimits, RateWindow
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage, ModelError, OpenAICompatClient
from companion.skills.base import Skill, SkillContext, SkillResult, Skills
from companion.skills.group_agent import GroupAgent
from companion.skills.history import ChatHistory, ChatScope, _Query
from companion.skills.reminder import PromiseBook
from companion.stickers import StickerBook
from tests.fakes import FakeEmbedder, scripted_gateway
from tests.test_qq import FakeClient

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"
T0 = datetime(2026, 11, 25, 12, tzinfo=UTC)


# ---- REV-10：跨进程同步与并发锁 ----


def test_people_registry_syncs_across_instances(tmp_path: Path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎", git=False)
    reg_qq = PeopleRegistry(layout)
    reg_web = PeopleRegistry(layout)

    pid = reg_qq.ensure("qq", "3000", "小明")
    assert reg_web.get(pid) is not None
    assert reg_web.get(pid).familiarity == "初识"  # type: ignore[union-attr]

    # 模拟控制台修改熟络度，qq 侧无需手动 reload 即可看到最新值
    time.sleep(0.02)
    reg_web.set_familiarity(pid, "熟络")
    updated = reg_qq.get(pid)
    assert updated is not None
    assert updated.familiarity == "熟络"


def test_promise_book_survives_transient_oserror_without_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "reminders.json"
    book_a = PromiseBook(path)
    book_b = PromiseBook(path)
    p = book_a.add("private:2000", "p1", "吃药", T0 + timedelta(hours=1), None, now=T0)
    assert len(book_b.pending()) == 1

    # 另一进程写入新约定后，本进程读取遇到短暂 Windows 文件锁冲突
    time.sleep(0.02)
    book_a.add("private:2000", "p1", "喝水", T0 + timedelta(hours=2), None, now=T0)

    real_read_text = Path.read_text
    calls = {"n": 0}

    def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path and calls["n"] < 3:
            calls["n"] += 1
            raise PermissionError("sharing violation")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    # 锁冲突时保留原内存状态，绝不改名为 .bad.json
    items_during_lock = book_b.pending()
    assert [i.id for i in items_during_lock] == [p.id]
    assert path.exists()
    assert not path.with_suffix(".bad.json").exists()

    # 锁释放后，下一次读取自动同步到最新的 2 条约定
    items_after = book_b.pending()
    assert len(items_after) == 2


def test_sticker_book_syncs_across_instances(tmp_path: Path):
    root = tmp_path / "stickers"
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fake-jpg")
    book_qq = StickerBook(root)
    book_web = StickerBook(root)

    book_qq.add(img, "deadbeef", "笑哭", "开心", "p1", now=T0)
    assert len(book_web) == 1
    assert book_web.get("deadbeef") is not None

    time.sleep(0.02)
    assert book_web.remove("deadbeef") is True
    assert len(book_qq) == 0


# ---- REV-11：QQ 断线容错与离线回补 ----


def test_onebot_call_wraps_send_disconnect_and_cleans_pending():
    async def go() -> None:
        client = OneBotClient("ws://127.0.0.1:9", None, lambda ev: asyncio.sleep(0))

        class BrokenWS:
            async def send(self, _data: str) -> None:
                raise OSError("connection reset")

        client._ws = BrokenWS()  # type: ignore[assignment]
        with pytest.raises(OneBotError, match="发送失败"):
            await client.call("send_private_msg", {"user_id": 1, "message": []})
        assert client._pending == {}

    asyncio.run(go())


def test_onebot_run_forever_resets_backoff_after_connection(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    async def go() -> None:
        client = OneBotClient("ws://127.0.0.1:9", None, lambda ev: asyncio.sleep(0))
        attempts = {"n": 0}

        async def fake_run_once() -> None:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                # 前两次未连上即失败：退避应递增 1 -> 2
                raise OSError("refused")
            # 第三次连上后中途断线：退避应重置回 1.0
            client._was_connected = True
            raise OSError("dropped mid-session")

        monkeypatch.setattr(client, "run_once", fake_run_once)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await client.run_forever()

    asyncio.run(go())
    assert sleeps == [1.0, 2.0, 1.0]


def _make_bot(tmp_path: Path) -> QQBot:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(lambda messages, json_mode: "嗯嗯")
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    cfg = QQConfig(
        bot_account=BOT,
        owner_accounts=[OWNER],
        groups=[GROUP],
        debounce_private_seconds=0,
        debounce_group_seconds=0,
        unsolicited_probability=0,
    )
    return QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: T0)


def test_backfill_not_truncated_by_live_message_after_connect(tmp_path: Path):
    bot = _make_bot(tmp_path)
    try:
        bot.c.index.set_meta(f"announced:group:{GROUP}", "x")
        bot.c.index.set_meta(f"announced_v2:group:{GROUP}", "x")
        # 断线前最后一条记录在 T0 - 3h
        bot.c.observe(
            body="qq",
            channel=f"group:{GROUP}",
            account=FRIEND,
            text="断线前最后一句",
            display_name="小红",
            now=T0 - timedelta(hours=3),
            meta={"message_id": "100"},
        )
        bot.client.replies["get_group_msg_history"] = {
            "messages": [
                {
                    "message_id": 99,
                    "user_id": int(FRIEND),
                    "time": int((T0 - timedelta(hours=4)).timestamp()),
                    "sender": {"nickname": "小红"},
                    "message": [{"type": "text", "data": {"text": "更早的旧消息"}}],
                },
                {
                    "message_id": 100,
                    "user_id": int(FRIEND),
                    "time": int((T0 - timedelta(hours=3)).timestamp()),
                    "sender": {"nickname": "小红"},
                    "message": [{"type": "text", "data": {"text": "断线前最后一句"}}],
                },
                {
                    "message_id": 101,
                    "user_id": 3001,
                    "time": int((T0 - timedelta(hours=3)).timestamp()),
                    "sender": {"nickname": "小刚"},
                    "message": [{"type": "text", "data": {"text": "同秒漏掉的消息"}}],
                },
                {
                    "message_id": 102,
                    "user_id": 3002,
                    "time": int((T0 - timedelta(hours=1)).timestamp()),
                    "sender": {"nickname": "小李"},
                    "message": [{"type": "text", "data": {"text": "离线期间的消息"}}],
                },
                {
                    "message_id": 200,
                    "user_id": int(FRIEND),
                    "time": int(T0.timestamp()),
                    "sender": {"nickname": "小红"},
                    "message": [{"type": "text", "data": {"text": "刚连上的实时消息"}}],
                },
            ]
        }

        async def go() -> None:
            orig_announce = bot._announce_groups

            async def announce_while_live_arrives() -> None:
                await orig_announce()
                # 模拟刚连上时，在 backfill 跑起来之前先收到一条实时消息
                await bot.handle_event(
                    {
                        "post_type": "message",
                        "self_id": int(BOT),
                        "user_id": int(FRIEND),
                        "message_id": 200,
                        "message_type": "group",
                        "group_id": int(GROUP),
                        "time": int(T0.timestamp()),
                        "sender": {"nickname": "小红"},
                        "message": [{"type": "text", "data": {"text": "刚连上的实时消息"}}],
                    }
                )

            bot._announce_groups = announce_while_live_arrives  # type: ignore[method-assign]
            await bot.handle_event(
                {"post_type": "meta_event", "meta_event_type": "lifecycle", "self_id": int(BOT)}
            )
            await bot.drain()

        asyncio.run(go())
        texts = [e.content for e in bot.c.log.iter_events()]
        assert "更早的旧消息" not in texts
        assert "同秒漏掉的消息" in texts
        assert "离线期间的消息" in texts
        assert texts.count("刚连上的实时消息") == 1
        assert bot._backfill_live_sets == []
    finally:
        bot.close()
        bot.c.close()


def test_life_tick_skips_when_disconnected(tmp_path: Path):
    bot = _make_bot(tmp_path)
    try:
        bot.client.connected = asyncio.Event()  # 未连接
        called = False

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal called
            called = True
            return None

        bot.c.prepare_activity = boom  # type: ignore[method-assign]
        asyncio.run(bot.life_tick())
        assert called is False
    finally:
        bot.close()
        bot.c.close()


def test_task_command_passes_full_skill_context(tmp_path: Path):
    bot = _make_bot(tmp_path)
    try:
        captured: list[SkillContext] = []

        def fake_run(skill: Any, marker: str, arg: str, ctx: SkillContext) -> Any:
            captured.append(ctx)
            return None, SkillResult("收到", ok=False)

        bot.c.skills.run = fake_run  # type: ignore[method-assign]
        src = Source("group", OWNER, GROUP, "主人", "1", Incoming(text="/任务 整理"))
        asyncio.run(bot._command(src, "任务 整理"))
        assert len(captured) == 1
        ctx = captured[0]
        assert ctx.notify is not None
        assert ctx.ask is not None
        assert ctx.fetch_images is not None
        assert ctx.fetch_files is not None
    finally:
        bot.close()
        bot.c.close()


# ---- REV-12：杂项边界与网关/反思容错 ----


def test_chime_uses_dedicated_reply_rule(tmp_path: Path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(lambda messages, json_mode: "嗯嗯")
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    try:
        pid = companion.people.ensure("qq", FRIEND, "小明")
        bundle = companion.context.build(
            person_id=pid,
            body="qq",
            channel=f"group:{GROUP}",
            query="今天晚霞很好看",
            now=T0,
            reply_mode="chime",
        )
        system_text = "\n".join(m.content for m in bundle.messages if m.role == "system")
        assert "不是刚和你聊完" in system_text
        assert "你们刚刚还在聊" not in system_text

        ev = Event(
            ts=T0,
            body="qq",
            channel=f"group:{GROUP}",
            actor=Actor(platform="qq", platform_id=FRIEND, person_id=pid, display_name="小明"),
            kind="message",
            content="今天晚霞很好看",
        )
        src = Source("group", FRIEND, GROUP, "小明", "1", Incoming(text="今天晚霞很好看"))
        pending = _Pending(kind="chime", sources=[src], events=[ev])
        assert pending.mode == "chime"
    finally:
        companion.close()


def test_chat_history_parses_filter_when_keyword_omitted():
    ctx = SkillContext("qq", f"group:{GROUP}", "p1", False, False, now=T0)
    q1 = _Query.parse(["3天内"], ctx)
    assert q1.words == []
    assert q1.since == T0 - timedelta(days=3)

    q2 = _Query.parse(["2小时内"], ctx)
    assert q2.words == []
    assert q2.since == T0 - timedelta(hours=2)

    q3 = _Query.parse(["图片"], ctx)
    assert q3.words == []
    assert q3.images_only is True

    q4 = _Query.parse(["谁：小明", "全部"], ctx)
    assert q4.words == []
    assert q4.who == "小明"
    assert q4.channel == "*"


def test_skills_prompt_deduplicates_overridden_markers():
    class SkillA(Skill):
        name = "a"
        markers = {"任务": "旧任务说明"}

        def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
            return SkillResult("a")

    class SkillB(Skill):
        name = "b"
        markers = {"任务": "群沙箱任务说明"}

        def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
            return SkillResult("b")

    skills = Skills([SkillA(), SkillB()], SkillsConfig(enabled=True))
    ctx = SkillContext("qq", f"group:{GROUP}", "p1", False, False)
    stable, _ = skills.prompt_parts(ctx)
    session_stable, _ = skills.session_prompt_parts(ctx)
    for text in (stable, session_stable):
        assert "群沙箱任务说明" in text
        assert "旧任务说明" not in text
        assert text.count("群沙箱任务说明") == 1


def test_group_agent_refunds_quota_when_stopped_early(tmp_path: Path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    config.skills.group_agent.enabled = True
    config.skills.group_agent.limit_per_group = 1
    from companion.skills.access import FileAccess

    access = FileAccess(config.skills.files, tmp_path, config.skills.agent.workspace, deny=[])
    ga = GroupAgent(
        config.skills.group_agent,
        tmp_path,
        config.models.subscription,
        access,
        lambda: [],
        tmp_path / "run",
        quota_ok=lambda: True,
        clock=lambda: T0,
    )
    stop = threading.Event()
    ctx = SkillContext("qq", f"group:{GROUP}", "p1", False, False, stop_event=stop)
    res = ga.run("任务", "画一张表", ctx)
    assert res.later is not None
    stop.set()
    early = res.later()
    assert early.ok is False
    # 配额已退还：紧接着再派一个任务不应被限额拦截
    res2 = ga.run("任务", "再试一次", SkillContext("qq", f"group:{GROUP}", "p1", False, False))
    assert res2.later is not None


def test_openai_compat_wraps_non_json_200_response():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")
    )
    client = OpenAICompatClient(
        name="test",
        base_url="https://example.invalid/v1",
        model="m",
        api_key="key",
        transport=transport,
    )
    try:
        with pytest.raises(ModelError, match="非 JSON 响应") as exc:
            client.chat([ChatMessage("user", "hi")])
        assert exc.value.retryable is True
    finally:
        client.close()


def test_gateway_subscription_ready_guards_stale_reset_and_releases_lock():
    lock_held_during_rpc = {"value": None}
    calls = {"n": 0}

    class FakeSub:
        name = "subscription"
        last_rate_limits: RateLimits | None = None

        def read_rate_limits(self) -> RateLimits:
            calls["n"] += 1
            # 如果 _subscription_ready 持锁调用 RPC，非阻塞 acquire 会返回 False
            acquired = gw._lock.acquire(blocking=False)
            lock_held_during_rpc["value"] = not acquired
            if acquired:
                gw._lock.release()
            self.last_rate_limits = RateLimits(
                windows=[
                    RateWindow(
                        used_percent=95.0,
                        window_minutes=300,
                        resets_at=T0 - timedelta(minutes=5),
                    )
                ]
            )
            return self.last_rate_limits

    sub = FakeSub()
    gw = ModelGateway(
        subscription=sub,  # type: ignore[arg-type]
        switch_at_used_percent=85.0,
    )
    assert gw.subscription_available(now=T0) is False
    assert lock_held_during_rpc["value"] is False
    # 过期 resets_at 被兜底为 DEFAULT_COOLDOWN，1 分钟后不会重复触发 RPC
    assert gw.subscription_available(now=T0 + timedelta(minutes=1)) is False
    assert calls["n"] == 1


def test_sleeper_weekly_people_isolates_single_person_failure(tmp_path: Path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    layout = MemoryLayout(config.memory_dir)
    init_memory(layout, "白璎", git=False)
    gateway, _ = scripted_gateway(lambda messages, json_mode: "{}")
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    try:
        p1 = companion.people.ensure("qq", "3001", "甲")
        p2 = companion.people.ensure("qq", "3002", "乙")
        for pid in (p1, p2):
            companion.index.add_episode(
                Episode(
                    started_at=T0 - timedelta(hours=2),
                    ended_at=T0 - timedelta(hours=1),
                    body="qq",
                    channel=f"group:{GROUP}",
                    summary=f"和 {pid} 聊天",
                    participants=[pid],
                    event_ids=[],
                ),
                now=T0,
            )
        sleeper = Sleeper(companion)
        seen: list[str] = []

        def fake_one(person: Any, **_kwargs: Any) -> bool:
            seen.append(person.person_id)
            if len(seen) == 1:
                raise ModelError("temporary failure")
            return True

        sleeper._reflect_one_person_weekly = fake_one  # type: ignore[method-assign]
        errs: list[str] = []
        updated = sleeper._reflect_people_weekly(T0 - timedelta(days=7), errors=errs)
        assert len(seen) == 2
        assert len(errs) == 1
        assert len(updated) == 1
        assert updated[0] == seen[1]
    finally:
        companion.close()


def test_chat_history_runs_with_omitted_keyword(tmp_path: Path):
    log = EventLog(tmp_path / "events")
    log.append(
        Event(
            ts=T0 - timedelta(hours=1),
            body="qq",
            channel=f"group:{GROUP}",
            actor=Actor(platform="qq", platform_id=FRIEND, display_name="小明"),
            kind="message",
            content="发了张图 [图片：猫]",
            meta={"images": ["abcdef12"]},
        )
    )
    history = ChatHistory(HistorySkillConfig(), log, ChatScope(HistorySkillConfig()))
    ctx = SkillContext("qq", f"group:{GROUP}", "p1", False, False, now=T0)
    res = history.run("翻记录", "图片", ctx)
    assert "发了张图" in res.text
