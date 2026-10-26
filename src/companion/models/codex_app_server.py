"""GPT 订阅后端：通过本地 `codex app-server`（JSON-RPC over stdio）调用 ChatGPT 账号下的模型。

做法：
- 启动时用 `-c` 覆盖配置：用中性的对话代理指令替换内置的编程助手指令，关掉执行命令与联网搜索，
  不保存会话历史；每轮回复都开一个新线程、沙箱只读、从不请求审批。
- 一律关掉用户 Codex 配置里的 MCP 服务器、插件、电脑操控与浏览器工具：她在群聊里收到的话可能
  带有诱导，不能让模型借这些工具碰到本机。
- 角色设定、记忆、对话记录全部放进本轮输入，所以和兜底 API 用的是同一份提示词。
- `account/rateLimits/read` 读额度窗口，供网关判断要不要提前切到兜底 API。

协议依据：https://developers.openai.com/codex/app-server （JSON-RPC 2.0，线上省略 "jsonrpc" 字段）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import tomllib
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

IMAGE_INSTRUCTIONS = """你是一个画师。用你的生图工具，按要求画一张图（只画一张）。
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

    @classmethod
    def from_codex(
        cls,
        codex_command: str,
        workdir: Path,
        *,
        overrides: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> CodexAppServerBackend:
        """overrides：额外的 `-c` 配置（值是 TOML）；默认是聊天用的“无工具”配置。"""
        exe = shutil.which(codex_command)
        if exe is None:
            raise ModelError(f"找不到 {codex_command}：请先安装 Codex CLI 并登录 ChatGPT")
        workdir.mkdir(parents=True, exist_ok=True)
        # 指令通过 thread/start 的 baseInstructions 传入；这里关掉用不到的工具
        settings = {"personality": '"none"', **lockdown_overrides()}
        settings |= (
            overrides
            if overrides is not None
            else {"features.shell_tool": "false", "web_search": '"disabled"'}
        )
        command = [str(_native_codex(Path(exe))), "app-server"]
        for key, value in settings.items():
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
                env=self._env,
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
                "sandbox": "read-only",
                "baseInstructions": self.instructions,
                "ephemeral": True,  # 不把聊天存进 Codex 的历史
            }
            if self.model:
                thread_params["model"] = self.model
            thread = self._request("thread/start", thread_params)
            thread_id = (thread.get("thread") or {}).get("id")
            if not thread_id:
                raise ModelError(f"thread/start 返回异常：{str(thread)[:200]}")

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": render_prompt(messages, json_schema)}]
                + [
                    {"type": "localImage", "path": str(path.resolve())}
                    for m in messages
                    for path in m.images
                ],
            }
            if self.effort:
                turn_params["effort"] = self.effort
            if json_schema is not None:
                turn_params["outputSchema"] = json_schema
            self._request("turn/start", turn_params)
            text, _ = self._collect_turn(thread_id)
            return ChatResult(
                text=text,
                backend=self.name,
                model=self.model or "codex-default",
                seconds=time.perf_counter() - started,
            )

    def generate_image(self, prompt: str) -> tuple[Path, str]:
        """用 Codex 内置的生图工具画一张图，返回（图片路径，模型改写后的画面描述）。"""
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
                "input": [{"type": "text", "text": f"请画：{prompt}"}],
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
        self, thread_id: str, *, allow_empty: bool = False
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


def lockdown_overrides() -> dict[str, str]:
    """关掉用户 Codex 配置里的 MCP 服务器和各种能碰到本机的工具（只读配置里的名字）。"""
    out = {f"features.{name}": "false" for name in _LOCKED_FEATURES}
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        servers = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return out
    for name in servers.get("mcp_servers") or {}:
        if name.replace("_", "").replace("-", "").isalnum():
            out[f"mcp_servers.{name}.enabled"] = "false"
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
