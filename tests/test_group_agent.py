"""群聊里的沙箱助手：这里每一条都是一道安全边界。

群里的人能让她做任何事，所以限制必须由沙箱和程序保证，不能靠模型自觉。
沙箱本身的效果由真实 Codex 验收（tests/test_group_sandbox_real.py）；这里测程序这一侧的保证。
"""

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.config import (
    AgentSkillConfig,
    FallbackConfig,
    FilesSkillConfig,
    GroupAgentConfig,
    SkillsConfig,
    SubscriptionConfig,
)
from companion.models.openai_compat import ModelError
from companion.skills import SkillContext
from companion.skills import group_agent as module
from companion.skills.access import FileAccess
from companion.skills.agent import CodexAgent
from companion.skills.base import SkillError, Skills
from companion.skills.files import SendFile
from companion.skills.group_agent import (
    GroupAgent,
    SandboxUnsafe,
    check_thread,
    preflight,
    sandbox_overrides,
    trim_new_files,
)
from tests.test_agent import FakeProc, approval, done

GROUP, GROUP2 = "4000", "4001"
T0 = datetime(2026, 11, 8, 12, tzinfo=UTC)
IN_GROUP = SkillContext("qq", f"group:{GROUP}", "f", private=False, owner=False, now=T0)
OWNER_IN_GROUP = SkillContext("qq", f"group:{GROUP}", "o", private=False, owner=True, now=T0)
IN_GROUP2 = SkillContext("qq", f"group:{GROUP2}", "f", private=False, owner=False, now=T0)
OWNER_PRIVATE = SkillContext("qq", "private:2000", "o", private=True, owner=True, now=T0)
FRIEND_PRIVATE = SkillContext("qq", "private:3000", "f", private=True, owner=False, now=T0)
MINECRAFT = SkillContext("minecraft", "world", "f", private=False, owner=False, now=T0)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_WINDOWS", True)
    monkeypatch.setattr(module, "WATCH_SECONDS", 0.01)
    monkeypatch.setattr(module, "STOP_GRACE_SECONDS", 0.05)

    class Env:
        pass

    e = Env()
    e.root = tmp_path
    e.ws = tmp_path / "ws"
    e.files = FilesSkillConfig(inbox=e.ws / "inbox", folder_max_mb=1)
    e.secret = tmp_path / "secrets"
    e.access = FileAccess(e.files, tmp_path / "program", e.ws, lambda: [e.secret])
    e.clock = Clock()
    e.procs = []
    e.launched = []
    e.quota = {"ok": True}
    e.checks = []

    def launcher(folder, cwd):
        e.launched.append((folder, cwd))
        return e.procs.pop(0)

    def check(proc, folder, canary):
        e.checks.append((proc, folder, canary))

    e.agent = GroupAgent(
        GroupAgentConfig(enabled=True, limit_per_group=2, limit_all_groups=3, tool_paths=[]),
        tmp_path / "program",
        SubscriptionConfig(),
        e.access,
        lambda: [e.secret],
        tmp_path / "program" / "data" / "run",
        quota_ok=lambda: e.quota["ok"],
        clock=e.clock,
        launcher=launcher,
        check=check,
        thread_check=lambda thread, folder: None,
    )
    return e


def thread_start(proc: FakeProc) -> dict:
    return next(m["params"] for m in proc.sent if m.get("method") == "thread/start")


def prompt_of(proc: FakeProc) -> str:
    turn = next(m["params"] for m in proc.sent if m.get("method") == "turn/start")
    return turn["input"][0]["text"]


# ---------- 开关和场合 ----------


def test_off_by_default():
    assert GroupAgentConfig().enabled is False


def test_disabled_config_never_starts(env):
    env.agent.cfg.enabled = False
    assert not env.agent.enabled()
    skills = Skills([env.agent], SkillsConfig())
    assert skills.find("【任务：删库】", IN_GROUP) is None


