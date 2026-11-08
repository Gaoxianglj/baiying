"""Agent：让独立的 Codex 进程替主人干活（整理文件、写脚本、查资料……）。

- 【任务：要做的事】：只限主人私聊或本地命令行（群聊另有沙箱版，见 group_agent.py）。
  她先回一句，任务在后台跑，做完她用自己的话汇报。
- 每个任务启动一个新的 `codex app-server`，用专属权限配置：
  工作文件夹（默认 data/workspace）可读写；其余位置只读；黑名单路径连读都不行。
  实测 Windows 沙箱会真正拦下黑名单读取和工作文件夹外的写入（2026-10-24）。
- 进程和线程的工作目录不能是工作文件夹：app-server 本身不在沙箱里，会加载工作目录及其上级里的
  .codex/config.toml，上一个任务留下的 MCP 配置能在沙箱外运行程序（tests/test_agent_sandbox_real.py）。
  所以工作目录放在 data/run 下沙箱写不到的地方，每次清空。那里在黑名单里，沙箱连进都进不去，
  命令必须把 workdir 设成工作文件夹，apply_patch 的相对路径也不再指向工作文件夹，这些都写在提示词里。
- 越权操作（如写工作文件夹外）Codex 会请求审批，转给主人：回复 /批准 或 /拒绝，超时算拒绝。
  写文件的审批要列出路径和内容（批准一次就可能写进上级的 .codex/config.toml，见 file_change_question）。
- 联网：沙箱里的命令在 Windows 上做不了 HTTPS（受限令牌拿不到 TLS 凭据），所以用 Codex 自带的联网搜索。
- 订阅额度到阈值时，改用 DeepSeek 驱动 Codex（key 通过环境变量传给子进程，不出现在命令行里），
  token 用量按兜底 API 的价格记账。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import sys
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
- 每条命令都把 workdir 设成工作文件夹（不设的话命令启动不了），apply_patch 一律写绝对路径。
- 工作文件夹的根目录不要建 .git、.codex、.agents（之后的命令会全部启动失败）；要用 git 就放进子文件夹。
- 需要查资料就用联网搜索；命令行里的网络下载在沙箱里用不了。
- 不要删除工作文件夹外的任何东西，不要碰密码、密钥、浏览器数据。
- 做完后用中文简要汇报：做了什么、结果如何、产出的文件路径。做不到就说明卡在哪里。

任务：{task}"""

_WINDOWS = sys.platform == "win32"
# Codex 的 elevated 后端每次进沙箱前，都要给写入根下的这些目录加 deny ACE
_PROTECTED_DIRS = (".codex", ".git", ".agents")
# 写进去就可能让程序在沙箱外运行，或改掉助手听的指令
_CODEX_FILES = (*_PROTECTED_DIRS, "agents.md", "agents.override.md")
_OUTSIDE = "工作文件夹外"
_CODEX_FILE = "Codex 配置"
_KINDS = {"add": "新建", "update": "修改", "delete": "删除"}
# 审批问题的长度：QQ 一条消息不宜太长，模型也不能靠长度把要紧的东西挤掉
_ASK_FILES = 5  # 最多列几个文件
_ASK_FILES_CHARS = 1000  # 文件列表最多占多少字
_ASK_PATH_CHARS = 200
_ASK_PREVIEW_CHARS = 200
_ASK_PREVIEW_LINES = 6
_ASK_REASON_CHARS = 200


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
    entries = {toml_path(workspace): "write"}
    for path in deny:
        if path.exists():  # 不存在的路径 Codex 会报错；之后出现的会在下一个任务启动时加上
            entries[toml_path(path)] = "deny"
    return {
        "default_permissions": '"companion_agent"',
        "permissions.companion_agent.extends": '":read-only"',
        "permissions.companion_agent.filesystem": toml_table(entries),
        "permissions.companion_agent.network.enabled": "true",
        "approval_policy": '"on-request"',
        "approvals_reviewer": '"user"',  # 不让 Codex 自己审，转给主人
        "web_search": '"live"' if web_search else '"disabled"',
    }


def toml_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def toml_table(entries: dict[str, str]) -> str:
    return "{" + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in entries.items()) + "}"


def empty_dir(path: Path) -> Path:
    """清空并返回这个目录，给 Codex 当工作目录。"""
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise OSError(f"清不空工作目录：{path}")
    return path


def _can_edit_acl(path: Path) -> bool:
    """这个进程能不能改 path 的 ACL。沙箱建的目录属主是沙箱用户，主人的进程改不了。"""
    if not _WINDOWS:
        return True
    read_control, write_dac, share_all, open_existing, backup_semantics = (
        0x00020000, 0x00040000, 7, 3, 0x02000000,
    )  # fmt: skip
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(
        str(path), read_control | write_dac, share_all, None, open_existing, backup_semantics, None
    )
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel32.CloseHandle(handle)
    return True


