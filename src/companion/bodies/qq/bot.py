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
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from companion import media
from companion.bodies.qq.message import ImageRef, Incoming, parse_segments, text_segments
from companion.bodies.qq.onebot import OneBotClient, OneBotError
from companion.config import QQConfig
from companion.core.companion import Companion
from companion.media import MediaError
from companion.memory.events import Event
from companion.memory.index import MemoryKind
from companion.memory.people import PeopleError
from companion.models.openai_compat import ModelError
from companion.skills import SkillContext

log = logging.getLogger(__name__)

T = TypeVar("T")

NOTICE = """大家好，我是{name}，一个 AI。
我会记住群里的聊天，这样以后还能记得和大家一起经历过的事。
记忆保存在主人的电脑上；聊天时，相关的内容会发给 AI 模型服务（OpenAI 或 DeepSeek）用来生成回复。
图片：只有发给我的、或我刚在群里说完话那一分钟里的图片，我才会看（也会发给模型服务），并保存在主人电脑上；其他时候的图片我不看也不存。
想让我记住什么：发「/记住 内容」
想让我忘掉什么：发「/忘掉 关键词」，我会列出相关的记忆让你挑"""

HELP = """可用指令：
/记住 内容 —— 立刻记下，不会忘
/忘掉 关键词 —— 列出相关记忆，再用 /确认忘掉 编号 删除
/绑定 平台 账号 —— 比如 /绑定 minecraft Steve，需主人确认
/说明 —— 我会怎样使用聊天记录"""

FORGET_TTL = timedelta(minutes=10)
BIND_PREFIX = "bind_request:"


_MODE_RANK = {"other": 0, "chime": 1, "partner": 2, "required": 3}


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
        self._pending: dict[str, _Pending] = {}
        self._forget_offers: dict[str, tuple[datetime, list[tuple[MemoryKind, str, str]]]] = {}

    # ---- 运行 ----

    async def run(self) -> None:
        await asyncio.gather(self.client.run_forever(), self._maintenance())

    async def _maintenance(self, every: float = 300.0) -> None:
        """定时整理已结束的会话；每天到点做一次“睡眠”。"""
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
            return
        if post_type != "message":
            return  # message_sent（自己发出的回显）、notice、request 暂不处理
        self.self_id = str(ev.get("self_id") or self.self_id)
        src = self._source(ev)
        if src is None:
            return
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
        if pending is not None:
            await self._reply_batch(pending)

    async def drain(self) -> None:
        """等所有待回的消息处理完（测试与退出时用）。"""
        while self._pending:
            tasks = [p.task for p in self._pending.values() if p.task is not None]
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reply_batch(self, pending: _Pending) -> None:
        src = pending.sources[-1]
        if not self._can_send():
            log.warning("发言过于频繁，这一轮只记录不回复")
            return
        loop = asyncio.get_running_loop()

        def interim(text: str) -> None:
            """她用技能前先说的话（如“我查一下”）：从工作线程里立刻发出去。"""
            asyncio.run_coroutine_threadsafe(self._send(src, text), loop).result(timeout=30)

        ctx = SkillContext(
            body="qq",
            channel=src.channel,
            person_id=pending.events[-1].actor.person_id or "",
            private=src.message_type == "private",
            owner=src.user_id in self.cfg.owner_accounts,
            now=src.received_at,
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
        if reply.silent:
            log.info("%s：这一轮选择不回复", src.channel)
            return
        self._sent_times.append(self.now())
        # 群里被直接叫到时引用那条消息，让大家知道她在回谁
        quote = None
        if src.message_type == "group" and pending.mode == "required":
            quote = pending.required_message_id
        if reply.text.strip():
            await self._send(src, reply.text, reply_to=quote)
        for path in reply.images:
            await self._send_image(src, path)
        if reply.sticker is not None:
            await self._send_sticker(src, reply.sticker.digest)
        partners = self._partners.setdefault(src.channel, {})
        for s in pending.sources:
            partners[s.user_id] = self.now()

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
        """
        inc = src.incoming
        vision = self.c.config.vision
        read = vision.enabled and (addressed or self._active(src.channel))
        descriptions: list[str] = []
        digests: list[str] = []
        for ref in inc.images:
            label = "表情包" if ref.sticker else "图片"
            if not read:
                descriptions.append(f"[{label}]")
                continue
            desc, digest = await self._look(ref, src)
            descriptions.append(f"[{label}：{desc}]" if desc else f"[{label}（没看清）]")
            if digest:
                digests.append(digest)
        text = inc.fill_images(descriptions)

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
            descriptions.append(f"[{label}：{desc}]" if desc else f"[{label}]")
        body = quoted.fill_images(descriptions).strip()
        if len(body) > 120:
            body = body[:119] + "…"
        said = "你说的" if who == "你" else f"{who}的话"
        return f"（引用了{said}：{body or '……'}）"

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

    async def _send(self, src: Source, text: str, reply_to: str | None = None) -> None:
        message = text_segments(text, reply_to)
        try:
            if src.message_type == "group":
                data = await self.client.call(
                    "send_group_msg", {"group_id": int(src.group_id), "message": message}
                )
            else:
                data = await self.client.call(
                    "send_private_msg", {"user_id": int(src.user_id), "message": message}
                )
        except OneBotError as e:
            log.error("发送失败：%s", e)
            return
        self._last_spoke[src.channel] = self.now()
        if isinstance(data, dict) and data.get("message_id") is not None:
            self._sent_ids.append(str(data["message_id"]))

    async def _announce_groups(self) -> None:
        """第一次接入某个群时，主动说明她会怎样使用聊天记录（每个群只说一次）。"""
        for gid in self.cfg.groups:
            key = f"announced:group:{gid}"
            if await self._call(self.c.index.get_meta, key):
                continue
            try:
                data = await self.client.call(
                    "send_group_msg",
                    {
                        "group_id": int(gid),
                        "message": text_segments(NOTICE.format(name=self.c.name)),
                    },
                )
            except OneBotError as e:
                log.error("向群 %s 发送说明失败：%s", gid, e)
                continue
            if isinstance(data, dict) and data.get("message_id") is not None:
                self._sent_ids.append(str(data["message_id"]))
            await self._call(self.c.index.set_meta, key, self.now().isoformat())

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
            await self.client.call(action, params)
        except OneBotError as e:
            log.error("发送图片失败：%s", e)
            return
        self._last_spoke[src.channel] = self.now()

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