@pytest.mark.parametrize(
    ("ctx", "ok"),
    [
        (IN_GROUP, True),
        (OWNER_IN_GROUP, True),  # 主人在群里说话，也是沙箱：群里别的人一样能影响她
        (OWNER_PRIVATE, False),
        (FRIEND_PRIVATE, False),
        (MINECRAFT, False),
    ],
)
def test_only_in_groups(env, ctx, ok):
    assert env.agent.allowed(ctx) is ok
    if not ok:
        with pytest.raises(SkillError):
            env.agent.run("任务", "x", ctx)


def test_group_gets_the_sandbox_and_owner_private_gets_the_full_agent(env, tmp_path):
    full = CodexAgent(
        AgentSkillConfig(workspace=env.ws), tmp_path, SubscriptionConfig(), FallbackConfig(),
        tmp_path / "run", quota_ok=lambda: True,
    )  # fmt: skip
    skills = Skills([full, env.agent], SkillsConfig())
    assert skills.find("【任务：x】", OWNER_PRIVATE)[1] is full
    assert skills.find("【任务：x】", OWNER_IN_GROUP)[1] is env.agent
    assert skills.find("【任务：x】", IN_GROUP)[1] is env.agent
    assert skills.find("【任务：x】", FRIEND_PRIVATE) is None
    assert "这个群的文件夹" in skills.prompt(IN_GROUP)
    assert "这个群的文件夹" not in skills.prompt(OWNER_PRIVATE)


def test_does_not_run_outside_windows(env, monkeypatch):
    monkeypatch.setattr(module, "_WINDOWS", False)
    assert not env.agent.enabled()


def test_needs_subscription_and_chat_folders(env):
    env.files.folder_enabled = False
    assert not env.agent.enabled()
    env.files.folder_enabled = True
    env.agent.sub = SubscriptionConfig(enabled=False)
    assert not env.agent.enabled()


# ---------- 权限配置 ----------


def overrides(env, *, tools=(), deny=()):
    cwd = env.root / "cwd"
    folder = env.access.folder(IN_GROUP.channel)
    return sandbox_overrides(folder, cwd, tools=list(tools), deny=list(deny)), cwd, folder


def fs_table(o) -> dict:
    return dict(re.findall(r'"([^"]+)" = "([^"]+)"', o[f"permissions.{module.PROFILE}.filesystem"]))