def move_aside_unmanageable(workspace: Path) -> list[str]:
    """把工作文件夹根目录下 Codex 改不了 ACL 的 .codex、.git、.agents 改名挪开，返回新名字。

    沙箱自己建的这些目录，elevated 后端加 deny ACE 时会失败，之后所有沙箱操作都起不来，连线程都开不了
    （实测 2026-11-08，Codex 0.164）。不挪开的话，一个任务就能让之后的任务全部失败。
    只改名不删，主人可以看里面是什么。主人自己建的（不影响运行）和子文件夹里的不碰。
    """
    moved = []
    for name in _PROTECTED_DIRS:
        path = workspace / name
        if not path.exists() or _can_edit_acl(path):
            continue
        target = workspace / f"{name}-留下的-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            path.rename(target)
        except OSError:
            log.warning("挪不开工作文件夹里的 %s", name, exc_info=True)
            continue
        log.warning("工作文件夹根目录有沙箱建的 %s，改名为 %s", name, target.name)
        moved.append(target.name)
    return moved


def _visible(text: str) -> str:
    """不可见字符（换行、方向控制符等）换成问号：这些字是模型写的，不能让它伪造问题的格式。"""
    return "".join(c if c.isprintable() else "?" for c in text)


def _short_path(path: str) -> str:
    """太长的路径去掉中间，开头和结尾最要紧。"""
    text = _visible(path) or "（没给路径）"
    if len(text) <= _ASK_PATH_CHARS:
        return text
    half = _ASK_PATH_CHARS // 2
    return f"{text[:half]}…{text[-half:]}"


def _quoted(diff: str) -> list[str]:
    """内容开头。每行前面加 >：模型写的内容再怎么排版，也冒充不了问题本身。"""
    text = diff.strip()
    lines = [line for line in text[:_ASK_PREVIEW_CHARS].splitlines() if line.strip()]
    quoted = [f"> {_visible(line.replace(chr(9), '  '))}" for line in lines[:_ASK_PREVIEW_LINES]]
    if len(text) > _ASK_PREVIEW_CHARS or len(lines) > _ASK_PREVIEW_LINES:
        quoted.append(f"（内容共 {len(text)} 字，只显示开头）")
    return quoted


def _path_warnings(path: str, workspace: Path, cwd: Path) -> list[str]:
    """这个路径要主人留意什么。按实际会写到的位置判断：.. 和链接都算上，.CODEX、.codex. 也是 .codex。"""
    try:
        real = (cwd / path).resolve()  # 相对路径按线程的工作目录算，和 Codex 一样
        inside = real.is_relative_to(workspace.resolve())
    except (OSError, ValueError, RuntimeError):
        real, inside = None, False
    parts = [*Path(path).parts, *(real.parts if real else ())]
    names = {part.split(":")[0].rstrip(" .").lower() for part in parts}
    warnings = [] if inside else [_OUTSIDE]
    if names.intersection(_CODEX_FILES):
        warnings.append(_CODEX_FILE)
    return warnings


def _reason_line(params: dict[str, Any]) -> str:
    reason = params.get("reason")
    if not reason:
        return ""
    text = _visible(str(reason))[:_ASK_REASON_CHARS]
    return f"\n助手自己说的原因（不一定可信，以上面的内容为准）：{text}"


