"""Agent：让独立的 Codex 进程替主人干活（整理文件、写脚本、查资料……）。

- 【任务：要做的事】：只限主人私聊或本地命令行。她先回一句，任务在后台跑，做完她用自己的话汇报。
- 每个任务启动一个新的 `codex app-server`，用专属权限配置：
  工作文件夹（默认 data/workspace）可读写；其余位置只读；黑名单路径连读都不行。
  实测 Windows 沙箱会真正拦下黑名单读取和工作文件夹外的写入（2026-10-24）。
- 越权操作（如写工作文件夹外）Codex 会请求审批，转给主人：回复 /批准 或 /拒绝，超时算拒绝。
- 联网：沙箱里的命令在 Windows 上做不了 HTTPS（受限令牌拿不到 TLS 凭据），所以用 Codex 自带的联网搜索。
- 订阅额度到阈值时，改用 DeepSeek 驱动 Codex（key 通过环境变量传给子进程，不出现在命令行里），
  token 用量按兜底 API 的价格记账。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion.config import AgentSkillConfig, FallbackConfig, SubscriptionConfig
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.openai_compat import ModelError
from companion.models.usage import UsageLedger, estimate_cost
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

log = logging.getLogger(__name__)

TASK_PREAMBLE = """你是“白璎”的助手，替她的主人在这台 Windows 电脑上完成一个任务。
- 工作文件夹：{workspace}。新建或修改的文件都放在这里；其他位置只读，部分位置禁止访问，不要尝试绕过。
- 需要查资料就用联网搜索；命令行里的网络下载在沙箱里用不了。
- 不要删除工作文件夹外的任何东西，不要碰密码、密钥、浏览器数据。
- 做完后用中文简要汇报：做了什么、结果如何、产出的文件路径。做不到就说明卡在哪里。