def posix(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def test_sandbox_is_default_deny(env):
    """不能以只读打底，也不能出现 :root：那样就是读全盘、只拦黑名单。"""
    o, cwd, folder = overrides(env)
    assert not any("extends" in key for key in o)
    assert ":root" not in o[f"permissions.{module.PROFILE}.filesystem"]
    assert o["windows.sandbox"] == '"mxc"'  # 另外两个后端都要求能读整个盘（实测）
    table = fs_table(o)
    assert table[posix(folder)] == "write"
    assert table[posix(cwd)] == "read"  # 工作目录只能读：写得进去就能留下 .codex 配置
    assert table[":minimal"] == "read"
    assert set(table) == {":minimal", posix(cwd), posix(folder)}


def test_sandbox_closes_everything_outside(env):
    o, _, _ = overrides(env)
    assert o[f"permissions.{module.PROFILE}.network.enabled"] == "false"
    assert o["web_search"] == '"disabled"'
    assert o["approval_policy"] == '"never"'
    assert o["shell_environment_policy.inherit"] == '"core"'
    assert o["notify"] == "[]"
    assert o["project_doc_max_bytes"] == "0"
    for feature in ("view_image", "image_generation", "hooks", "request_permissions_tool"):
        assert o[f"features.{feature}"] == "false"


def test_program_folders_are_denied_and_tools_can_reopen_them(env, tmp_path):
    (tmp_path / "pf" / "python").mkdir(parents=True)
    o, _, _ = overrides(env, tools=[tmp_path / "pf" / "python"], deny=[tmp_path / "pf"])
    table = fs_table(o)
    assert table[posix(tmp_path / "pf")] == "none"
    assert table[posix(tmp_path / "pf" / "python")] == "read"


def test_tool_paths_cannot_open_the_machine(env, tmp_path):
    fine = tmp_path / "python"
    fine.mkdir()
    env.ws.mkdir()
    (env.ws / "chats").mkdir()
    env.secret.mkdir()
    env.agent.root.mkdir(parents=True, exist_ok=True)
    env.agent.cfg.tool_paths = [
        str(fine),
        "relative/path",
        str(tmp_path / "missing"),
        str(Path(tmp_path.anchor)),  # 整个盘
        str(env.agent.root),  # 项目文件夹
        str(env.agent.root / "data"),
        str(env.ws / "chats"),  # 聊天文件夹
        str(env.secret),  # 黑名单
        str(tmp_path),  # 包含项目文件夹和黑名单的上层
    ]
    (env.agent.root / "data").mkdir(exist_ok=True)
    tools = env.agent._tools()
    assert fine.resolve() in tools
    for bad in (tmp_path, env.agent.root, env.ws / "chats", env.secret, Path(tmp_path.anchor)):
        assert bad.resolve() not in tools
    assert all("relative" not in str(p) and "missing" not in str(p) for p in tools)


def test_unswitchable_mcp_server_refuses_to_launch(env, tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers."weird name"]\ncommand = "x"\n', "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    with pytest.raises(SandboxUnsafe, match="关不掉"):
        env.agent._launch(env.access.folder(IN_GROUP.channel), tmp_path / "cwd")


# ---------- 工作目录：app-server 不在沙箱里，会加载工作目录下的 .codex/config.toml ----------


def test_thread_cwd_is_never_the_group_folder(env):
    proc = FakeProc(done())
    env.procs.append(proc)
    env.agent.run("任务", "x", IN_GROUP).later()
    folder, cwd = env.launched[0]
    assert folder == env.access.folder(IN_GROUP.channel)
    assert thread_start(proc)["cwd"] == str(cwd)
    assert not cwd.resolve().is_relative_to(env.access.chats.resolve())
    assert not cwd.resolve().is_relative_to(env.ws.resolve())  # 主人的助手也写不到
    assert cwd.resolve().is_relative_to(env.agent.run_dir.resolve())


def test_cwd_is_emptied_every_time(env):
    cwd = env.agent._fresh_cwd()
    (cwd / ".codex").mkdir()
    (cwd / ".codex" / "config.toml").write_text("planted", "utf-8")
    (cwd / "AGENTS.md").write_text("planted", "utf-8")
    again = env.agent._fresh_cwd()
    assert again == cwd and list(again.iterdir()) == []


def test_refuses_when_cwd_cannot_be_emptied(env, monkeypatch):
    cwd = env.agent._fresh_cwd()
    (cwd / "AGENTS.md").write_text("planted", "utf-8")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert not result.ok and env.launched == []


# ---------- 越权：一律拒绝，不打扰主人 ----------


def test_escalation_is_declined_without_asking_the_owner(env):
    asked = []
    ctx = SkillContext(
        "qq", f"group:{GROUP}", "f", private=False, owner=False, now=T0,
        ask=lambda q: asked.append(q) or True,  # 主人就算会点同意，也不该被问
    )  # fmt: skip
    proc = FakeProc([approval(), "WAIT", *done()])
    env.procs.append(proc)
    env.agent.run("任务", "x", ctx).later()
    assert asked == []
    assert next(m for m in proc.sent if m.get("id") == 7)["result"]["decision"] == "decline"
    assert thread_start(proc)["approvalPolicy"] == "never"


def test_permission_request_gets_nothing(env):
    asked = []
    ctx = SkillContext(
        "qq", f"group:{GROUP}", "f", private=False, owner=False, now=T0,
        ask=lambda q: asked.append(q) or True,
    )  # fmt: skip
    request = {
        "id": 8,
        "method": "item/permissions/requestApproval",
        "params": {"permissions": {"fileSystem": {"write": ["C:/"]}}},
    }
    proc = FakeProc([request, "WAIT", *done()])
    env.procs.append(proc)
    env.agent.run("任务", "x", ctx).later()
    assert asked == []
    assert next(m for m in proc.sent if m.get("id") == 8)["result"]["permissions"] == {}


def test_progress_is_not_spammed_into_the_group(env):
    notes = []
    ctx = SkillContext(
        "qq", f"group:{GROUP}", "f", private=False, owner=False, now=T0, notify=notes.append
    )
    env.procs.append(FakeProc(done()))
    env.agent.run("任务", "x", ctx).later()
    assert notes == []


def test_prompt_tells_where_it_can_work(env):
    proc = FakeProc(done())
    env.procs.append(proc)
    env.agent.run("任务", "整理{花括号}表格", IN_GROUP).later()
    text = prompt_of(proc)
    assert str(env.access.folder(IN_GROUP.channel)) in text
    assert "整理{花括号}表格" in text
    assert "群里的人，不是主人" in text


# ---------- 限额 ----------


def finish(env, ctx=IN_GROUP):
    env.procs.append(FakeProc(done()))
    return env.agent.run("任务", "x", ctx).later()


def test_limit_per_group_then_window_frees_it(env):
    finish(env)
    finish(env)
    with pytest.raises(SkillError, match="这个群派的任务够多了"):
        env.agent.run("任务", "x", IN_GROUP)
    assert finish(env, IN_GROUP2).ok  # 别的群不受影响
    env.clock.now = T0 + timedelta(hours=25)
    assert finish(env).ok


def test_limit_across_all_groups(env):
    finish(env)
    finish(env, IN_GROUP2)
    finish(env, IN_GROUP2)
    with pytest.raises(SkillError, match="所有群派的任务够多了"):
        env.agent.run("任务", "x", IN_GROUP)


def test_limit_survives_restart(env):
    finish(env)
    finish(env)
    again = GroupAgent(
        env.agent.cfg, env.agent.root, SubscriptionConfig(), env.access, lambda: [],
        env.agent.run_dir, quota_ok=lambda: True, clock=env.clock,
    )  # fmt: skip
    with pytest.raises(SkillError, match="够多了"):
        again.run("任务", "x", IN_GROUP)


def test_broken_quota_file_does_not_block(env):
    env.agent.state_path.parent.mkdir(parents=True, exist_ok=True)
    env.agent.state_path.write_text("{坏的", "utf-8")
    assert finish(env).ok


def test_owner_quota_and_deepseek_are_not_used(env):
    """订阅额度快用完就不做：不能让群里的人花主人的钱（没有 DeepSeek 兜底）。"""
    env.quota["ok"] = False
    with pytest.raises(SkillError, match="额度"):
        env.agent.run("任务", "x", IN_GROUP)
    assert env.launched == []
    assert env.agent._recent() == []


def test_one_task_at_a_time(env):
    env.procs.append(FakeProc(done()))
    first = env.agent.run("任务", "x", IN_GROUP)
    with pytest.raises(SkillError, match="还没做完"):
        env.agent.run("任务", "y", IN_GROUP2)
    assert len(env.agent._recent()) == 1  # 被拒绝的不占限额
    assert first.later().ok
    assert finish(env, IN_GROUP2).ok


def test_empty_task_is_refused(env):
    with pytest.raises(SkillError, match="没说"):
        env.agent.run("任务", "  ", IN_GROUP)


# ---------- 启动前自检 ----------


class CheckProc:
    """假的 command/exec：按要求造结果，顺便看脚本里写的路径。"""

    def __init__(self, **seen):
        self.seen = seen
        self.script = ""
        self.write_inside = seen.pop("write_inside", True)
        self.error = seen.pop("error", None)
        self.raw = seen.pop("raw", None)
        self.touch_outside = seen.pop("touch_outside", False)

    def _request(self, method, params):
        assert method == "command/exec"
        self.script = params["command"][-1]
        self.cwd = params["cwd"]
        if self.error:
            raise ModelError(self.error)
        if self.raw is not None:
            return {"stdout": self.raw}
        inside = re.search(r"WriteAllText\('([^']*\.sandbox-check[^']*)', '(\w+)'\)", self.script)
        outside = re.search(r"WriteAllText\('([^']*written[^']*)'", self.script)
        canary = re.search(r"ReadAllText\('([^']*canary[^']*)'\)", self.script)
        if self.write_inside:
            Path(inside.group(1)).write_text(inside.group(2), "utf-8")
        if self.touch_outside:
            Path(outside.group(1)).write_text("x", "utf-8")
        result = {
            "readOutside": "",
            "wroteOutside": False,
            "wroteInside": True,
            "readInside": inside.group(2) if self.write_inside else "",
            "network": False,
            "env": "PATH,TEMP",
        }
        if self.seen.pop("reads_canary", False):
            result["readOutside"] = Path(canary.group(1)).read_text("utf-8")
        result.update(self.seen)
        return {"stdout": json.dumps(result)}


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "group"
    path.mkdir()
    return path


def test_preflight_passes_when_sandbox_works(folder, tmp_path):
    proc = CheckProc()
    preflight(proc, folder, tmp_path / "run" / "canary.txt")
    assert proc.cwd == str(folder)
    assert list(folder.iterdir()) == []  # 不留东西
    assert not (tmp_path / "run" / "canary.txt").exists()


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"reads_canary": True}, "读到了"),
        ({"touch_outside": True}, "写到了"),
        ({"wroteOutside": True}, "写到了"),
        ({"network": True}, "连上了网"),
        ({"env": "PATH,SOME_API_KEY"}, "密钥"),
        ({"env": "PATH,GITHUB_TOKEN"}, "密钥"),
        ({"env": "PATH,OPENAI_API_KEY,TEMP"}, "密钥"),  # setx 存的变量在注册表里，沙箱能读
        ({"write_inside": False}, "读写不了"),
        ({"readInside": "别的内容"}, "读写不了"),
        ({"raw": ""}, "没跑起来"),
        ({"raw": "不是 JSON"}, "没跑起来"),
        ({"raw": "[1, 2]"}, "没跑起来"),
        ({"error": "elevated Windows sandbox requires effective `:root` read access"}, "没跑起来"),
    ],
)
def test_preflight_fails_closed(folder, tmp_path, kwargs, why):
    with pytest.raises(SandboxUnsafe, match=why):
        preflight(CheckProc(**kwargs), folder, tmp_path / "run" / "canary.txt")
    assert list(folder.iterdir()) == []
    assert not list((tmp_path / "run").iterdir())


