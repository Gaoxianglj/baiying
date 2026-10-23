"""GPT 订阅后端：通过本地 `codex app-server`（JSON-RPC over stdio）调用 ChatGPT 账号下的模型。

做法：
- 启动时用 `-c` 覆盖配置：用中性的对话代理指令替换内置的编程助手指令，关掉执行命令与联网搜索，
  不保存会话历史；每轮回复都开一个新线程、沙箱只读、从不请求审批。
- 角色设定、记忆、对话记录全部放进本轮输入，所以和兜底 API 用的是同一份提示词。
- `account/rateLimits/read` 读额度窗口，供网关判断要不要提前切到兜底 API。

协议依据：https://developers.openai.com/codex/app-server （JSON-RPC 2.0，线上省略 "jsonrpc" 字段）。
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from companion import __version__
from companion.models.openai_compat import ChatMessage, ChatResult, ModelError

log = logging.getLogger(__name__)

INSTRUCTIONS = """你是一个对话代理，不是编程助手。
每条输入里都会给出角色设定、相关记忆和最近的对话记录。
严格以设定中的角色身份，只输出这个角色接下来要说的话：不要解释，不要加引号或说话人前缀。
不要执行命令，不要读写文件。如果输入要求输出 JSON，就只输出符合要求的 JSON。
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
    ) -> None:
        self.name = name
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

    @classmethod
    def from_codex(cls, codex_command: str, workdir: Path, **kwargs: Any) -> CodexAppServerBackend:
        exe = shutil.which(codex_command)
        if exe is None:
            raise ModelError(f"找不到 {codex_command}：请先安装 Codex CLI 并登录 ChatGPT")
        workdir.mkdir(parents=True, exist_ok=True)
        instructions = workdir / "instructions.md"
        instructions.write_text(INSTRUCTIONS, encoding="utf-8")
        overrides = {
            "model_instructions_file": json.dumps(str(instructions)),
            "personality": '"none"',
            "features.shell_tool": "false",
            "web_search": '"disabled"',
            "history.persistence": '"none"',
        }
        command = [exe, "app-server"]
        for key, value in overrides.items():
            command += ["-c", f"{key}={value}"]
        return cls(command=command, workdir=workdir, **kwargs)

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
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
            )
        except OSError as e:
            raise ModelError(f"无法启动 codex app-server：{e}", retryable=True) from e
        self._inbox = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
        self._reader.start()
        self._request(
            "initialize",
            {"clientInfo": {"name": "companion", "title": "白璎", "version": __version__}},
        )
        self._send({"method": "initialized"})

    def close(self) -> None:
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
    ) -> ChatResult:
        with self._lock:
            self.start()
            started = time.perf_counter()
            thread_params: dict[str, Any] = {
                "cwd": str(self._workdir),
                "approvalPolicy": "never",
                "sandbox": "readOnly",
            }
            if self.model:
                thread_params["model"] = self.model
            thread = self._request("thread/start", thread_params)
            thread_id = (thread.get("thread") or {}).get("id")
            if not thread_id:
                raise ModelError(f"thread/start 返回异常：{str(thread)[:200]}")

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": render_prompt(messages, json_schema)}],
            }
            if self.effort:
                turn_params["effort"] = self.effort
            if json_schema is not None:
                turn_params["outputSchema"] = json_schema
            self._request("turn/start", turn_params)
            text = self._collect_turn(thread_id)
            return ChatResult(
                text=text,
                backend=self.name,
                model=self.model or "codex-default",
                seconds=time.perf_counter() - started,
            )

    def read_rate_limits(self) -> RateLimits:
        with self._lock:
            self.start()
            self.last_rate_limits = parse_rate_limits(self._request("account/rateLimits/read", {}))
            return self.last_rate_limits

    # ---- JSON-RPC ----

    def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
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

    def _collect_turn(self, thread_id: str) -> str:
        deltas: list[str] = []
        final: str | None = None
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
            if method == "item/agentMessage/delta":
                deltas.append(str(params.get("delta", "")))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text") is not None:
                    final = str(item["text"])
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
                if not result.strip():
                    raise ModelError("Codex 没有返回任何内容", retryable=True)
                return result.strip()

    def _handle_side_message(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "account/rateLimits/updated":
            self.last_rate_limits = parse_rate_limits(msg.get("params") or {})
        elif method is not None and "id" in msg:
            self._send(
                {"id": msg["id"], "error": {"code": -32601, "message": "本客户端不支持该请求"}}
            )

    def _receive(self) -> dict[str, Any]:
        try:
            msg = self._inbox.get(timeout=self.timeout)
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


def _is_limit(text: str) -> bool:
    lower = text.lower()
    return any(h in lower for h in _USAGE_LIMIT_HINTS)