任务：{task}"""


def default_deny(root: Path) -> list[Path]:
    """默认黑名单：密钥、记忆、Codex 登录信息、浏览器与密码管理器数据、NapCat（QQ 登录）。"""
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    paths = [
        root / ".env",
        root / "config.local.toml",
        root / "data" / "memory",
        root / "data" / "run",
        root / "data" / "usage.jsonl",
        Path(os.environ.get("CODEX_HOME") or home / ".codex"),
        home / ".ssh",
        home / ".aws",
        home / ".azure",
        home / ".kube",
        home / ".docker",
        home / ".gnupg",
        home / ".git-credentials",
        home / ".config" / "gh",
        roaming / "GitHub CLI",
        local / "Microsoft" / "Credentials",
        roaming / "Microsoft" / "Credentials",
        roaming / "Microsoft" / "Protect",
        local / "Google" / "Chrome" / "User Data",
        local / "Microsoft" / "Edge" / "User Data",
        local / "BraveSoftware",
        roaming / "Mozilla" / "Firefox",
        roaming / "Opera Software",
        local / "1Password",
        roaming / "1Password",
        roaming / "Bitwarden",
        roaming / "KeePass",
        roaming / "KeePassXC",
        local / "LastPass",
    ]
    if os.environ.get("NAPCAT_DIR"):
        paths.append(Path(os.environ["NAPCAT_DIR"]))
    return paths


def permission_overrides(workspace: Path, deny: list[Path], *, web_search: bool) -> dict[str, str]:
    """专属权限配置（`-c` 覆盖项）：以只读为底，工作文件夹可写，黑名单禁止读取，审批交给我们。"""
    entries = {_toml_path(workspace): "write"}
    for path in deny:
        if path.exists():  # 不存在的路径 Codex 会报错；之后出现的会在下一个任务启动时加上
            entries[_toml_path(path)] = "deny"
    table = "{" + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in entries.items()) + "}"
    return {
        "default_permissions": '"companion_agent"',
        "permissions.companion_agent.extends": '":read-only"',
        "permissions.companion_agent.filesystem": table,
        "permissions.companion_agent.network.enabled": "true",
        "approval_policy": '"on-request"',
        "approvals_reviewer": '"user"',  # 不让 Codex 自己审，转给主人
        "web_search": '"live"' if web_search else '"disabled"',
    }


def _toml_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


@dataclass
class AgentOutcome:
    text: str
    ok: bool
    commands: int = 0
    files: int = 0
    seconds: float = 0.0
    backend: str = "subscription"


class AgentRun:
    """跑一个任务：读 app-server 的消息，处理审批、进度、超时和取消。"""

    def __init__(
        self,
        backend: CodexAppServerBackend,
        *,
        workspace: Path,
        effort: str | None,
        ask: Callable[[str], bool] | None,
        progress: Callable[[str], None] | None,
        timeout: float,
        progress_every: float,
    ) -> None:
        self.backend = backend
        self.workspace = workspace
        self.effort = effort
        self.ask = ask
        self.progress = progress
        self.timeout = timeout
        self.progress_every = progress_every
        self.cancelled = threading.Event()
        self.tokens = (0, 0)  # 输入、输出 token

    def run(self, task: str) -> AgentOutcome:
        b = self.backend
        started = time.monotonic()
        b.start()
        thread = b._request(
            "thread/start",
            {"cwd": str(self.workspace), "approvalPolicy": "on-request", "ephemeral": True},
        )
        thread_id = (thread.get("thread") or {}).get("id")
        if not thread_id:
            raise ModelError(f"thread/start 返回异常：{str(thread)[:200]}")
        text = TASK_PREAMBLE.format(workspace=self.workspace, task=task)
        params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        if self.effort:
            params["effort"] = self.effort
        turn = b._request("turn/start", params)
        turn_id = (turn.get("turn") or {}).get("id")

        commands = files = 0
        final = ""
        last_note = ""
        last_progress = time.monotonic()
        interrupted = False
        while True:
            if not interrupted and (
                self.cancelled.is_set() or time.monotonic() - started > self.timeout
            ):
                interrupted = True
                b._send({"id": -1, "method": "turn/interrupt",
                         "params": {"threadId": thread_id, "turnId": turn_id}})  # fmt: skip
            msg = b._receive()
            method = msg.get("method")
            params = msg.get("params") or {}
            if method is None:
                continue
            if "id" in msg:
                self._answer(msg)
                continue
            if method == "item/completed":
                item = params.get("item") or {}
                kind = item.get("type")
                if kind == "commandExecution":
                    commands += 1
                elif kind == "fileChange":
                    files += len(item.get("changes") or [])
                elif kind == "agentMessage":
                    final = str(item.get("text") or "")
                    if item.get("phase") != "final_answer":
                        last_note = final
            elif method == "thread/tokenUsage/updated":
                total = (params.get("tokenUsage") or {}).get("total") or {}
                self.tokens = (int(total.get("inputTokens", 0)), int(total.get("outputTokens", 0)))
            elif method == "account/rateLimits/updated":
                b._handle_side_message(msg)
            elif method == "turn/completed":
                status = (params.get("turn") or {}).get("status")
                seconds = time.monotonic() - started
                if status == "completed":
                    return AgentOutcome(final or "（没有汇报）", True, commands, files, seconds)
                why = "被取消了" if self.cancelled.is_set() else (
                    "超时了" if interrupted else f"中断了（{status}）"
                )  # fmt: skip
                note = f"任务{why}。已执行 {commands} 条命令、改了 {files} 个文件。"
                return AgentOutcome(note + (f"最后的进展：{final}" if final else ""), False,
                                    commands, files, seconds)  # fmt: skip
            if (
                self.progress
                and time.monotonic() - last_progress >= self.progress_every
                and (commands or files or last_note)
            ):
                last_progress = time.monotonic()
                minutes = (last_progress - started) / 60
                note = f"（任务进行中，{minutes:.0f} 分钟：已执行 {commands} 条命令、改了 {files} 个文件"
                self.progress(note + (f"；{last_note[:80]}）" if last_note else "）"))

    def _answer(self, msg: dict[str, Any]) -> None:
        """Codex 发来的请求：审批转给主人；其他一律拒绝。"""
        method = str(msg.get("method"))
        params = msg.get("params") or {}
        b = self.backend
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            what = params.get("command") or "修改文件"
            question = f"助手想做一件需要你批准的事：\n{str(what)[:300]}"
            if params.get("reason"):
                question += f"\n原因：{str(params['reason'])[:200]}"
            ok = self._ask(question)
            b._send({"id": msg["id"], "result": {"decision": "accept" if ok else "decline"}})
        elif method == "item/permissions/requestApproval":
            asked = params.get("permissions") or {}
            question = "助手想要更多权限：\n" + json.dumps(asked, ensure_ascii=False)[:300]
            if params.get("reason"):
                question += f"\n原因：{str(params['reason'])[:200]}"
            granted = asked if self._ask(question) else {}
            b._send({"id": msg["id"], "result": {"permissions": granted}})
        else:
            b._handle_side_message(msg)

    def _ask(self, question: str) -> bool:
        if self.ask is None:
            return False
        try:
            return self.ask(question)
        except Exception:
            log.exception("转发审批失败")
            return False


class CodexAgent(Skill):
    name = "agent"
    label = "帮主人干活"
    markers = {
        "任务": "【任务：要做的事】让助手在电脑上替主人干活（整理文件、写脚本、做表格、查资料等），"
        "在后台进行，做完你会收到结果再告诉主人。只在主人明确要你做事时用，任务描述要完整具体",
    }

    def __init__(
        self,
        cfg: AgentSkillConfig,
        root: Path,
        subscription: SubscriptionConfig,
        fallback: FallbackConfig,
        *,
        quota_ok: Callable[[], bool],
        ledger: UsageLedger | None = None,
        launcher: Callable[[str], CodexAppServerBackend] | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.sub = subscription
        self.fb = fallback
        self.quota_ok = quota_ok
        self.ledger = ledger
        self.launch = launcher or self._launch
        self._lock = threading.Lock()
        self._busy = False
        self._current: AgentRun | None = None  # 正在跑的任务（用于取消）

    @property
    def workspace(self) -> Path:
        ws = self.cfg.workspace
        return ws if ws.is_absolute() else self.root / ws

    # ---- 可用性 ----

    def enabled(self) -> bool:
        return self.cfg.enabled and (self.sub.enabled or self._deepseek_ready())

    def allowed(self, ctx: SkillContext) -> bool:
        return ctx.owner and ctx.private

    def status(self) -> str:
        if self._busy:
            return "正在干活"
        return f"工作文件夹：{self.workspace}"

    def _deepseek_ready(self) -> bool:
        if not (self.cfg.deepseek_fallback and self.fb.api_key is not None):
            return False
        if self.ledger is None:
            return True
        return self.ledger.month_cost("fallback") < self.fb.monthly_budget_usd

    def _choose_backend(self) -> str:
        if self.sub.enabled and self.quota_ok():
            return "subscription"
        if self._deepseek_ready():
            return "deepseek"
        raise SkillError("GPT 订阅额度快用完了，DeepSeek 也用不了，暂时没法干活")

    # ---- 执行 ----

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        task = arg.strip()
        if not task:
            raise SkillError("没说要做什么")
        if not self.allowed(ctx):
            raise SkillError("只有主人私聊时才能用")
        backend = self._choose_backend()
        with self._lock:
            if self._busy:
                raise SkillError("上一个任务还没做完，等它做完或让主人发 /停止任务")
            self._busy = True
        return SkillResult(
            "任务已经交给助手在后台做了，做完你会收到结果。现在先简短回一句（比如“好，我去弄”），"
            "不要假装已经做完。",
            later=lambda: self._work(task, backend, ctx),
            report=True,
        )

    def _work(self, task: str, backend: str, ctx: SkillContext) -> SkillResult:
        self.workspace.mkdir(parents=True, exist_ok=True)
        proc: CodexAppServerBackend | None = None
        try:
            proc = self.launch(backend)
            agent_run = AgentRun(
                proc,
                workspace=self.workspace,
                effort=self.cfg.effort,
                ask=ctx.ask,
                progress=ctx.notify,
                timeout=self.cfg.task_timeout_minutes * 60,
                progress_every=self.cfg.progress_every_seconds,
            )
            with self._lock:
                self._current = agent_run
            outcome = agent_run.run(task)
            outcome.backend = backend
            self._charge(backend, agent_run.tokens)
            log.info("任务结束（%s，%.0f 秒，%s）：%s", backend, outcome.seconds,
                     "完成" if outcome.ok else "未完成", outcome.text[:100])  # fmt: skip
            via = "（这次是 DeepSeek 驱动的）" if backend == "deepseek" else ""
            return SkillResult(f"任务：{task}\n助手的汇报{via}：\n{outcome.text}",
                               ok=outcome.ok, report=True)  # fmt: skip
        except ModelError as e:
            log.warning("任务失败：%s", e)
            return SkillResult(f"任务：{task}\n没做成：{e}", ok=False, report=True)
        except Exception:
            log.exception("任务出错")
            return SkillResult(f"任务：{task}\n没做成：程序出了点问题", ok=False, report=True)
        finally:
            if proc is not None:
                proc.close()
            with self._lock:
                self._current = None
                self._busy = False

    def cancel(self) -> bool:
        """主人发 /停止任务。"""
        with self._lock:
            current = self._current
        if current is None:
            return False
        current.cancelled.set()
        return True

    # ---- 启动 Codex ----

    def _launch(self, backend: str) -> CodexAppServerBackend:
        deny = default_deny(self.root) + [
            Path(p) if Path(p).is_absolute() else self.root / p for p in self.cfg.extra_deny
        ]
        overrides = permission_overrides(self.workspace, deny, web_search=self.cfg.web_search)
        env = None
        model = self.sub.model
        if backend == "deepseek":
            assert self.fb.api_key is not None
            env = dict(os.environ, COMPANION_DEEPSEEK_KEY=self.fb.api_key.get_secret_value())
            model = self.cfg.deepseek_model
            base = self.fb.base_url.rstrip("/") + "/"
            overrides |= {
                "model_provider": '"companion_deepseek"',
                "model_providers.companion_deepseek": (
                    '{name = "deepseek", base_url = ' + json.dumps(base)
                    + ', wire_api = "responses", env_key = "COMPANION_DEEPSEEK_KEY"}'
                ),
                "web_search": '"disabled"',  # 只有 deepseek-v4-pro 支持，且另外计费
            }  # fmt: skip
        if model:
            overrides["model"] = json.dumps(model)
        return CodexAppServerBackend.from_codex(
            self.sub.codex_command,
            self.workspace,
            overrides=overrides,
            timeout=600.0,  # 一条长命令可能好几分钟没有消息
            name="agent",
            env=env,
        )

    def _charge(self, backend: str, tokens: tuple[int, int]) -> None:
        if backend != "deepseek" or self.ledger is None:
            return
        tin, tout = tokens
        self.ledger.record(
            backend="fallback",
            model=f"codex-agent/{self.cfg.deepseek_model}",
            input_tokens=tin,
            output_tokens=tout,
            cost_usd=estimate_cost(
                tin, tout, self.fb.price_input_per_m, self.fb.price_output_per_m
            ),
            purpose="agent",
        )

    def close(self) -> None:
        self.cancel()