def test_failed_check_runs_nothing_and_refunds(env):
    def bad(proc, folder, canary):
        raise SandboxUnsafe("读到了群文件夹以外的文件")

    env.agent.check = bad
    proc = FakeProc([])
    env.procs.append(proc)
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert not result.ok and result.report
    assert "读到了" not in result.text  # 细节进日志，不告诉群里
    assert not any(m.get("method") in ("thread/start", "turn/start") for m in proc.sent)
    assert proc.closed
    assert env.agent._recent() == []  # 没跑不算限额
    assert not env.agent._busy
    assert "没通过" in env.agent.status()


def test_launch_failure_is_reported_and_refunded(env):
    def explode(folder, cwd):
        raise ModelError("找不到 codex")

    env.agent.launch = explode
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert not result.ok and env.agent._recent() == [] and not env.agent._busy


def test_check_runs_in_the_same_process_before_the_task(env):
    proc = FakeProc(done())
    env.procs.append(proc)
    env.agent.run("任务", "x", IN_GROUP).later()
    assert env.checks[0][0] is proc
    assert env.checks[0][1] == env.access.folder(IN_GROUP.channel)
    canary = env.checks[0][2]
    assert not canary.resolve().is_relative_to(env.ws.resolve())
    assert "上次自检通过" in env.agent.status()


