"""QQ 身体：收 NapCat 的消息事件，决定要不要开口，处理记忆指令。

发言策略：
- 私聊：总是回复。
- 群聊（只限配置里允许的群）：被 @、被叫名字、或有人引用她的话时回复；
  否则只记下来。若话题和她的某条记忆强相关，按一定概率、隔一段时间才主动插一句。
- 全局每分钟发言上限，防刷屏、降低账号风控风险。

Companion 不是线程安全地被多处同时使用的，所以所有对它的调用都放进同一个工作线程。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from companion import media
from companion.bodies.qq.message import (
    FORWARD_UNREAD,
    FileRef,
    ForwardRef,
    ImageRef,
    Incoming,
    parse_segments,
    render_forward,
    text_segments,
)
from companion.bodies.qq.onebot import OneBotClient, OneBotError
from companion.config import QQConfig
from companion.core.actions import Actions, emoji_id
from companion.core.companion import Companion, Reply
from companion.life.activities import ActivityPrep
from companion.media import MediaError
from companion.memory.events import Event
from companion.memory.index import MemoryKind
from companion.memory.people import SELF_ID, PeopleError
from companion.models.openai_compat import ModelError
from companion.skills import SkillCall, SkillContext
from companion.skills.files import FileInbox
from companion.skills.reminder import Reminder
from companion.stickers import Sticker

log = logging.getLogger(__name__)

T = TypeVar("T")

OCR_MAX_CHARS = 150  # 图里的字最多带这么多
POKED = "（戳了戳你）"  # 被戳一戳时交给她的话
REMINDER_GIVE_UP = timedelta(days=1)  # 到点一天后还发不出去的提醒就放弃
REMINDER_MAX_WAIT_MINUTES = 30  # 提醒发不出去时，最多隔这么久再试
LONG_CHUNK = 400  # 打包成聊天记录时，每条最多这么多字

NOTICE = """大家好，我是{name}，一个 AI。
我会记住群里的聊天，这样以后还能记得和大家一起经历过的事。
记忆保存在主人的电脑上；聊天时，相关的内容会发给 AI 模型服务（OpenAI 或 DeepSeek）用来生成回复。
图片：发给我的、或我刚在群里说完话那一分钟里的图片，我会直接看；其他时候的图片平时不看，\
但有人让我翻记录时可能回头看。看过的会发给模型服务，并保存在主人电脑上。
聊天记录：我可以翻这个群的记录，也可能在主人接入的其他群里被问到时翻\
（私聊的记录不会被翻到群里）。主人也能让我翻。
群文件夹：这个群有自己的文件夹（在主人电脑上）。我可以把图片和文字存进去，也可以发给群里任何人。\
存进去的东西不受 /忘掉 影响，要删请找主人。
想让我记住什么：发「/记住 内容」
想让我忘掉什么：发「/忘掉 关键词」，我会列出相关的记忆让你挑"""

NOTICE_UPDATE = """我能做的事多了一些，和大家说一声：
图片：平时没看的图片，有人让我翻记录时我可能回头看\
（看过的会发给模型服务，并保存在主人电脑上）。
聊天记录：我可以翻这个群的记录，也可能在主人接入的其他群里被问到时翻\
（私聊的记录不会被翻到群里）。主人也能让我翻。
群文件夹：这个群有了自己的文件夹（在主人电脑上）。我可以把图片和文字存进去，也可以发给群里任何人。\
存进去的东西不受 /忘掉 影响，要删请找主人。
完整说明发「/说明」。"""

HELP = """可用指令：
/记住 内容 —— 立刻记下，不会忘
/忘掉 关键词 —— 列出相关记忆，再用 /确认忘掉 编号 删除
/绑定 平台 账号 —— 比如 /绑定 minecraft Steve，需主人确认
/说明 —— 我会怎样使用聊天记录"""

FORGET_TTL = timedelta(minutes=10)
BIND_PREFIX = "bind_request:"


_MODE_RANK = {"other": 0, "chime": 1, "partner": 2, "required": 3}
MAX_SPEAKER_NAMES = 200  # 每个群记住多少个最近说过话的名字（用来把她写的“@名字”变成真正的 @）


@dataclass
class _Pending:
    """等待合并回复的一组消息（同一个聊天）。"""

    sources: list[Source] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    kind: str = "other"
    required_message_id: str | None = None  # 最后一条直接叫她的消息，群里回复时引用它
    task: asyncio.Task[None] | None = None

    def add(self, src: Source, event: Event, kind: str) -> None:
        self.sources.append(src)
        self.events.append(event)
        if _MODE_RANK[kind] > _MODE_RANK[self.kind]:
            self.kind = kind
        if kind == "required":
            self.required_message_id = src.message_id

    @property
    def mode(self) -> str:
        """交给 Companion 的回复模式：required 一般要回；optional 可沉默；strict 更倾向沉默。"""
        return {"required": "required", "partner": "optional", "chime": "optional"}.get(
            self.kind, "strict"
        )


@dataclass
class Source:
    """一条 QQ 消息的来龙去脉。"""

    message_type: str  # private / group
    user_id: str
    group_id: str
    name: str
    message_id: str | None
    incoming: Incoming
    received_at: datetime | None = None  # 消息发出时间（OneBot 的 time 字段）

    @property
    def channel(self) -> str:
        return (
            f"group:{self.group_id}" if self.message_type == "group" else f"private:{self.user_id}"
        )


class QQBot:
    def __init__(
        self,
        companion: Companion,
        cfg: QQConfig,
        *,
        client: Any | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] | None = None,
        on_daily: Callable[[], object] | None = None,
    ) -> None:
        self.c = companion
        self.cfg = cfg
        token = cfg.access_token.get_secret_value() if cfg.access_token else None
        self.client = client or OneBotClient(cfg.ws_url, token, self.handle_event)
        self.self_id = cfg.bot_account or ""
        self.rng = rng or random.Random()
        self.now = clock or (lambda: datetime.now(UTC))
        self.on_daily = on_daily
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion")
        self._sent_times: deque[datetime] = deque()
        self._sent_ids: deque[str] = deque(maxlen=500)
        self._last_unsolicited: dict[str, datetime] = {}
        self._last_spoke: dict[str, datetime] = {}
        self._partners: dict[str, dict[str, datetime]] = {}  # 频道 → {刚和她聊过的人: 时间}
        self._speakers: dict[str, dict[str, str]] = {}  # 群 → {最近说过话的名字: QQ 号}
        self._pending: dict[str, _Pending] = {}
        self._jobs: set[asyncio.Task[None]] = set()
        self._approvals: dict[str, list[Any]] = {}  # 审批码 → [threading.Event, 是否批准]
        self._bot_rounds: dict[str, int] = {}  # 频道 → 和别的机器人连着聊了几轮
        self._bot_paused_until: dict[str, datetime] = {}
        self._forget_offers: dict[str, tuple[datetime, list[tuple[MemoryKind, str, str]]]] = {}
        self._activity: asyncio.Task[None] | None = None  # 正在做的自主活动
        self._last_poke: dict[str, datetime] = {}  # 频道 → 上次理会 / 发出戳一戳的时间
        self._reminder_text: dict[int, tuple[str, Sticker | None]] = {}  # 写好、还没发出去的提醒
        self._reminder_retry: dict[int, tuple[int, datetime]] = {}  # 提醒 → （失败次数，下次再试）
        skills = companion.config.skills
        self.inbox = FileInbox(skills.files, companion.config.root, skills.agent.workspace)

    # ---- 运行 ----

    async def run(self) -> None:
        await asyncio.gather(
            self.client.run_forever(),
            self._maintenance(),
            self._life_loop(),
            self._reminder_loop(),
        )

    # ---- 定时提醒 ----

    async def _reminder_loop(self, every: float = 15.0) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.reminder_tick()
            except Exception:
                log.exception("定时提醒出错")

    async def reminder_tick(self) -> None:
        """到点的提醒发出去。NapCat 断线时先不动，连上后再补发（她会说明迟到了）。"""
        skills = self.c.config.skills
        connected = getattr(self.client, "connected", None)
        if not (skills.enabled and skills.reminders.enabled):
            return
        if connected is not None and not connected.is_set():
            return
        now = self.now()
        for reminder in await self._call(self.c.reminders.due, now):
            retry = self._reminder_retry.get(reminder.id)
            if retry is None or now >= retry[1]:
                await self._fire_reminder(reminder, now)

    async def _fire_reminder(self, reminder: Reminder, now: datetime) -> None:
        account = reminder.channel.removeprefix("private:")
        if not account.isdigit():  # 只发回设提醒的私聊
            log.error("提醒 %d 的聊天不对，不发：%s", reminder.id, reminder.channel)
            await self._call(self.c.reminders.finish, reminder.id, now)
            return
        written = self._reminder_text.get(reminder.id)
        if written is None:  # 只写一次：重发时不再调用模型，也不会说出两版
            written = await self._call(self.c.remind_text, reminder, now)
            self._reminder_text[reminder.id] = written
        text, sticker = written
        src = Source("private", account, "", "", None, Incoming(text=""))
        self._sent_times.append(now)  # 这是答应过的事，不受每分钟上限限制，但算进去
        if not await self._send_reply(src, text):
            self._reminder_failed(reminder, now)
            return
        await self._call(self.c.reminder_sent, reminder, text, sticker, now)
        self._reminder_text.pop(reminder.id, None)
        self._reminder_retry.pop(reminder.id, None)
        if sticker is not None:
            await self._send_sticker(src, sticker.digest)

    def _reminder_failed(self, reminder: Reminder, now: datetime) -> None:
        """发不出去（比如对方把她删了）：隔一会儿再试，隔得越来越久；过了一天还不行就放弃。"""
        if now - reminder.due > REMINDER_GIVE_UP:
            log.error("提醒 %d 一直发不出去，放弃：%s", reminder.id, reminder.what)
            self.c.reminders.finish(reminder.id, now)
            self._reminder_text.pop(reminder.id, None)
            self._reminder_retry.pop(reminder.id, None)
            return
        attempts = self._reminder_retry.get(reminder.id, (0, now))[0] + 1
        wait = min(2**attempts, REMINDER_MAX_WAIT_MINUTES)
        self._reminder_retry[reminder.id] = (attempts, now + timedelta(minutes=wait))
        log.warning("提醒 %d 没发出去，%d 分钟后再试", reminder.id, wait)

    # ---- 主动找主人、自主活动 ----

    async def _life_loop(self, every: float = 60.0) -> None:
        """每分钟看一次：该不该跟主人说早安 / 晚安；额度充足时要不要自己找点事做。"""
        while True:
            await asyncio.sleep(every)
            try:
                await self.life_tick()
            except Exception:
                log.exception("主动找主人 / 自主活动出错")

    async def life_tick(self) -> None:
        now = self.now()
        owner = await self._owner()
        if owner is not None and self._can_send():
            kind = await self._call(self.c.life.owner.greeting_due, owner[1], owner[0].channel, now)
            if kind is not None:
                reply = await self._call(
                    self.c.greet, person_id=owner[1], body="qq", channel=owner[0].channel,
                    kind=kind, now=now,
                )  # fmt: skip
                if reply is not None:
                    await self._send_proactive(owner[0], reply, [])
        if self._activity is not None and not self._activity.done():
            return  # 上一件还没做完
        prep = await self._call(
            self.c.prepare_activity,
            now,
            owner_id=owner[1] if owner else None,
            owner_channel=owner[0].channel if owner else None,
        )
        if prep is not None:
            self._activity = asyncio.create_task(self._do_activity(prep, owner))
            self._jobs.add(self._activity)
            self._activity.add_done_callback(self._jobs.discard)

    async def _do_activity(self, prep: ActivityPrep, owner: tuple[Source, str] | None) -> None:
        # 做事（几十秒到几分钟）放在别的线程，不占用记忆核心的工作线程，聊天照常
        result = await asyncio.to_thread(self.c.activities.perform, prep)
        reply, images = await self._call(
            self.c.finish_activity,
            result,
            self.now(),
            owner_id=owner[1] if owner else None,
            owner_channel=owner[0].channel if owner else None,
        )
        if reply is not None and owner is not None:
            await self._send_proactive(owner[0], reply, images)

    async def _owner(self) -> tuple[Source, str] | None:
        """主人的私聊（第一个主人 QQ 号）和 ta 的人物 id；没配置主人就是 None。"""
        if not self.cfg.owner_accounts:
            return None
        account = self.cfg.owner_accounts[0]
        pid = await self._call(self.c.people.ensure, "qq", account, None)
        src = Source("private", account, "", "", None, Incoming(text=""))
        return src, pid

    async def _send_proactive(self, src: Source, reply: Reply, images: list[Path]) -> None:
        self._sent_times.append(self.now())
        if reply.text.strip():
            await self._send_reply(src, reply.text)
        await self._do_actions(src, reply.actions, None)
        for path in images:
            await self._send_image(src, path)
        if reply.sticker is not None:
            await self._send_sticker(src, reply.sticker.digest)

    async def _maintenance(self, every: float = 300.0) -> None:
        """定时整理已结束的会话；每天到点做一次“睡眠”；确保今天有安排。"""
        last_daily: str | None = None
        while True:
            await asyncio.sleep(every)
            try:
                await self._call(self.c.extract_pending)
                local = self.now().astimezone()
                if self.on_daily and local.hour >= self.c.config.sleep.hour:
                    today = f"{local:%Y-%m-%d}"
                    if today != last_daily:
                        last_daily = today
                        await self._call(self.on_daily)
                await self._call(self.c.life.ensure_plan)  # 今天的安排（睡眠时没排成就补上）
            except Exception:
                log.exception("后台整理出错")

    async def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    # ---- 事件 ----

    async def handle_event(self, ev: dict[str, Any]) -> None:
        post_type = ev.get("post_type")
        if post_type == "meta_event":
            if ev.get("meta_event_type") == "lifecycle" and ev.get("self_id"):
                self.self_id = str(ev["self_id"])
                await self._announce_groups()
                if self.cfg.backfill:  # 每次连上都补一次离线期间的群消息（按消息编号去重）
                    self._spawn(self.backfill())
            return
        if post_type == "notice":
            await self._notice(ev)
            return
        if post_type != "message":
            return  # message_sent（自己发出的回显）、request 暂不处理
        self.self_id = str(ev.get("self_id") or self.self_id)
        src = self._source(ev)
        if src is None:
            return
        if src.message_type == "group":
            await self._note_speaker(src, ev.get("sender") or {})
        text = src.incoming.text
        if text[:1] in ("/", "／"):
            await self._command(src, text[1:].strip())
            return

        addressed = (
            src.message_type == "private"
            or src.incoming.mentioned_self
            or (src.incoming.reply_to is not None and src.incoming.reply_to in self._sent_ids)
            or any(alias in text for alias in self.cfg.aliases)
        )
        text, digests = await self._enrich(src, addressed)
        meta: dict[str, object] = {}
        if src.message_id:
            meta["message_id"] = src.message_id
        if digests:
            meta["images"] = digests
        event = await self._call(
            self.c.observe,
            body="qq",
            channel=src.channel,
            account=src.user_id,
            text=text,
            display_name=src.name,
            now=src.received_at,
            meta=meta,
        )

        if self._bot_paused(src):  # 和别的机器人聊太久了，先只听不回
            return

        # 决定这条消息算哪一类：必须回 / 对话延续中 / 主动插话 / 只记录
        if addressed:
            kind = "required"
        else:
            kind = self._conversation_kind(src)
            if kind is None and await self._should_chime_in(src.channel, text):
                kind = "chime"
        if kind is None:
            return
        self._enqueue(src, event, kind)

    # ---- 消息合并与对话延续 ----

    def _conversation_kind(self, src: Source) -> str | None:
        """群里没 @她 的消息：她在“活跃”中（刚说完话的 conversation_seconds 秒内）才考虑接话。

        刚和她聊的人接着说 → partner（交给模型判断要不要接）；别人插话 → other（判断更严格）。
        """
        if src.message_type != "group":
            return None
        last = self._last_spoke.get(src.channel)
        window = timedelta(seconds=self.cfg.conversation_seconds)
        if last is None or self.now() - last > window:
            return None
        partners = self._partners.get(src.channel, {})
        seen = partners.get(src.user_id)
        if seen is not None and self.now() - seen <= window:
            return "partner"
        return "other" if self.cfg.others_join_conversation else None

    def _enqueue(self, src: Source, event: Event, kind: str) -> None:
        """先不急着回：等一小会儿，把接连发来的几条（文字、图片、表情）合成一轮再回。"""
        pending = self._pending.get(src.channel)
        if pending is None:
            pending = _Pending()
            self._pending[src.channel] = pending
        pending.add(src, event, kind)
        if pending.task is not None:
            pending.task.cancel()
        delay = (
            self.cfg.debounce_private_seconds
            if src.message_type == "private"
            else self.cfg.debounce_group_seconds
        )
        pending.task = asyncio.create_task(self._flush_later(src.channel, delay))

    async def _flush_later(self, channel: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        pending = self._pending.pop(channel, None)
        if pending is None:
            return
        me = asyncio.current_task()
        if me is not None:  # 回复中的也算进 drain 要等的
            self._jobs.add(me)  # type: ignore[arg-type]
        try:
            await self._reply_batch(pending)
        finally:
            if me is not None:
                self._jobs.discard(me)  # type: ignore[arg-type]

    async def drain(self) -> None:
        """等所有待回的消息处理完（测试与退出时用）。"""
        while self._pending or self._jobs:
            tasks = [p.task for p in self._pending.values() if p.task is not None]
            tasks += list(self._jobs)
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reply_batch(self, pending: _Pending) -> None:
        src = pending.sources[-1]
        # 上限是防刷屏、防风控的；主人私聊是一对一，当电脑助手用时连着说几句很正常，不能悄悄不回
        owner_private = src.message_type == "private" and src.user_id in self.cfg.owner_accounts
        if not owner_private and not self._can_send():
            log.warning("发言过于频繁，这一轮只记录不回复")
            return
        loop = asyncio.get_running_loop()

        def interim(text: str) -> None:
            """她用技能前先说的话（如“我查一下”）、后台任务的进度：从别的线程里立刻发出去。"""
            asyncio.run_coroutine_threadsafe(self._send(src, text), loop).result(timeout=30)

        ctx = SkillContext(
            body="qq",
            channel=src.channel,
            person_id=pending.events[-1].actor.person_id or "",
            private=src.message_type == "private",
            owner=src.user_id in self.cfg.owner_accounts,
            now=src.received_at,
            notify=interim,
            ask=lambda question: self._ask_owner(src, question, loop),
            fetch_images=lambda message_id: asyncio.run_coroutine_threadsafe(
                self._message_images(message_id), loop
            ).result(timeout=90),
        )
        try:
            reply = await self._call(
                self.c.respond,
                pending.events[-1],
                mode=pending.mode,
                batch=pending.events,
                skill_ctx=ctx,
                on_interim=interim,
            )
        except ModelError as e:
            log.error("生成回复失败：%s", e)
            if src.message_type == "private":
                await self._send(src, "（我这会儿有点恍神，等一下再聊好吗）")
            return
        for call in reply.later:  # 画图、Agent 等耗时的活：后台做，做完再发
            task = asyncio.create_task(self._run_later(src, ctx, call))
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)
        if reply.silent:
            log.info("%s：这一轮选择不回复", src.channel)
            return
        self._sent_times.append(self.now())
        # 群里被直接叫到时引用那条消息，让大家知道她在回谁
        quote = None
        if src.message_type == "group" and pending.mode == "required":
            quote = pending.required_message_id
        if reply.text.strip():
            await self._send_reply(src, reply.text, reply_to=quote)
        actions = reply.actions
        poker = next((s for s in reversed(pending.sources) if s.incoming.text == POKED), None)
        if actions.poke and poker is None:  # 只能戳回戳了她的人，不随手戳别人
            log.info("%s：没人戳她，不戳", src.channel)
            actions.poke = False
        await self._do_actions(poker or src, actions, pending.sources[-1].message_id)
        for path in reply.images:
            await self._send_image(src, path)
        for path in reply.files:
            await self._send_file(src, path)
        if reply.sticker is not None:
            await self._send_sticker(src, reply.sticker.digest)
        partners = self._partners.setdefault(src.channel, {})
        for s in pending.sources:
            partners[s.user_id] = self.now()
        if any(s.user_id in self.cfg.peer_bots for s in pending.sources):
            self._count_bot_round(src.channel)

    async def _run_later(self, src: Source, ctx: SkillContext, call: SkillCall) -> None:
        assert call.later is not None
        result = await asyncio.to_thread(call.later)  # 不占用记忆核心的工作线程，聊天照常
        if result.report:  # 让她用自己的话转述结果
            text, files = await self._call(self.c.report, ctx, call.skill, result)
            await self._send_reply(src, text)
            for path in files:
                await self._send_file(src, path)
            return
        if not result.ok:
            log.warning("%s：后台技能没成功：%s", src.channel, result.text)
            await self._send(src, "（" + result.text[:80] + "）")
            return
        paths = await self._call(self.c.deliver, "qq", src.channel, result, self.now())
        for path in paths:
            await self._send_image(src, path)

    # ---- 防止和别的机器人无限对聊 ----

    def _bot_paused(self, src: Source) -> bool:
        """别的机器人发来的消息：和它们连着聊满轮数后暂停回复。真人一说话就重新计数。"""
        channel = src.channel
        if src.user_id not in self.cfg.peer_bots:
            self._bot_rounds.pop(channel, None)
            self._bot_paused_until.pop(channel, None)
            return False
        until = self._bot_paused_until.get(channel)
        if until is None:
            return False
        if self.now() < until:
            log.info("%s：和机器人聊了 %d 轮，暂停中，不回复", channel, self.cfg.max_bot_rounds)
            return True
        self._bot_paused_until.pop(channel, None)
        return False

    def _count_bot_round(self, channel: str) -> None:
        rounds = self._bot_rounds.get(channel, 0) + 1
        if rounds >= self.cfg.max_bot_rounds:
            self._bot_rounds.pop(channel, None)
            self._bot_paused_until[channel] = self.now() + timedelta(
                minutes=self.cfg.bot_pause_minutes
            )
            log.info("%s：和机器人连着聊了 %d 轮，先停一下", channel, rounds)
        else:
            self._bot_rounds[channel] = rounds

    def _source(self, ev: dict[str, Any]) -> Source | None:
        user_id = str(ev.get("user_id", ""))
        if not user_id or user_id == self.self_id:
            return None
        message_type = ev.get("message_type")
        group_id = str(ev.get("group_id") or "")
        if message_type == "group":
            if group_id not in self.cfg.groups:
                return None
        elif message_type != "private":
            return None
        sender = ev.get("sender") or {}
        name = str(sender.get("card") or sender.get("nickname") or user_id)
        incoming = parse_segments(ev.get("message"), self.self_id)
        if not incoming.text:
            if not incoming.mentioned_self:
                return None
            incoming.text = "（@了你一下）"
        message_id = str(ev["message_id"]) if ev.get("message_id") is not None else None
        received = datetime.fromtimestamp(ev["time"], UTC) if ev.get("time") else None
        return Source(message_type, user_id, group_id, name, message_id, incoming, received)

    # ---- 看图 ----

    def _active(self, channel: str) -> bool:
        """她刚在这个频道说过话（active_seconds 秒内）。"""
        last = self._last_spoke.get(channel)
        window = timedelta(seconds=self.c.config.vision.active_seconds)
        return last is not None and self.now() - last <= window

    async def _enrich(self, src: Source, addressed: bool) -> tuple[str, list[str]]:
        """补全一条消息：按规则看图、写明引用了谁的什么话；返回（正文，看过的图片哈希）。

        看图：私聊、@她、引用并 @她 时看；群里没冲她来的，只在她活跃时看，否则记作“[图片]”且不下载。
        转发的聊天记录总是展开成文字（记录里的图不看）。
        """
        inc = src.incoming
        vision = self.c.config.vision
        read = vision.enabled and (addressed or self._active(src.channel))
        descriptions: list[str] = []
        digests: list[str] = []
        for ref in inc.images:
            label = "表情包" if ref.sticker else "图片"
            if not read:
                descriptions.append(await self._unseen(ref, label, "没看"))  # 明说没看，免得她猜
                continue
            desc, digest = await self._look(ref, src)
            descriptions.append(
                f"[{label}：{desc}]" if desc else await self._unseen(ref, label, "没看清")
            )
            if digest:
                digests.append(digest)
        forwards = [await self._read_forward(ref) for ref in inc.forwards]
        receive = addressed or self._active(src.channel)
        files = [await self._receive_file(ref, src, receive) for ref in inc.files]
        voice = await self._hear(src, receive) if inc.voice else None
        text = inc.fill(descriptions, forwards, files, voice)

        if inc.reply_to:
            text += await self._describe_quote(inc.reply_to, read and addressed, digests)
        return text, digests

    async def _describe_quote(self, message_id: str, look: bool, digests: list[str]) -> str:
        """引用了一条消息：写明引用的是谁的什么话；@她 时顺便看被引用的图。"""
        msg = await self._get_message(message_id)
        if msg is None:
            return "（引用了一条消息）"
        sender = msg.get("sender") or {}
        uid = str(sender.get("user_id", ""))
        who = (
            "你"
            if uid == self.self_id
            else str(sender.get("card") or sender.get("nickname") or uid or "某人")
        )
        quoted = parse_segments(msg.get("message"), self.self_id)
        descriptions = []
        for ref in quoted.images:
            label = "表情包" if ref.sticker else "图片"
            desc = None
            if look:
                desc, digest = await self._look(ref, None)
                if digest:
                    digests.append(digest)
            descriptions.append(f"[{label}：{desc}]" if desc else f"[{label}（没看）]")
        body = quoted.fill(descriptions).strip()  # 引用的聊天记录只记作“[聊天记录]”
        if len(body) > 120:
            body = body[:119] + "…"
        said = "你说的" if who == "你" else f"{who}的话"
        return f"（引用了{said}：{body or '……'}）"

    async def _read_forward(self, ref: ForwardRef) -> str:
        """展开一段合并转发：消息里带了内容就直接用，没带再向 NapCat 要；都拿不到就明说没看。"""
        nodes = ref.nodes
        if nodes is None and ref.id:
            try:
                data = await self.client.call("get_forward_msg", {"id": ref.id})
            except (OneBotError, TimeoutError) as e:
                log.warning("取聊天记录失败：%s", e)
            else:  # NapCat 给 {"messages": [...]}，有的实现直接给列表
                nodes = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(nodes, list) or not nodes:
            return FORWARD_UNREAD
        return render_forward(nodes, self.self_id)

    # ---- 图里的字、语音 ----

    async def _unseen(self, ref: ImageRef, label: str, why: str) -> str:
        """没看（或没看清）的图：用 QQ 自带的文字识别读出图里的字（本机做，不花模型额度）。"""
        if self.cfg.ocr and not ref.sticker and (ref.url or ref.file):
            words = await self._ocr(ref)
            if words:
                return f"[{label}（{why}，图里的字：{words}）]"
        return f"[{label}（{why}）]"

    async def _ocr(self, ref: ImageRef) -> str:
        try:
            data = await self.client.call("ocr_image", {"image": ref.url or ref.file}, timeout=30)
        except (OneBotError, TimeoutError) as e:
            log.info("识别图里的字失败：%s", e)
            return ""
        items = data.get("texts", []) if isinstance(data, dict) else data or []
        words = " ".join(str(t.get("text", "")).strip() for t in items if isinstance(t, dict))
        words = " ".join(words.split())
        return words if len(words) <= OCR_MAX_CHARS else words[: OCR_MAX_CHARS - 1] + "…"

    async def _hear(self, src: Source, receive: bool) -> str:
        """一段语音：发给她的就用 QQ 自带的语音转文字听一下。"""
        if not (self.cfg.voice_in and receive and src.message_id):
            return "[语音（没听）]"
        try:
            data = await self.client.call(
                "fetch_ptt_text", {"message_id": int(src.message_id)}, timeout=30
            )
        except (OneBotError, TimeoutError, ValueError) as e:
            log.info("语音转文字失败：%s", e)
            return "[语音（没听清）]"
        text = str((data.get("text") if isinstance(data, dict) else data) or "").strip()
        return f"[语音：{text}]" if text else "[语音（没听清）]"

    # ---- 回应、戳一戳、用声音说话、长内容 ----

    async def _do_actions(self, src: Source, actions: Actions, message_id: str | None) -> None:
        if actions.voice and self.cfg.voice_out:
            await self._send_voice(src, actions.voice)
        elif actions.voice:
            await self._send(src, actions.voice)
        emoji = emoji_id(actions.reaction) if actions.reaction else None
        if emoji and message_id and self.cfg.reactions:
            try:
                params = {"message_id": int(message_id), "emoji_id": emoji, "set": True}
                await self.client.call("set_msg_emoji_like", params)
            except (OneBotError, ValueError) as e:
                log.info("表情回应失败：%s", e)
        if actions.poke and self.cfg.poke:
            await self._poke(src)

    async def _poke(self, src: Source) -> None:
        params: dict[str, object] = {"user_id": src.user_id}
        if src.message_type == "group":
            params["group_id"] = src.group_id
        try:
            await self.client.call("send_poke", params)
        except OneBotError as e:
            log.info("戳一戳失败：%s", e)
            return
        self._last_poke[src.channel] = self.now()

    async def _send_voice(self, src: Source, text: str) -> None:
        """用 QQ 的 AI 声音说一句，只在群里。

        私聊做不到：NapCat 的 get_ai_record 和 send_group_ai_record 调的是同一个 QQ 功能，
        生成语音的同时就把它发到了那个群里（2026-10-28 私聊试语音时，群里多出了三条）。
        所以私聊里她说的“语音”一律改发文字。
        """
        if src.message_type != "group":
            await self._send(src, text)
            return
        params = {"group_id": src.group_id, "character": self.cfg.voice_character, "text": text}
        try:
            await self.client.call("send_group_ai_record", params, timeout=60)
        except (OneBotError, TimeoutError) as e:
            log.info("用声音说话失败，改发文字：%s", e)
            await self._send(src, text)
            return
        self._last_spoke[src.channel] = self.now()

    async def _send_reply(self, src: Source, text: str, reply_to: str | None = None) -> bool:
        """她说的话：太长就打包成一条聊天记录发（不刷屏），发不出去再分开发。返回是否发出去了。"""
        limit = self.cfg.long_text_chars
        if limit and len(text) > limit and await self._send_forward(src, text):
            return True
        return await self._send(src, text, reply_to=reply_to)

    async def _send_forward(self, src: Source, text: str) -> bool:
        nodes = [
            {"type": "node", "data": {"user_id": self.self_id, "nickname": self.c.name,
                                      "content": [{"type": "text", "data": {"text": chunk}}]}}
            for chunk in split_long(text)
        ]  # fmt: skip
        if src.message_type == "group":
            action, params = "send_group_forward_msg", {"group_id": int(src.group_id)}
        else:
            action, params = "send_private_forward_msg", {"user_id": int(src.user_id)}
        try:
            data = await self.client.call(action, params | {"messages": nodes}, timeout=60)
        except (OneBotError, TimeoutError) as e:
            log.info("打包成聊天记录失败，改成直接发：%s", e)
            return False
        self._last_spoke[src.channel] = self.now()
        if isinstance(data, dict) and data.get("message_id") is not None:
            self._sent_ids.append(str(data["message_id"]))
        return True

    # ---- 戳一戳（通知） ----

    async def _notice(self, ev: dict[str, Any]) -> None:
        """别人戳了她：当成“（戳了戳你）”交给她，同一个聊天里隔一会儿才理一次。"""
        if not (ev.get("notice_type") == "notify" and ev.get("sub_type") == "poke"):
            return
        user_id = str(ev.get("user_id") or "")
        if not self.cfg.poke or str(ev.get("target_id")) != self.self_id or user_id == self.self_id:
            return
        group_id = str(ev.get("group_id") or "")
        if group_id and group_id not in self.cfg.groups:
            return
        kind = "group" if group_id else "private"
        names = (
            {v: k for k, v in (await self._speakers_in(f"group:{group_id}")).items()}
            if group_id
            else {}
        )
        name = names.get(user_id, user_id)
        received = datetime.fromtimestamp(ev["time"], UTC) if ev.get("time") else None
        src = Source(kind, user_id, group_id, name, None, Incoming(text=POKED), received)
        last = self._last_poke.get(src.channel)
        if last is not None and self.now() - last < timedelta(
            seconds=self.cfg.poke_cooldown_seconds
        ):
            return
        self._last_poke[src.channel] = self.now()
        event = await self._call(
            self.c.observe, body="qq", channel=src.channel, account=user_id, text=POKED,
            display_name=None if name == user_id else name, now=received,
        )  # fmt: skip
        if self._bot_paused(src):
            return
        self._enqueue(src, event, "required")

    # ---- 补上离线期间的群消息 ----

    async def backfill(self) -> int:
        """把服务没开时群里的消息补记进来（只记不回）；返回补了几条。"""
        total = 0
        for gid in self.cfg.groups:
            try:
                n = await self._backfill_group(gid)
            except (OneBotError, TimeoutError) as e:
                log.warning("补群 %s 的消息失败：%s", gid, e)
                continue
            if n:
                log.info("补记了群 %s 离线期间的 %d 条消息", gid, n)
            total += n
        return total

    async def _backfill_group(self, gid: str) -> int:
        channel = f"group:{gid}"
        since = self.now() - timedelta(hours=self.cfg.backfill_hours)
        last, known = await self._call(self._channel_tail, channel, since)
        params = {"group_id": gid, "message_seq": 0, "count": self.cfg.backfill_max}
        data = await self.client.call("get_group_msg_history", params, timeout=60)
        messages = (data or {}).get("messages") or [] if isinstance(data, dict) else []
        added = 0
        for m in sorted(messages, key=lambda x: x.get("time") or 0):
            uid, mid = str(m.get("user_id") or ""), str(m.get("message_id") or "")
            if not m.get("time") or uid in ("", self.self_id) or mid in known:
                continue
            ts = datetime.fromtimestamp(m["time"], UTC)
            if ts < since or (last is not None and ts <= last):
                continue
            text = self._offline_text(parse_segments(m.get("message"), self.self_id))
            if not text:
                continue
            sender = m.get("sender") or {}
            name = str(sender.get("card") or sender.get("nickname") or uid)
            await self._call(
                self.c.observe, body="qq", channel=channel, account=uid, text=text,
                display_name=name, now=ts, meta={"message_id": mid, "backfill": True},
            )  # fmt: skip
            added += 1
        return added

    def _offline_text(self, inc: Incoming) -> str:
        """离线时的消息：图、文件、语音都不补看，只说明是什么；聊天记录有内容就展开。"""
        images = [f"[{'表情包' if r.sticker else '图片'}（没看）]" for r in inc.images]
        forwards = [render_forward(r.nodes, self.self_id) if r.nodes else FORWARD_UNREAD
                    for r in inc.forwards]  # fmt: skip
        files = [self.inbox.skipped(r.name, r.size, "没收") for r in inc.files]
        return inc.fill(images, forwards, files, "[语音（没听）]").strip()

    def _channel_tail(self, channel: str, since: datetime) -> tuple[datetime | None, set[str]]:
        """（在工作线程里）这个频道最后一条记录的时间，以及最近记过的消息编号。"""
        last: datetime | None = None
        known: set[str] = set()
        for e in self.c.log.iter_events(since=since):
            if e.channel != channel:
                continue
            if e.meta.get("message_id"):
                known.add(str(e.meta["message_id"]))
            if e.actor.person_id != SELF_ID and (last is None or e.ts > last):
                last = e.ts
        return last, known

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    # ---- 文件 ----

    async def _receive_file(self, ref: FileRef, src: Source, receive: bool) -> str:
        """收一个文件：发给她的（私聊、@她、她刚说完话时）且不太大，就存进工作文件夹的 inbox。"""
        inbox = self.inbox
        if not (inbox.cfg.enabled and receive):
            return inbox.skipped(ref.name, ref.size, "没收")
        if ref.size and ref.size > inbox.max_bytes:
            return inbox.skipped(ref.name, ref.size, f"超过 {inbox.cfg.max_mb:g} MB，没收")
        try:
            data = await self._download_file(ref, src)
            path = await asyncio.to_thread(inbox.save, ref.name, data, self.now(), src.channel)
            text = await asyncio.to_thread(inbox.describe, path, ref.name)
        except (MediaError, OneBotError, OSError, TimeoutError) as e:
            log.warning("收文件失败（%s）：%s", ref.name, e)
            return inbox.skipped(ref.name, ref.size, "没收到")
        log.info("收到文件：%s", path)
        return text

    async def _download_file(self, ref: FileRef, src: Source) -> bytes:
        max_bytes = self.inbox.max_bytes
        url = ref.url
        if not url and ref.file_id:
            # 没有直链时向 NapCat 要：可能给本地路径，也可能给链接
            data = await self.client.call("get_file", {"file_id": ref.file_id}) or {}
            local = data.get("file")
            if local and Path(local).is_file():
                if Path(local).stat().st_size > max_bytes:
                    raise MediaError("文件太大")
                return await asyncio.to_thread(Path(local).read_bytes)
            url = data.get("url")
            if not url and src.message_type == "group":
                params = {"group_id": int(src.group_id), "file_id": ref.file_id}
                url = (await self.client.call("get_group_file_url", params) or {}).get("url")
        if not url:
            raise MediaError("拿不到文件地址")
        return await asyncio.to_thread(media.fetch, url, max_bytes=max_bytes)

    async def _send_file(self, src: Source, path: Path) -> None:
        """发文件（NapCat 和这里在同一台电脑上，直接给本地路径）。"""
        if src.message_type == "group":
            action = "upload_group_file"
            params: dict[str, object] = {"group_id": int(src.group_id)}
        else:
            action = "upload_private_file"
            params = {"user_id": int(src.user_id)}
        params |= {"file": str(path.resolve()), "name": path.name}
        try:
            await self.client.call(action, params, timeout=120.0)
        except (OneBotError, TimeoutError) as e:
            log.error("发文件失败（%s）：%s", path.name, e)
            await self._send(src, f"（文件 {path.name} 没发出去）")
            return
        self._last_spoke[src.channel] = self.now()

    async def _look(self, ref: ImageRef, src: Source | None) -> tuple[str | None, str | None]:
        """看一张图；表情包顺便判断能否收藏，并记下是谁发的。"""
        try:
            data = await self._download(ref)
            pid = None
            if ref.sticker and src is not None:
                pid = await self._call(self.c.people.ensure, "qq", src.user_id, src.name)
            return await self._call(self.c.look, data, sticker=ref.sticker, person_id=pid)
        except (MediaError, ModelError, OneBotError, OSError) as e:
            log.warning("看图失败：%s", e)
            return None, None

    async def _download(self, ref: ImageRef) -> bytes:
        max_bytes = self.c.config.vision.max_bytes
        url = ref.url
        if not url and ref.file:
            # 没有直链时向 NapCat 换：可能给本地路径，也可能给链接
            data = await self.client.call("get_image", {"file": ref.file})
            local = (data or {}).get("file")
            if local and Path(local).is_file():
                return await asyncio.to_thread(Path(local).read_bytes)
            url = (data or {}).get("url")
        if not url:
            raise MediaError("拿不到图片地址")
        return await asyncio.to_thread(media.fetch, url, max_bytes=max_bytes)

    async def _message_images(self, message_id: str) -> list[bytes]:
        """（她回头看当时没看的图）向 QQ 要这条消息里的图片。

        不能经过工作线程：它正在等这里的结果。"""
        msg = await self._get_message(message_id)
        if msg is None:
            return []
        out = []
        for ref in parse_segments(msg.get("message"), self.self_id).images:
            try:
                out.append(await self._download(ref))
            except (MediaError, OneBotError, OSError) as e:
                log.info("取消息 %s 的图片失败：%s", message_id, e)
        return out

    async def _get_message(self, message_id: str) -> dict[str, Any] | None:
        try:
            msg = await self.client.call("get_msg", {"message_id": int(message_id)})
        except (OneBotError, ValueError) as e:
            log.warning("取被引用的消息失败：%s", e)
            return None
        return msg if isinstance(msg, dict) else None

    # ---- 发言策略 ----

    async def _should_chime_in(self, channel: str, text: str) -> bool:
        if not channel.startswith("group:") or len(text) < 4:
            return False
        now = self.now()
        last = self._last_unsolicited.get(channel)
        interval = timedelta(minutes=self.cfg.unsolicited_min_interval_minutes)
        if last is not None and now - last < interval:
            return False
        if self.rng.random() >= self.cfg.unsolicited_probability:
            return False
        hits = await self._call(self.c.retriever.search, text, limit=1)
        if not hits or hits[0].cosine is None or hits[0].cosine < self.cfg.unsolicited_min_cosine:
            return False
        self._last_unsolicited[channel] = now
        return True

    def _can_send(self) -> bool:
        """每分钟发言上限（按“回复了几轮”计；选择沉默的不算）。"""
        now = self.now()
        while self._sent_times and now - self._sent_times[0] > timedelta(minutes=1):
            self._sent_times.popleft()
        return len(self._sent_times) < self.cfg.max_replies_per_minute

    async def _send(self, src: Source, text: str, reply_to: str | None = None) -> bool:
        """发一条文字，返回是否发出去了。"""
        if src.message_type == "group":
            action, params = "send_group_msg", {"group_id": int(src.group_id)}
        else:
            action, params = "send_private_msg", {"user_id": int(src.user_id)}
        attempts = [text_segments(text, reply_to)]
        if src.message_type == "group":  # 群里把“@QQ号”“@名字”变成真正的 @；发不出去再退回纯文字
            names = await self._speakers_in(src.channel)
            with_at = text_segments(text, reply_to, mentions=names, self_id=self.self_id)
            if with_at != attempts[0]:
                attempts.insert(0, with_at)
        for message in attempts:
            try:
                data = await self.client.call(action, params | {"message": message})
                break
            except (OneBotError, TimeoutError) as e:
                log.error("发送失败：%s", e)
        else:
            return False
        self._last_spoke[src.channel] = self.now()
        if isinstance(data, dict) and data.get("message_id") is not None:
            self._sent_ids.append(str(data["message_id"]))
        return True

    async def _speakers_in(self, channel: str) -> dict[str, str]:
        """群里最近说过话的人：名字（群名片、昵称）→ QQ 号。第一次用时从事件日志里补上重启前的。"""
        names = self._speakers.get(channel)
        if names is None:
            loaded = await self._call(self._recent_speakers, channel)
            names = self._speakers.setdefault(channel, loaded)
        return names

    def _recent_speakers(self, channel: str) -> dict[str, str]:
        """（在工作线程里）她在上下文里能看到的那段时间内，这个群里说过话的人。"""
        since = self.now() - timedelta(hours=self.c.config.retrieval.recent_hours)
        names: dict[str, str] = {}
        for e in self.c.log.iter_events(since=since):
            if e.channel == channel and e.actor.platform == "qq" and e.actor.display_name:
                names.pop(e.actor.display_name, None)
                names[e.actor.display_name] = e.actor.platform_id
        return names

    async def _note_speaker(self, src: Source, sender: dict[str, Any]) -> None:
        names = await self._speakers_in(src.channel)
        for name in (sender.get("card"), sender.get("nickname")):
            if name := str(name or "").strip():
                names.pop(name, None)  # 挪到最后：同名时以最近说话的为准
                names[name] = src.user_id
        while len(names) > MAX_SPEAKER_NAMES:
            names.pop(next(iter(names)))

    async def _announce_groups(self) -> None:
        """第一次接入某个群时，主动说明她会怎样使用聊天记录（每个群只说一次）。"""
        for gid in self.cfg.groups:
            key = f"announced:group:{gid}"
            updated_key = f"announced_v2:group:{gid}"  # 能翻记录、有群文件夹以后，要再说一次
            told = await self._call(self.c.index.get_meta, key)
            if told and await self._call(self.c.index.get_meta, updated_key):
                continue
            text = NOTICE_UPDATE if told else NOTICE.format(name=self.c.name)
            try:
                data = await self.client.call(
                    "send_group_msg", {"group_id": int(gid), "message": text_segments(text)}
                )
            except OneBotError as e:
                log.error("向群 %s 发送说明失败：%s", gid, e)
                continue
            if isinstance(data, dict) and data.get("message_id") is not None:
                self._sent_ids.append(str(data["message_id"]))
            stamp = self.now().isoformat()
            await self._call(self.c.index.set_meta, key, stamp)
            await self._call(self.c.index.set_meta, updated_key, stamp)

    async def _send_sticker(self, src: Source, digest: str) -> None:
        await self._send_image(src, self.c.stickers.image_path(digest), sticker=True)

    async def _send_image(self, src: Source, path: Path, *, sticker: bool = False) -> None:
        if not path.exists():
            return
        b64 = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
        data: dict[str, object] = {"file": f"base64://{b64}"}
        if sticker:
            data |= {"sub_type": 1, "summary": "[动画表情]"}
        segment = {"type": "image", "data": data}
        if src.message_type == "group":
            action, params = "send_group_msg", {"group_id": int(src.group_id), "message": [segment]}
        else:
            action, params = "send_private_msg", {"user_id": int(src.user_id), "message": [segment]}
        try:
            await self.client.call(action, params, timeout=60.0)  # 动图、原图比压缩过的大不少
        except (OneBotError, TimeoutError) as e:
            log.error("发送图片失败：%s", e)
            if not sticker:
                await self._send(src, f"（图片 {path.name} 没发出去）")
            return
        self._last_spoke[src.channel] = self.now()

    def _ask_owner(self, src: Source, question: str, loop: asyncio.AbstractEventLoop) -> bool:
        """（在后台线程里）把审批请求发给主人，等 /批准 或 /拒绝；超时算拒绝。"""
        code = f"{self.rng.randrange(1000, 10000)}"
        waiter = threading.Event()
        self._approvals[code] = [waiter, False]
        minutes = self.c.config.skills.agent.approval_timeout_minutes
        text = f"{question}\n回复 /批准 {code} 或 /拒绝 {code}（{minutes:g} 分钟内不回就算拒绝）"
        try:
            asyncio.run_coroutine_threadsafe(self._send(src, text), loop).result(timeout=30)
            waiter.wait(timeout=minutes * 60)
            return bool(self._approvals[code][1])
        finally:
            self._approvals.pop(code, None)

    async def _notify_owners(self, text: str) -> None:
        for owner in self.cfg.owner_accounts:
            try:
                await self.client.call(
                    "send_private_msg",
                    {"user_id": int(owner), "message": text_segments(text)},
                )
            except OneBotError as e:
                log.error("通知主人失败：%s", e)

    # ---- 指令 ----

    async def _command(self, src: Source, line: str) -> None:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        is_owner = src.user_id in self.cfg.owner_accounts
        pid = await self._call(self.c.people.ensure, "qq", src.user_id, src.name)

        if cmd == "记住":
            if not arg:
                await self._send(src, "要我记住什么呢？比如：/记住 我周六要考试")
                return
            event: Event = await self._call(
                self.c.observe,
                body="qq",
                channel=src.channel,
                account=src.user_id,
                text=f"请记住：{arg}",
                display_name=src.name,
            )
            await self._call(self.c.remember, event, arg)
            await self._send(src, "嗯，记下了，不会忘的。")
        elif cmd == "忘掉":
            if not arg:
                await self._send(src, "要我忘掉什么呢？比如：/忘掉 生日")
                return
            offers = await self._call(self.c.forget_candidates, pid, arg)
            if not offers:
                await self._send(src, f"我没找到和「{arg}」有关的、关于你的记忆。")
                return
            self._forget_offers[pid] = (self.now(), offers)
            lines = [f"{i}. {text[:60]}" for i, (_, _, text) in enumerate(offers, 1)]
            await self._send(
                src,
                f"我找到这些和「{arg}」有关的记忆：\n"
                + "\n".join(lines)
                + "\n回复 /确认忘掉 编号（如 /确认忘掉 1 3）或 /确认忘掉 全部；不回复就算了。",
            )
        elif cmd == "确认忘掉":
            await self._confirm_forget(src, pid, arg)
        elif cmd == "绑定":
            await self._request_bind(src, pid, arg, is_owner)
        elif cmd == "确认" and is_owner:
            await self._confirm_bind(src, arg)
        elif cmd == "说明":
            await self._send(src, NOTICE.format(name=self.c.name))
        elif cmd == "状态" and is_owner:
            statuses = await self._call(self.c.gateway.status)
            await self._send(
                src,
                "\n".join(f"{'●' if s.available else '○'} {s.name}：{s.detail}" for s in statuses),
            )
        elif cmd in ("批准", "拒绝") and is_owner and src.message_type == "private":
            entry = self._approvals.get(arg)
            if entry is None:
                await self._send(src, "没有这个审批（可能已经超时了）。")
                return
            entry[1] = cmd == "批准"
            entry[0].set()
            await self._send(src, "好，批准了。" if cmd == "批准" else "好，拒绝了。")
        elif cmd == "停止任务" and is_owner:
            agents = [s for s in self.c.skills.all if s.name in ("agent", "group_agent")]
            stopped = [agent.cancel() for agent in agents]  # type: ignore[attr-defined]
            await self._send(src, "好，让助手停下了。" if any(stopped) else "现在没有在做的任务。")
        elif cmd in ("帮助", "help"):
            await self._send(src, HELP)
        # 其他以 / 开头的消息不理会，避免和别的机器人抢指令

    async def _confirm_forget(self, src: Source, pid: str, arg: str) -> None:
        offer = self._forget_offers.get(pid)
        if offer is None or self.now() - offer[0] > FORGET_TTL:
            await self._send(src, "没有待确认的忘记请求了，先发 /忘掉 关键词 吧。")
            return
        items = offer[1]
        if arg in ("全部", "all"):
            chosen = items
        else:
            nums = {int(n) for n in re.findall(r"\d+", arg)}
            chosen = [it for i, it in enumerate(items, 1) if i in nums]
        if not chosen:
            await self._send(src, "没看懂要忘掉哪几条，用编号告诉我，比如 /确认忘掉 1 2。")
            return
        del self._forget_offers[pid]
        n_mem, n_msg = await self._call(self.c.forget, [(k, m) for k, m, _ in chosen])
        extra = n_mem - len(chosen)
        note = f"（连同引用同一段对话的另外 {extra} 条记忆）" if extra > 0 else ""
        await self._send(src, f"好，已经忘掉了{note}，相关的 {n_msg} 条聊天记录也删了。")

    async def _request_bind(self, src: Source, pid: str, arg: str, is_owner: bool) -> None:
        parts = arg.split()
        if len(parts) != 2:
            await self._send(src, "格式是 /绑定 平台 账号，比如 /绑定 minecraft Steve")
            return
        platform, account = parts[0].lower(), parts[1]
        if is_owner:
            await self._bind(src, platform, account, pid)
            return
        code = f"{self.rng.randrange(10**4):04d}"
        await self._call(
            self.c.index.set_meta,
            BIND_PREFIX + code,
            f"{platform}\t{account}\t{pid}\t{src.name}",
        )
        await self._notify_owners(
            f"{src.name}（QQ {src.user_id}）想把 {platform}:{account} 绑定为 ta 本人。"
            f"同意的话回复 /确认 {code}"
        )
        await self._send(src, "好，已经请主人确认了。")

    async def _confirm_bind(self, src: Source, code: str) -> None:
        raw = await self._call(self.c.index.get_meta, BIND_PREFIX + code.strip())
        if not raw:
            await self._send(src, "没有这个绑定请求。")
            return
        platform, account, pid, name = raw.split("\t")
        await self._bind(src, platform, account, pid, who=name)
        await self._call(self.c.index.set_meta, BIND_PREFIX + code.strip(), "")

    async def _bind(
        self, src: Source, platform: str, account: str, pid: str, who: str | None = None
    ) -> None:
        try:
            await self._call(self.c.people.bind, platform, account, pid)
        except PeopleError as e:
            await self._send(src, f"绑定失败：{e}")
            return
        await self._send(src, f"好，{platform}:{account} 现在就是{who or '你'}了。")


def split_long(text: str, limit: int = LONG_CHUNK) -> list[str]:
    """长内容按段落切成几条（每条不超过 limit 字），打包成聊天记录时一段一条。"""
    chunks: list[str] = []
    current = ""
    for para in [p.strip() for p in text.splitlines() if p.strip()]:
        while len(para) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if current and len(current) + 1 + len(para) > limit:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks or [text]
