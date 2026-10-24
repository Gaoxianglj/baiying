"""OneBot 11 正向 WebSocket 客户端（连 NapCat 的 WebSocket 服务）。

同一条连接上既收事件（带 post_type），也发动作、收动作结果（用 echo 对应）。断线自动重连。
协议：https://github.com/botuniverse/onebot-11
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from itertools import count
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    def __init__(self, url: str, token: str | None, on_event: EventHandler) -> None:
        self.url = url
        self.token = token
        self.on_event = on_event
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._echo = count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self.connected = asyncio.Event()

    async def run_forever(self, *, max_backoff: float = 60.0) -> None:
        backoff = 1.0
        while True:
            try:
                await self.run_once()
                backoff = 1.0
            except InvalidStatus as e:
                status = e.response.status_code
                hint = (
                    "token 不对，或者这个端口上跑的不是 NapCat（可能被别的程序占用）"
                    if status in (401, 403)
                    else "这个地址上的服务不接受 WebSocket 连接"
                )
                log.error(
                    "%s 拒绝了连接（HTTP %d）：%s；%.0f 秒后重试", self.url, status, hint, backoff
                )
            except (OSError, InvalidHandshake, ConnectionClosed, TimeoutError) as e:
                log.warning("与 NapCat 的连接断开：%s；%.0f 秒后重连", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def run_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with connect(self.url, additional_headers=headers, max_size=2**24) as ws:
            self._ws = ws
            self.connected.set()
            log.info("已连接 NapCat：%s", self.url)
            try:
                async for raw in ws:
                    self._dispatch(raw)
            finally:
                self._ws = None
                self.connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(OneBotError("连接已断开"))
                self._pending.clear()

    async def call(self, action: str, params: dict[str, Any], timeout: float = 30.0) -> Any:
        if self._ws is None:
            raise OneBotError("还没有连上 NapCat")
        echo = str(next(self._echo))
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") != "ok" and resp.get("retcode") not in (0, None):
            raise OneBotError(
                f"{action} 失败：{resp.get('message') or resp.get('wording') or resp}"
            )
        return resp.get("data")

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("忽略非 JSON 消息")
            return
        echo = msg.get("echo")
        if echo is not None and str(echo) in self._pending:
            fut = self._pending[str(echo)]
            if not fut.done():
                fut.set_result(msg)
            return
        if "post_type" in msg:
            # 事件处理可能很慢（要调用模型），放进后台任务，不阻塞收消息
            task = asyncio.create_task(self._handle(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle(self, event: dict[str, Any]) -> None:
        try:
            await self.on_event(event)
        except Exception:
            log.exception("处理事件出错：%s", str(event)[:200])