GOOD_THREAD = {
    "activePermissionProfile": {"id": module.PROFILE, "extends": None},
    "approvalPolicy": "never",
    "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
    "instructionSources": [],
}


def good_thread(folder: Path, **changes) -> dict:
    sandbox = {**GOOD_THREAD["sandbox"], "writableRoots": [str(folder)]}
    return {**GOOD_THREAD, "sandbox": sandbox, **changes}


def test_thread_check_accepts_the_expected_config(folder):
    check_thread(good_thread(folder), folder)


@pytest.mark.parametrize(
    "changes",
    [
        {"activePermissionProfile": {"id": "other", "extends": None}},
        {"activePermissionProfile": {"id": module.PROFILE, "extends": ":read-only"}},
        {"activePermissionProfile": None},
        {"approvalPolicy": "on-request"},
        {"sandbox": {"writableRoots": [], "networkAccess": True}},
        {"sandbox": {"networkAccess": False}},
        {"sandbox": None},
        {"instructionSources": ["C:/AGENTS.md"]},
    ],
)
def test_thread_check_fails_closed(folder, changes):
    with pytest.raises(SandboxUnsafe):
        check_thread(good_thread(folder, **changes), folder)


def test_thread_check_rejects_extra_writable_roots(folder, tmp_path):
    thread = good_thread(folder)
    thread["sandbox"]["writableRoots"].append(str(tmp_path))
    with pytest.raises(SandboxUnsafe, match="可写位置"):
        check_thread(thread, folder)