def file_change_question(
    changes: list[dict[str, Any]] | None, params: dict[str, Any], workspace: Path, cwd: Path
) -> str:
    """把写文件的申请写成给主人看的问题。

    路径、内容、原因都是模型写的，任务又可能来自带诱导的文件，所以要防它骗人：
    警告放在最前面，有警告的文件排在前面，一堆无害的文件挤不掉它们；原因放最后，不当依据。
    """
    blocks: list[tuple[list[str], list[str]]] = []
    for change in changes or []:
        path = str(change.get("path") or "")
        kind = change.get("kind") or {}
        target = str(kind.get("move_path") or "")
        warnings = _path_warnings(path, workspace, cwd)
        head = f"{_KINDS.get(str(kind.get('type')), '改动')} {_short_path(path)}"
        if target:  # 改名也算写：可以把无害的文件移到 .codex/config.toml
            warnings += [w for w in _path_warnings(target, workspace, cwd) if w not in warnings]
            head = f"移动 {_short_path(path)} → {_short_path(target)}"
        if warnings:
            head += f"（{'、'.join(warnings)}）"
        blocks.append((warnings, [head, *_quoted(str(change.get("diff") or ""))]))

    if blocks:
        lines = [f"助手申请写 {len(blocks)} 个文件，批准前看清楚："]
    else:
        lines = ["助手申请修改文件，但这边看不到要改什么（哪个文件、什么内容都不知道），建议拒绝。"]
    outside = sum(_OUTSIDE in warnings for warnings, _ in blocks)
    codex = sum(_CODEX_FILE in warnings for warnings, _ in blocks)
    if outside:
        lines.append(f"注意：{outside} 个在工作文件夹外，不确定就拒绝。")
    if codex:
        lines.append(
            f"注意：{codex} 个是 .codex、.git、AGENTS.md 这类文件，"
            "写进去可能让程序在沙箱外运行，或改掉助手听的指令。"
        )
    if params.get("grantRoot"):
        root = _short_path(str(params["grantRoot"]))
        lines.append(f"注意：它还想让这次任务之后都能随便写这个文件夹：{root}")

    shown = used = 0
    for _, block in sorted(blocks, key=lambda item: not item[0]):
        size = sum(len(line) for line in block)
        if shown and (shown >= _ASK_FILES or used + size > _ASK_FILES_CHARS):
            break
        shown += 1
        used += size
        lines += [f"{shown}. {block[0]}", *block[1:]]
    if shown < len(blocks):
        lines.append(f"还有 {len(blocks) - shown} 个文件没显示。")
    return "\n".join(lines) + _reason_line(params)


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
        preamble: str = TASK_PREAMBLE,
        approval_policy: str = "on-request",
        cwd: Path | None = None,
        on_thread: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.workspace = workspace
        self.effort = effort
        self.ask = ask
        self.progress = progress
        self.timeout = timeout
        self.progress_every = progress_every
        self.preamble = preamble
        self.approval_policy = approval_policy
        self.cwd = cwd or workspace  # 线程的工作目录；app-server 会加载这里的 .codex/config.toml
        self.on_thread = on_thread  # 拿到线程实际生效的配置后、开始干活前，可以在这里拦下
        self.cancelled = threading.Event()
        self.tokens = (0, 0)  # 输入、输出 token
        self._file_changes: dict[str, list[dict[str, Any]]] = {}  # 正在进行的写文件，审批时要用

    def run(self, task: str) -> AgentOutcome:
        b = self.backend
        started = time.monotonic()
        b.start()
        thread = b._request(
            "thread/start",
            {"cwd": str(self.cwd), "approvalPolicy": self.approval_policy, "ephemeral": True},
        )
        thread_id = (thread.get("thread") or {}).get("id")
        if not thread_id:
            raise ModelError(f"thread/start 返回异常：{str(thread)[:200]}")
        if self.on_thread is not None:
            self.on_thread(thread)
        text = self.preamble.format(workspace=self.workspace, task=task)
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
            if method == "item/started":
                item = params.get("item") or {}
                if item.get("type") == "fileChange":
                    self._file_changes[str(item.get("id"))] = item.get("changes") or []
            elif method == "item/fileChange/patchUpdated":
                self._file_changes[str(params.get("itemId"))] = params.get("changes") or []
            elif method == "item/completed":
                item = params.get("item") or {}
                kind = item.get("type")
                if kind == "commandExecution":
                    commands += 1
                elif kind == "fileChange":
                    files += len(item.get("changes") or [])
                    self._file_changes.pop(str(item.get("id")), None)
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
            if method == "item/fileChange/requestApproval":
                # 请求本身只有 itemId，路径和内容在之前的消息里
                changes = self._file_changes.get(str(params.get("itemId")))
                question = file_change_question(changes, params, self.workspace, self.cwd)
            else:
                what = params.get("command") or "（看不到要跑什么命令，建议拒绝）"
                question = f"助手想做一件需要你批准的事：\n{str(what)[:300]}" + _reason_line(params)
            ok = self._ask(question)
            b._send({"id": msg["id"], "result": {"decision": "accept" if ok else "decline"}})
        elif method == "item/permissions/requestApproval":
            asked = params.get("permissions") or {}
            question = "助手想要更多权限：\n" + json.dumps(asked, ensure_ascii=False)[:300]
            question += _reason_line(params)
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
        run_dir: Path,
        *,
        quota_ok: Callable[[], bool],
        ledger: UsageLedger | None = None,
        launcher: Callable[[str], CodexAppServerBackend] | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.sub = subscription
        self.fb = fallback
        self.run_dir = run_dir  # data/run：沙箱写不到
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

    @property
    def cwd(self) -> Path:
        """Codex 进程和线程的工作目录。"""
        return self.run_dir / "agent-cwd"

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
        # 工作目录的上级也在 Codex 找配置的范围内，沙箱能写到它们就又能逃出去
        if self.cwd.resolve().is_relative_to(self.workspace.resolve()):
            raise SkillError(
                "工作文件夹设得太大了，把程序的 data 文件夹也包进去了，不安全，让主人改小"
            )
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
        proc: CodexAppServerBackend | None = None
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            moved = move_aside_unmanageable(self.workspace)
            cwd = empty_dir(self.cwd)  # 也让 _launch 的黑名单加得上 data/run（不存在的路径不加）
            proc = self.launch(backend)
            agent_run = AgentRun(
                proc,
                workspace=self.workspace,
                cwd=cwd,
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
            note = (
                f"开始前发现工作文件夹根目录有以前的任务建的目录，已改名挪开：{'、'.join(moved)}。"
                "这类目录有可能是诱导内容留下的，告诉主人去看一眼。\n"
                if moved
                else ""
            )
            return SkillResult(f"任务：{task}\n{note}助手的汇报{via}：\n{outcome.text}",
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
            self.cwd,  # 进程本身也不在工作文件夹里启动
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
