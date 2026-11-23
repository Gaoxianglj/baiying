"""GPT 订阅后端：通过本地 `codex app-server`（JSON-RPC over stdio）调用 ChatGPT 账号下的模型。

做法：
- 启动时用 `-c` 覆盖配置：用中性的对话代理指令替换内置的编程助手指令，关掉执行命令、看本机图片
  与联网搜索；普通聊天按频道保存线程，辅助调用使用临时线程，沙箱只读、从不请求审批。
- 一律关掉用户 Codex 配置里的 MCP 服务器、插件、电脑操控与浏览器工具：她在群聊里收到的话可能
  带有诱导，不能让模型借这些工具碰到本机。她要碰文件只能走技能，技能里才有权限检查。
- 角色设定与检索记忆每轮刷新，事件按编号增量投递；兜底 API 仍用原来的完整提示词。
- `account/rateLimits/read` 读额度窗口，供网关判断要不要提前切到兜底 API。

协议依据：https://developers.openai.com/codex/app-server （JSON-RPC 2.0，线上省略 "jsonrpc" 字段）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from companion import __version__
from companion.models.openai_compat import ChatMessage, ChatResult, ModelError
from companion.models.runtime import Observer, RunControl, ToolSet
from companion.models.sessions import ChatSessions

log = logging.getLogger(__name__)

INSTRUCTIONS = """你是一个对话代理，不是编程助手。
每条输入里都会给出角色设定、相关记忆和最近的对话记录。
严格以设定中的角色身份，只输出这个角色接下来要说的话：不要解释，不要加引号或说话人前缀。
不要执行命令，不要读写文件。如果输入要求输出 JSON，就只输出符合要求的 JSON。
"""

IMAGE_INSTRUCTIONS = """你是一个画师。用你的生图工具，按要求画一张图（只画一张）。
如果附了图片，那是参考图：按要求参考它的角色、风格或构图。
不要执行命令，不要读写文件。画完只回复“好了”。如果不能画，简短说明原因。
"""

_USAGE_LIMIT_HINTS = ("usage limit", "rate limit", "quota", "limit reached", "额度")


@dataclass(frozen=True)
class RateWindow:
    used_percent: float
    window_minutes: int | None
    resets_at: datetime | None


@dataclass(frozen=True)
class RateLimits:
    windows: tuple[RateWindow, ...]

    def max_used_percent(self) -> float:
        return max((w.used_percent for w in self.windows), default=0.0)

    def reset_time(self, threshold: float) -> datetime | None:
        """达到阈值的窗口里最晚的重置时间。"""
        times = [w.resets_at for w in self.windows if w.used_percent >= threshold and w.resets_at]
        return max(times, default=None)


def parse_rate_limits(payload: dict[str, Any]) -> RateLimits:
    """兼容 `rateLimits` 单桶视图与 `rateLimitsByLimitId` 多桶视图。"""
    buckets: list[dict[str, Any]] = []
    if isinstance(payload.get("rateLimits"), dict):
        buckets.append(payload["rateLimits"])
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        buckets.extend(b for b in by_id.values() if isinstance(b, dict))
    windows = []
    for bucket in buckets:
        for name in ("primary", "secondary"):
            w = bucket.get(name)
            if not isinstance(w, dict) or w.get("usedPercent") is None:
                continue
            resets = w.get("resetsAt")
            windows.append(
                RateWindow(
                    used_percent=float(w["usedPercent"]),
                    window_minutes=w.get("windowDurationMins"),
                    resets_at=datetime.fromtimestamp(resets, UTC) if resets else None,
                )
            )
    return RateLimits(tuple(windows))


def render_prompt(messages: list[ChatMessage], json_schema: dict[str, Any] | None) -> str:
    """把聊天消息压成一段输入文本。"""
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"<设定>\n{m.content}\n</设定>")
        elif m.role == "assistant":
            parts.append(f"<你之前说>\n{m.content}\n</你之前说>")
        elif m.role == "skill":
            parts.append(f"<技能结果>\n{m.content}\n</技能结果>")
        else:
            parts.append(f"<对方说>\n{m.content}\n</对方说>")
    if json_schema is not None:
        parts.append(
            "只输出符合以下 JSON Schema 的 JSON：\n" + json.dumps(json_schema, ensure_ascii=False)
        )
    else:
        parts.append("请只输出你接下来要说的话。")
    return "\n\n".join(parts)


class CodexAppServerBackend:
    def __init__(
        self,
        *,
        command: list[str],
        workdir: Path,
        model: str | None = None,
        effort: str | None = "low",
        timeout: float = 120.0,
        name: str = "subscription",
        env: dict[str, str] | None = None,
        instructions: str = INSTRUCTIONS,
        sessions: ChatSessions | None = None,
        persistent_chats: bool = True,
        recover_pending: bool = False,
    ) -> None:
        self.name = name
        self.instructions = instructions  # 每个线程的基础指令（替换内置的编程助手指令）
        self._env = env
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self._command = command
        self._workdir = workdir
        self._proc: subprocess.Popen[str] | None = None
        self._inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()  # 同一时刻只跑一轮
        self.last_rate_limits: RateLimits | None = None
        self.sessions = sessions
        self.persistent_chats = persistent_chats
        self.recover_pending = recover_pending
        self._loaded: set[str] = set()
        self._tools: ToolSet | None = None
        self._observer: Observer | None = None
        self._active_thread: str | None = None
        self._active_turn: str | None = None
        self.native_tools_supported = True
        self._experimental_api = True
        self._control: RunControl | None = None
        self._control_requests: dict[int, str] = {}
        self._control_sent: set[str] = set()
        self._control_counter = -1000
        self._interrupt_sent = False
        self._send_lock = threading.Lock()

    @classmethod
    def from_codex(
        cls,
        codex_command: str,
        workdir: Path,
        *,
        overrides: dict[str, str] | None = None,
        codex_home: Path | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> CodexAppServerBackend:
        """overrides：额外的 `-c` 配置（值是 TOML）；默认是聊天用的“无工具”配置。"""
        exe = shutil.which(codex_command)
        if exe is None:
            raise ModelError(f"找不到 {codex_command}：请先安装 Codex CLI 并登录 ChatGPT")
        workdir.mkdir(parents=True, exist_ok=True)
        if codex_home is not None:
            if not codex_home.is_dir():
                raise ModelError(f"白璎的 Codex 目录不存在：{codex_home}")
            env = dict(os.environ if env is None else env)
            env["CODEX_HOME"] = str(codex_home)
            env["CODEX_SQLITE_HOME"] = str(codex_home)
        # 指令通过 thread/start 的 baseInstructions 传入；这里关掉用不到的工具
        settings = {"personality": '"none"', **lockdown_overrides(codex_home)}
        settings |= overrides if overrides is not None else talk_only_overrides()
        command = [str(_native_codex(Path(exe))), "app-server"]
        for key, value in settings.items():
            command += ["-c", f"{key}={value}"]
        return cls(command=command, workdir=workdir, env=env, **kwargs)

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._loaded.clear()
        try:
            self._proc = subprocess.Popen(
                self._command,
                cwd=self._workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=self._env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as e:
            raise ModelError(f"无法启动 codex app-server：{e}", retryable=True) from e
        self._inbox = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
        self._reader.start()
        init = {"clientInfo": {"name": "companion", "title": "白璎", "version": __version__}}
        if self._experimental_api:
            init["capabilities"] = {"experimentalApi": True}
        try:
            self._request("initialize", init)
        except ModelError as e:
            if not self._experimental_api or not any(w in str(e).lower() for w in (
                "experimental", "capabilities", "unknown field", "unsupported",
            )):
                raise
            self._experimental_api = False
            self.native_tools_supported = False
            self.close()
            self.start()
            return
        self._send({"method": "initialized"})

    def close(self) -> None:
        self._loaded.clear()
        self._active_thread, self._active_turn, self._control = None, None, None
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        if self._reader is not None:
            self._reader.join(timeout=5)  # 读到 EOF 后由读线程关闭 stdout

    # ---- 对外接口 ----

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        session_key: str | None = None,
        session_messages: list[ChatMessage] | None = None,
        session_bootstrap: list[ChatMessage] | None = None,
        tools: ToolSet | None = None,
        observer: Observer | None = None,
        control: RunControl | None = None,
    ) -> ChatResult:
        with self._lock:
            self.start()
            if not self.native_tools_supported:
                tools = None
            instructions = self.instructions
            if tools and tools.specs:
                instructions = instructions.replace(
                    "不要执行命令，不要读写文件。",
                    "不要执行命令，也不要自行访问本机。文件操作仅能调用本应用提供的 baiying_ "
                    "原生工具，由程序检查权限。",
                )
            started = time.perf_counter()
            # 辅助调用没有 key，始终隔离；只有显式的日常聊天才持久化。
            key = (session_key if self.sessions is not None and self.persistent_chats
                   and json_schema is None else None)
            policy = hashlib.sha256(
                (f"{self.model}\n{instructions}\n{self._workdir}\n{self._command}\n"
                 + json.dumps(tools.specs if tools else [], sort_keys=True)).encode()
            ).hexdigest()
            row = self.sessions.get(key) if key and self.sessions else None
            if row and (row[1] != policy or (row[2] and not self.recover_pending)):
                # 上次生成结果未知时，不盲目重放那次工具链；用原始对话重新引导。
                self.sessions.retire("policy-change" if row[1] != policy else "uncertain-turn", key)
                row = None
            thread_params: dict[str, Any] = {
                "cwd": str(self._workdir),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "baseInstructions": instructions,
                "ephemeral": key is None,
            }
            if self.model:
                thread_params["model"] = self.model
            if tools and tools.specs:
                thread_params["dynamicTools"] = tools.specs
            thread_id = row[0] if row else None
            if thread_id and thread_id not in self._loaded:
                try:
                    self._request("thread/resume", {
                        "threadId": thread_id,
                        **{k: v for k, v in thread_params.items()
                           if k not in {"ephemeral", "dynamicTools"}},
                    })
                except ModelError as e:
                    # 只在已知历史不存在时重建；认证/网络错误交给原网关兜底。
                    if not any(s in str(e).lower() for s in (
                        "not found", "no rollout", "does not exist", "unknown thread",
                    )):
                        raise
                    assert self.sessions is not None and key is not None
                    self.sessions.retire("missing-thread", key)
                    thread_id = None
                    row = None
            if thread_id is None:
                try:
                    thread = self._request("thread/start", thread_params)
                except ModelError as e:
                    if not tools or not any(w in str(e).lower() for w in (
                        "dynamictools", "dynamic_tools", "experimental", "unknown field",
                    )):
                        raise
                    self.native_tools_supported = False
                    tools = None
                    thread_params.pop("dynamicTools", None)
                    log.warning("当前 Codex 不支持动态工具，改用原技能标记执行链")
                    thread = self._request("thread/start", thread_params)
                thread_id = (thread.get("thread") or {}).get("id")
                if key and thread_id:
                    assert self.sessions is not None
                    self.sessions.put(key, thread_id, policy)
            if not thread_id:
                raise ModelError(f"thread/start 返回异常：{str(thread)[:200]}")
            if key:
                self._loaded.add(thread_id)
            selected = messages
            if key:
                assert self.sessions is not None
                source = session_messages if row else session_bootstrap
                selected = [m for m in (source if source is not None else messages)
                            if m.event_id is None or self.sessions.unseen(key, m.event_id)]

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": render_prompt(selected, json_schema)}]
                + [
                    {"type": "localImage", "path": str(path.resolve())}
                    for m in selected
                    for path in m.images
                ],
            }
            if self.effort:
                turn_params["effort"] = self.effort
            if tools and tools.specs:
                turn_params["input"][0]["text"] += (
                    "\n\n当前已提供对应的原生工具。需要技能时优先调用原生工具，不要把调用写成"
                    "文本标记；arg 是参数或路径，写文件正文放 content，其他 content 填空。"
                    "屏幕、剪贴板仍按原标记使用。工具返回前不要声称操作完成。"
                )
            if json_schema is not None:
                turn_params["outputSchema"] = json_schema
            if key:
                assert self.sessions is not None
                self.sessions.pending(key)
            try:
                turn = self._request("turn/start", turn_params)
                self._tools, self._observer = tools, observer
                self._active_thread = thread_id
                self._active_turn = (turn.get("turn") or {}).get("id")
                self._control = control
                self._control_sent.clear()
                self._control_requests.clear()
                self._interrupt_sent = False
                self._observe("turn/started", {"threadId": thread_id,
                                               "turn": turn.get("turn") or {}})
                text, _ = self._collect_turn(thread_id, turn_id=(turn.get("turn") or {}).get("id"))
            except ModelError:
                # 避免仍在生成的旧 turn 与下一条消息竞争；索引 pending 留作恢复依据。
                if key:
                    self.close()
                raise
            finally:
                self._tools, self._observer = None, None
                self._active_thread, self._active_turn = None, None
                self._control = None
            if key:
                assert self.sessions is not None
                self.sessions.complete(key, thread_id, [m.event_id for m in selected if m.event_id])
            return ChatResult(
                text=text,
                backend=self.name,
                model=self.model or "codex-default",
                seconds=time.perf_counter() - started,
                raw={"session_key": key, "thread_id": thread_id} if key else {},
            )

    def note_response(self, result: ChatResult, event_id: str) -> None:
        """模型回复经 QQ 标记清理后落事件日志，不在下一轮重复输入给原线程。"""
        key, tid = result.raw.get("session_key"), result.raw.get("thread_id")
        if self.sessions and key and tid:
            self.sessions.complete(key, tid, [event_id])

    def rotate_sessions(self, reason: str) -> int:
        with self._lock:
            if self.sessions is None:
                return 0
            ids = self.sessions.retire(reason)
            if self._proc is not None and self._proc.poll() is None:
                for tid in ids:
                    if tid in self._loaded:
                        try:
                            self._request("thread/unsubscribe", {"threadId": tid})
                        except ModelError as e:
                            log.warning("旧聊天线程已失效，取消订阅暂时失败：%s", e)
            self._loaded.difference_update(ids)
            # 只切换本项目索引，不删除用户 Codex 原始会话；失败也可从事件日志重建。
            return len(ids)

    def generate_image(self, prompt: str, references: Sequence[Path] = ()) -> tuple[Path, str]:
        """用 Codex 内置的生图工具画一张图，返回（图片路径，模型改写后的画面描述）。

        references：参考图（调用方查过权限的本地文件），实测能保持角色和风格（2026-11-08）。"""
        with self._lock:
            self.start()
            thread = self._request(
                "thread/start",
                {
                    "cwd": str(self._workdir),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "baseInstructions": IMAGE_INSTRUCTIONS,
                    "ephemeral": True,
                },
            )
            thread_id = (thread.get("thread") or {}).get("id")
            if not thread_id:
                raise ModelError(f"thread/start 返回异常：{str(thread)[:200]}")
            turn: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": f"请画：{prompt}"}]
                + [{"type": "localImage", "path": str(p.resolve())} for p in references],
            }
            if self.effort:
                turn["effort"] = self.effort
            self._request("turn/start", turn)
            reply, images = self._collect_turn(thread_id, allow_empty=True)
        for item in images:
            path = item.get("savedPath")
            if item.get("status") == "completed" and path and Path(path).exists():
                return Path(path), str(item.get("revisedPrompt") or prompt)
        failure = next((i.get("failure") for i in images if i.get("failure")), None)
        raise ModelError(f"没有画出来：{failure or reply or '没有生成图片'}"[:300])

    def read_rate_limits(self) -> RateLimits:
        with self._lock:
            self.start()
            self.last_rate_limits = parse_rate_limits(self._request("account/rateLimits/read", {}))
            return self.last_rate_limits

    # ---- JSON-RPC ----

    def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            with self._send_lock:
                self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
        except OSError as e:
            raise ModelError(f"codex app-server 已退出：{e}", retryable=True) from e

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            msg = self._receive()
            if msg.get("id") == request_id and "method" not in msg:
                if "error" in msg:
                    err = msg["error"]
                    text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise ModelError(f"{method} 失败：{text}", retryable=_is_limit(text))
                return msg.get("result") or {}
            self._handle_side_message(msg)

    def _collect_turn(
        self, thread_id: str, *, allow_empty: bool = False, turn_id: str | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """读完一轮的消息，返回（最终回复，生成的图片 item）。"""
        deltas: list[str] = []
        final: str | None = None
        images: list[dict[str, Any]] = []
        while True:
            msg = self._receive()
            method = msg.get("method")
            params = msg.get("params") or {}
            if method is None:
                continue
            if "id" in msg:  # 服务端发来的请求（如审批），一律拒绝
                self._handle_side_message(msg)
                continue
            if params.get("threadId") not in (None, thread_id):
                continue
            incoming_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
            if turn_id and incoming_turn and incoming_turn != turn_id:
                continue
            self._observe(str(method), params)
            if (method == "item/completed"
                    and (params.get("item") or {}).get("type") == "contextCompaction"):
                log.info("Codex 聊天线程完成上下文压缩：%s", thread_id)
            if method == "item/agentMessage/delta":
                deltas.append(str(params.get("delta", "")))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text") is not None:
                    final = str(item["text"])
                elif item.get("type") == "imageGeneration":
                    item.pop("result", None)  # base64 原图很大；用 savedPath
                    images.append(item)
            elif method == "account/rateLimits/updated":
                self.last_rate_limits = parse_rate_limits(params)
            elif method == "error":
                text = str(params.get("error") or params)
                raise ModelError(f"Codex 出错：{text[:300]}", retryable=_is_limit(text))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                if status not in (None, "completed"):
                    err = turn.get("error") or {}
                    text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise ModelError(
                        f"Codex 本轮{status}：{text[:300]}",
                        retryable=_is_limit(text) or status == "failed",
                    )
                result = final if final is not None else "".join(deltas)
                if not result.strip() and not allow_empty:
                    raise ModelError("Codex 没有返回任何内容", retryable=True)
                return result.strip(), images

    def _handle_side_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "item/tool/call" and "id" in msg:
            p = msg.get("params") or {}
            valid = (self._tools is not None and p.get("threadId") == self._active_thread
                     and p.get("turnId") == self._active_turn and not p.get("namespace")
                     and isinstance(p.get("callId"), str) and bool(p["callId"]))
            if valid:
                try:
                    result = self._tools.call(p.get("tool", ""), p.get("arguments"), p["callId"])
                except Exception:
                    log.exception("原生技能回调失败")
                    result = {"success": False, "contentItems": [
                        {"type": "inputText", "text": "工具调用失败，请核对实际状态。"}]}
            else:
                result = {"success": False, "contentItems": [
                    {"type": "inputText", "text": "工具请求不属于当前聊天或当前轮，拒绝执行。"}]}
            self._send({"id": msg["id"], "result": result})
            return
        if method == "account/rateLimits/updated":
            self.last_rate_limits = parse_rate_limits(msg.get("params") or {})
        elif method is not None and "id" in msg:
            self._send(
                {"id": msg["id"], "error": {"code": -32601, "message": "本客户端不支持该请求"}}
            )

    def _observe(self, method: str, params: dict[str, Any]) -> None:
        if self._observer is not None:
            self._observer(method, params)

    def interrupt_current(self) -> None:
        if self._active_thread and self._active_turn and not self._interrupt_sent:
            self._interrupt_sent = True
            self._send({"id": -999, "method": "turn/interrupt", "params": {
                "threadId": self._active_thread, "turnId": self._active_turn,
            }})

    def _pump_control(self) -> None:
        control = self._control
        if control is None or not self._active_turn:
            return
        if control.stopped.is_set() or (control.deadline and time.monotonic() > control.deadline):
            self.interrupt_current()
            return
        for item in control.pending():
            if item["id"] in self._control_sent:
                continue
            self._control_counter -= 1
            rid = self._control_counter
            self._control_sent.add(item["id"])
            self._control_requests[rid] = item["id"]
            self._send({"id": rid, "method": "turn/steer", "params": {
                "threadId": self._active_thread, "expectedTurnId": self._active_turn,
                "input": [{"type": "text", "text": "原任务的追加要求：" + item["text"]}],
            }})

    def compact_session(self, session_key: str, observer: Observer | None = None) -> bool:
        with self._lock:
            row = self.sessions.get(session_key) if self.sessions else None
            if row is None:
                return False
            self.start()
            self._request("thread/resume", {"threadId": row[0], "approvalPolicy": "never",
                                            "sandbox": "read-only", "cwd": str(self._workdir)})
            self._observer = observer
            try:
                self._request("thread/compact/start", {"threadId": row[0]})
                self._collect_turn(row[0], allow_empty=True)
            finally:
                self._observer = None
            return True

    def _receive(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                self._pump_control()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                try:
                    msg = self._inbox.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    continue
                rid = msg.get("id") if isinstance(msg, dict) else None
                if rid in self._control_requests and isinstance(msg, dict) and "method" not in msg:
                    item_id = self._control_requests.pop(rid)
                    if self._control:
                        self._control.receipt(item_id, "error" not in msg,
                                              "运行中的原任务已接收" if "error" not in msg
                                              else "本轮未接收，将在下一步或恢复时重投")
                    continue
                break
        except queue.Empty as e:
            self.close()
            raise ModelError(
                f"codex app-server {self.timeout:.0f} 秒没有响应", retryable=True
            ) from e
        if msg is None:
            self._proc = None
            raise ModelError("codex app-server 意外退出", retryable=True)
        return msg

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        inbox = self._inbox
        with proc.stdout:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    inbox.put(json.loads(line))
                except json.JSONDecodeError:
                    log.debug("忽略非 JSON 输出：%s", line[:200])
        inbox.put(None)


# 关掉的功能：电脑操控、浏览器、插件（含其 MCP 服务器）、应用连接器、多代理
_LOCKED_FEATURES = (
    "computer_use",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "plugins",
    "apps",
    "multi_agent",
)


def _mcp_server_names(home: Path | None = None) -> list[str]:
    home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return list(config.get("mcp_servers") or {})


def _can_switch_off(name: str) -> bool:
    return name.replace("_", "").replace("-", "").isalnum()  # 别的字符写不进 -c 的点号路径


def lockdown_overrides(codex_home: Path | None = None) -> dict[str, str]:
    """关掉 Codex 配置里的 MCP 服务器和能访问本机的工具（只读配置里的名字）。"""
    out = {f"features.{name}": "false" for name in _LOCKED_FEATURES}
    # 记忆由白璎自己的事件、档案与检索管理，不混入个人 Codex 的全局记忆。
    out |= {"memories.generate_memories": "false", "memories.use_memories": "false",
            "memories.dedicated_tools": "false"}
    for name in _mcp_server_names(codex_home):
        if _can_switch_off(name):
            out[f"mcp_servers.{name}.enabled"] = "false"
    return out


def stuck_mcp_servers(codex_home: Path | None = None) -> list[str]:
    """lockdown_overrides 关不掉的 MCP 服务器：名字特殊，会留在 Codex 进程里。"""
    return [name for name in _mcp_server_names(codex_home) if not _can_switch_off(name)]


# 只负责“说话”的进程（聊天、画画、自主活动）不需要的工具。
# view_image 的实测（2026-11-08）：不关时，模型能直接看这台电脑上任意路径的图片，
# 绕过技能的所有权限检查；沙箱也拦不住它，因为它不是在沙箱里跑的命令。
_LOCAL_TOOLS = (
    "shell_tool",
    "unified_exec",
    "view_image",
    "goals",
    "hooks",
    "sleep_tool",
    "skill_search",
    "tool_suggest",
    "workspace_dependencies",
)


def talk_only_overrides(*, image: bool = False, web: bool = False) -> dict[str, str]:
    """不碰本机的进程配置：image = 能生图（参考图由程序查过权限后附上）；web = 能用自带的搜索。"""
    out = {f"features.{name}": "false" for name in _LOCAL_TOOLS}
    out["features.image_generation"] = "true" if image else "false"
    out["web_search"] = '"live"' if web else '"disabled"'
    return out


def _native_codex(shim: Path) -> Path:
    """npm 在 Windows 上装的是 codex.cmd / codex.ps1 包装脚本，经 cmd.exe 转发时引号参数会被改写；
    尽量找到包里自带的 codex.exe 直接启动。找不到就用原路径。"""
    if shim.suffix.lower() not in (".cmd", ".ps1", ".bat"):
        return shim
    package = shim.parent / "node_modules" / "@openai" / "codex"
    for candidate in sorted(package.glob("**/vendor/*/bin/codex.exe")):
        return candidate
    return shim


def _is_limit(text: str) -> bool:
    lower = text.lower()
    return any(h in lower for h in _USAGE_LIMIT_HINTS)