def test_wrong_thread_config_starts_no_turn_and_refunds(env):
    def bad(thread, folder):
        raise SandboxUnsafe("线程配置不对（网络）")

    env.agent.thread_check = bad
    proc = FakeProc(done())
    env.procs.append(proc)
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert not result.ok and "网络" not in result.text
    assert not any(m.get("method") == "turn/start" for m in proc.sent)
    assert env.agent._recent() == [] and not env.agent._busy


def test_codex_errors_do_not_reach_the_group(env):
    class FailingProc(FakeProc):
        def _request(self, method, params):
            if method == "turn/start":
                raise ModelError(r"failed to load C:\Users\主人\.codex\x")
            return super()._request(method, params)

    env.procs.append(FailingProc([]))
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert not result.ok and "主人" not in result.text and ".codex" not in result.text


# ---------- 时限和文件夹大小 ----------


class HungProc(FakeProc):
    """一条卡住的命令：不发消息，直到进程被杀。"""

    def __init__(self, on_start=None):
        super().__init__([])
        self.killed = threading.Event()
        self.on_start = on_start

    def _request(self, method, params):
        result = super()._request(method, params)
        if method == "turn/start" and self.on_start:
            self.on_start()
        return result

    def close(self):
        self.killed.set()
        super().close()

    def _receive(self):
        if self.killed.wait(10):
            raise ModelError("codex app-server 意外退出")
        raise AssertionError("进程没有被杀")


def test_hung_command_is_killed_at_the_deadline(env):
    env.agent.cfg.task_timeout_minutes = 0.001
    proc = HungProc()
    env.procs.append(proc)
    started = time.monotonic()
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert proc.killed.is_set() and time.monotonic() - started < 5
    assert not result.ok and "超过了" in result.text and result.report
    assert not env.agent._busy


def test_owner_can_stop_a_group_task(env):
    proc = HungProc()
    env.procs.append(proc)
    pending = env.agent.run("任务", "x", IN_GROUP)
    assert env.agent.cancel() is False  # 还没开始
    box = {}
    worker = threading.Thread(target=lambda: box.update(result=pending.later()))
    worker.start()
    for _ in range(200):
        if env.agent.cancel():
            break
        time.sleep(0.01)
    worker.join(timeout=10)
    assert not box["result"].ok and "主人让它停下" in box["result"].text
    assert proc.killed.is_set()


def test_filling_the_folder_stops_the_task_and_removes_what_it_wrote(env):
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    (folder / "旧文件.txt").write_bytes(b"x" * 1000)

    def fill():
        (folder / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))

    proc = HungProc(on_start=fill)
    env.procs.append(proc)
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert not result.ok and "写满" in result.text and "big.bin" in result.text
    assert proc.killed.is_set()
    assert not (folder / "big.bin").exists()
    assert (folder / "旧文件.txt").exists()


def test_process_is_stopped_before_cleaning_up(env):
    """容器里的进程可能还在写，先结束再清理，不然删了又写回来。"""
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    order = []

    class Proc(FakeProc):
        def close(self):
            order.append("close")
            super().close()

    env.procs.append(Proc(done()))
    real_trim = module.trim_new_files

    def spy(*args):
        order.append("trim")
        return real_trim(*args)

    module.trim_new_files = spy
    try:
        env.agent.run("任务", "x", IN_GROUP).later()
    finally:
        module.trim_new_files = real_trim
    assert order[:2] == ["close", "trim"]


def test_full_folder_is_refused_before_starting(env):
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    (folder / "旧文件.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(SkillError, match="满了"):
        env.agent.run("任务", "x", IN_GROUP)
    assert env.agent._recent() == [] and env.launched == []
    assert (folder / "旧文件.bin").exists()


def test_trim_only_touches_what_this_task_wrote(tmp_path):
    (tmp_path / "old.bin").write_bytes(b"x" * 500)
    before = module._snapshot(tmp_path)
    (tmp_path / "small.bin").write_bytes(b"x" * 100)
    (tmp_path / "large.bin").write_bytes(b"x" * 400)
    removed = trim_new_files(tmp_path, before, limit=650)
    assert removed == ["large.bin"]  # 大的先删，够了就停
    assert (tmp_path / "old.bin").exists() and (tmp_path / "small.bin").exists()


def test_trim_leaves_old_files_even_when_over_limit(tmp_path):
    (tmp_path / "old.bin").write_bytes(b"x" * 500)
    before = module._snapshot(tmp_path)
    assert trim_new_files(tmp_path, before, limit=10) == []
    assert (tmp_path / "old.bin").exists()


def test_within_limit_nothing_is_removed(env):
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)

    class WritingProc(FakeProc):
        def _request(self, method, params):
            if method == "turn/start":
                (folder / "产出.csv").write_text("a,b", "utf-8")
            return super()._request(method, params)

    env.procs.append(WritingProc(done()))
    result = env.agent.run("任务", "x", IN_GROUP).later()
    assert result.ok and (folder / "产出.csv").exists() and "删掉" not in result.text


# ---------- 产出的文件 ----------


def test_output_can_be_sent_only_from_this_group(env):
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    (folder / "产出.csv").write_text("a,b", "utf-8")
    sender = SendFile(env.files, env.access)
    assert sender.resolve("产出.csv", IN_GROUP) == (folder / "产出.csv").resolve()
    with pytest.raises(SkillError):
        sender.resolve(str(folder / "产出.csv"), IN_GROUP2)
    with pytest.raises(SkillError):
        sender.resolve(str(folder / "产出.csv"), FRIEND_PRIVATE)


@pytest.mark.parametrize(
    "name", ["工具.exe", "启动.bat", "脚本.ps1", "a.CMD", "x.js", "快捷.lnk", "desktop.ini"]
)
def test_runnable_output_is_not_handed_out(env, name):
    """以前群文件夹里造不出这些文件（save_new 拒绝）；有了沙箱能造，就不能往外递。"""
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    (folder / name).write_bytes(b"x")
    with pytest.raises(SkillError, match="不能发"):
        SendFile(env.files, env.access).resolve(name, IN_GROUP)
    env.access.check_send((folder / name).resolve(), OWNER_PRIVATE)  # 主人私聊不受影响


def test_scripts_for_reading_can_still_be_sent(env):
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    (folder / "统计.py").write_text("print(1)", "utf-8")
    assert SendFile(env.files, env.access).resolve("统计.py", IN_GROUP)


def test_link_to_outside_cannot_be_sent(env, tmp_path):
    folder = env.access.folder(IN_GROUP.channel)
    folder.mkdir(parents=True)
    target = tmp_path / "outside.txt"
    target.write_text("秘密", "utf-8")
    link = folder / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("本机不能建符号链接")
    with pytest.raises(SkillError):
        SendFile(env.files, env.access).resolve("link.txt", IN_GROUP)
