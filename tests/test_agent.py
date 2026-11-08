"""S-04 Agent：权限配置、审批转发、进度、超时取消、只限主人私聊、DeepSeek 兜底、QQ 全流程。"""

import asyncio
import queue
import random
import threading
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from companion.bodies.qq.bot import QQBot
from companion.config import (
    AgentSkillConfig,
    FallbackConfig,
    QQConfig,
    SubscriptionConfig,
    load_config,
)
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.models.codex_app_server import lockdown_overrides
from companion.models.usage import UsageLedger
from companion.skills import SkillContext, SkillError
from companion.skills import agent as module
from companion.skills.agent import (
    AgentRun,
    CodexAgent,
    default_deny,
    move_aside_unmanageable,
    permission_overrides,
)
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, OWNER, FRIEND = "1000", "2000", "3000"
OWNER_CTX = SkillContext(body="qq", channel="private:2000", person_id="p", private=True, owner=True)


# ---------- 权限配置 ----------


def test_permission_overrides(tmp_path):
    ws = tmp_path / "workspace"
    secret = tmp_path / "secret"
    ws.mkdir()
    secret.mkdir()
    o = permission_overrides(ws, [secret, tmp_path / "missing"], web_search=True)
    fs = o["permissions.companion_agent.filesystem"]
    assert f'"{str(ws.resolve()).replace(chr(92), "/")}" = "write"' in fs
    assert f'"{str(secret.resolve()).replace(chr(92), "/")}" = "deny"' in fs
    assert "missing" not in fs  # 不存在的路径不加（Codex 会报错）
    assert o["default_permissions"] == '"companion_agent"'
    assert o["permissions.companion_agent.extends"] == '":read-only"'
    assert o["approvals_reviewer"] == '"user"' and o["web_search"] == '"live"'


def test_default_deny_covers_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("NAPCAT_DIR", str(tmp_path / "napcat"))
    deny = default_deny(tmp_path)
    for p in (tmp_path / ".env", tmp_path / "data" / "memory", tmp_path / "napcat"):
        assert p in deny
    assert any(p.name == ".codex" for p in deny)
    assert any("Chrome" in str(p) for p in deny)


def test_lockdown_disables_user_mcp_servers(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.node_repl]\ncommand = "x"\n[mcp_servers."weird name"]\ncommand = "y"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    o = lockdown_overrides()
    assert o["mcp_servers.node_repl.enabled"] == "false"
    assert not any("weird" in k for k in o)
    assert o["features.computer_use"] == "false" and o["features.plugins"] == "false"


# ---------- 跑任务 ----------


class FakeProc:
    """假的 app-server：按脚本吐消息；审批请求等客户端回复后才继续。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.closed = False
        self.answered = threading.Event()

    def start(self):
        pass

    def close(self):
        self.closed = True

    def _request(self, method, params):
        self.sent.append({"method": method, "params": params})
        return {"thread": {"id": "t1"}} if method == "thread/start" else {"turn": {"id": "u1"}}

    def _send(self, msg):
        self.sent.append(msg)
        if "result" in msg:
            self.answered.set()

    def _handle_side_message(self, msg):
        pass

    def _receive(self):
        if not self.script:
            time.sleep(0.01)
            return {"method": "noop", "params": {}}
        item = self.script.pop(0)
        if item == "WAIT":
            assert self.answered.wait(5)
            return {"method": "noop", "params": {}}
        if item == "SLOW":
            time.sleep(0.05)
            return {"method": "noop", "params": {}}
        return item


def done(text="整理好了，文件在 data/workspace/汇总.xlsx"):
    return [
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": text, "phase": "final_answer"}},
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {"tokenUsage": {"total": {"inputTokens": 1000, "outputTokens": 200}}},
        },
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]


def cmd_item(n=1):
    return {
        "method": "item/completed",
        "params": {"item": {"type": "commandExecution", "status": "completed"}},
    }


def approval(cmd="New-Item C:/outside/x.txt"):
    return {
        "id": 7,
        "method": "item/commandExecution/requestApproval",
        "params": {"command": cmd, "reason": "要写工作文件夹外"},
    }


def make_run(proc, tmp_path, ask=None, progress=None, timeout=60, every=1000):
    return AgentRun(
        proc,
        workspace=tmp_path,
        effort="low",
        ask=ask,
        progress=progress,
        timeout=timeout,
        progress_every=every,
    )


def test_run_counts_and_reports(tmp_path):
    proc = FakeProc(
        [
            cmd_item(),
            cmd_item(),
            {
                "method": "item/completed",
                "params": {
                    "item": {"type": "fileChange", "changes": [{"path": "a"}, {"path": "b"}]}
                },
            },
            *done(),
        ]
    )
    run = make_run(proc, tmp_path)
    out = run.run("整理表格")
    assert out.ok and out.commands == 2 and out.files == 2 and "汇总.xlsx" in out.text
    assert run.tokens == (1000, 200)
    started = proc.sent[1]["params"]
    assert (
        "整理表格" in started["input"][0]["text"] and str(tmp_path) in started["input"][0]["text"]
    )


@pytest.mark.parametrize("answer", [True, False])
def test_approval_is_forwarded(tmp_path, answer):
    asked = []
    proc = FakeProc([approval(), "WAIT", *done()])
    run = make_run(proc, tmp_path, ask=lambda q: asked.append(q) or answer)
    run.run("x")
    assert "New-Item C:/outside/x.txt" in asked[0] and "要写工作文件夹外" in asked[0]
    reply = next(m for m in proc.sent if m.get("id") == 7)
    assert reply["result"] == {"decision": "accept" if answer else "decline"}


def test_no_ask_means_decline(tmp_path):
    proc = FakeProc([approval(), "WAIT", *done()])
    make_run(proc, tmp_path).run("x")
    assert next(m for m in proc.sent if m.get("id") == 7)["result"]["decision"] == "decline"


def test_timeout_interrupts(tmp_path):
    proc = FakeProc(
        [
            "SLOW",
            "SLOW",
            "SLOW",
            {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
        ]
    )
    out = make_run(proc, tmp_path, timeout=0.01).run("x")
    assert not out.ok and "超时" in out.text
    assert any(m.get("method") == "turn/interrupt" for m in proc.sent)


def test_progress_notes(tmp_path):
    notes = []
    proc = FakeProc([cmd_item(), "SLOW", cmd_item(), *done()])
    make_run(proc, tmp_path, progress=notes.append, every=0.01).run("x")
    assert notes and "已执行" in notes[0]


# ---------- 技能 ----------


def make_agent(tmp_path, procs, *, quota=True, key=True):
    launched = []

    def launcher(backend):
        launched.append(backend)
        return procs.pop(0)

    fb = FallbackConfig(api_key=SecretStr("sk-test") if key else None, monthly_budget_usd=5)
    agent = CodexAgent(
        AgentSkillConfig(workspace=tmp_path / "ws"),
        tmp_path,
        SubscriptionConfig(),
        fb,
        tmp_path / "run",
        quota_ok=lambda: quota,
        ledger=UsageLedger(tmp_path / "usage.jsonl"),
        launcher=launcher,
    )
    return agent, launched


def test_only_owner_private(tmp_path):
    agent, _ = make_agent(tmp_path, [])
    assert agent.allowed(OWNER_CTX)
    group = SkillContext(body="qq", channel="group:1", person_id="p", private=False, owner=True)
    friend = SkillContext(body="qq", channel="private:3", person_id="p", private=True, owner=False)
    assert not agent.allowed(group) and not agent.allowed(friend)
    with pytest.raises(SkillError):
        agent.run("任务", "删库", friend)


def test_background_task_busy_and_report(tmp_path):
    proc = FakeProc(done())
    agent, launched = make_agent(tmp_path, [proc])
    result = agent.run("任务", "整理表格", OWNER_CTX)
    assert result.report and "不要假装已经做完" in result.text
    with pytest.raises(SkillError, match="还没做完"):
        agent.run("任务", "再来一个", OWNER_CTX)
    out = result.later()
    assert out.ok and out.report and "汇总.xlsx" in out.text and launched == ["subscription"]
    assert proc.closed and (tmp_path / "ws").is_dir()
    assert agent.run("任务", "再来一个", OWNER_CTX).later is not None  # 做完就能接新任务


def test_deepseek_when_quota_runs_out(tmp_path):
    agent, launched = make_agent(tmp_path, [FakeProc(done())], quota=False)
    out = agent.run("任务", "x", OWNER_CTX).later()
    assert launched == ["deepseek"] and "DeepSeek 驱动" in out.text
    assert agent.ledger.month_cost("fallback") > 0  # 按 token 记账
    nothing, _ = make_agent(tmp_path, [], quota=False, key=False)
    with pytest.raises(SkillError, match="没法干活"):
        nothing.run("任务", "x", OWNER_CTX)


def test_deepseek_launch_keeps_key_off_command_line(tmp_path, monkeypatch):
    captured = {}

    def fake_from_codex(cmd, workdir, *, overrides, **kw):
        captured.update(overrides=overrides, **kw)
        return FakeProc([])

    monkeypatch.setattr("companion.skills.agent.CodexAppServerBackend.from_codex", fake_from_codex)
    agent = CodexAgent(
        AgentSkillConfig(),
        tmp_path,
        SubscriptionConfig(),
        FallbackConfig(api_key=SecretStr("sk-secret")),
        tmp_path / "run",
        quota_ok=lambda: False,
    )
    agent._launch("deepseek")
    assert "sk-secret" not in str(captured["overrides"])
    assert captured["env"]["COMPANION_DEEPSEEK_KEY"] == "sk-secret"
    assert captured["overrides"]["model_provider"] == '"companion_deepseek"'
    assert captured["overrides"]["model"] == '"deepseek-flash"'


# ---------- 工作目录：不能让留在工作文件夹里的配置在沙箱外运行 ----------


def test_thread_and_process_run_outside_the_workspace(tmp_path, monkeypatch):
    proc = FakeProc(done())
    agent, _ = make_agent(tmp_path, [proc])
    assert agent.run("任务", "x", OWNER_CTX).later().ok
    thread = next(m for m in proc.sent if m.get("method") == "thread/start")["params"]
    assert thread["cwd"] == str(tmp_path / "run" / "agent-cwd")
    turn = next(m for m in proc.sent if m.get("method") == "turn/start")["params"]
    text = turn["input"][0]["text"]
    assert str(agent.workspace) in text and "workdir" in text and "绝对路径" in text

    captured = {}

    def fake_from_codex(cmd, workdir, *, overrides, **kw):
        captured.update(workdir=workdir, overrides=overrides)
        return FakeProc([])

    monkeypatch.setattr("companion.skills.agent.CodexAppServerBackend.from_codex", fake_from_codex)
    agent._launch("subscription")
    assert captured["workdir"] == tmp_path / "run" / "agent-cwd"
    fs = captured["overrides"]["permissions.companion_agent.filesystem"]
    assert f'"{module.toml_path(agent.workspace)}" = "write"' in fs
    assert fs.count('"write"') == 1  # 工作目录只读：沙箱不能往里写配置


def test_cwd_is_emptied_before_each_task(tmp_path):
    agent, _ = make_agent(tmp_path, [FakeProc(done())])
    planted = agent.cwd / ".codex" / "config.toml"
    planted.parent.mkdir(parents=True)
    planted.write_text("planted", "utf-8")
    assert agent.run("任务", "x", OWNER_CTX).later().ok
    assert list(agent.cwd.iterdir()) == []


def test_task_fails_when_cwd_cannot_be_emptied(tmp_path, monkeypatch):
    agent, launched = make_agent(tmp_path, [])
    (agent.cwd).mkdir(parents=True)
    (agent.cwd / "AGENTS.md").write_text("planted", "utf-8")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    result = agent.run("任务", "x", OWNER_CTX).later()
    assert not result.ok and launched == []
    assert not agent._busy  # 出错也要能接下一个任务


@pytest.mark.parametrize("workspace", [".", "data"])
def test_refuses_workspace_that_contains_the_cwd(tmp_path, workspace):
    """工作目录的上级也在 Codex 找配置的范围内：沙箱能写 data/.codex 就又能逃出去。"""
    fb = FallbackConfig()
    launched = []
    agent = CodexAgent(
        AgentSkillConfig(workspace=Path(workspace)),
        tmp_path,
        SubscriptionConfig(),
        fb,
        tmp_path / "data" / "run",
        quota_ok=lambda: True,
        launcher=lambda backend: launched.append(backend),
    )
    with pytest.raises(SkillError, match="设得太大"):
        agent.run("任务", "x", OWNER_CTX)
    assert launched == [] and not agent._busy


def test_moves_aside_only_what_codex_cannot_manage(tmp_path, monkeypatch):
    for name in (".codex", ".git", ".agents", "sub/.git"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text("留着看", "utf-8")
    monkeypatch.setattr(module, "_can_edit_acl", lambda path: path.name != ".git")
    moved = move_aside_unmanageable(tmp_path)
    assert len(moved) == 1 and moved[0].startswith(".git-留下的-")
    assert (tmp_path / moved[0] / "config").read_text("utf-8") == "留着看"  # 只改名不删
    assert not (tmp_path / ".git").exists()
    assert (tmp_path / ".codex").is_dir() and (tmp_path / ".agents").is_dir()
    assert (tmp_path / "sub" / ".git").is_dir()
    assert move_aside_unmanageable(tmp_path) == []


def test_move_aside_tolerates_failure(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr(module, "_can_edit_acl", lambda path: False)

    def refuse(self, target):
        raise PermissionError

    monkeypatch.setattr(Path, "rename", refuse)
    assert move_aside_unmanageable(tmp_path) == []
    assert (tmp_path / ".codex").is_dir()


def test_own_folders_are_left_alone(tmp_path):
    """自己建的目录改得了 ACL，不用挪；不存在的路径当作改不了。"""
    (tmp_path / ".git").mkdir()
    assert move_aside_unmanageable(tmp_path) == []
    assert (tmp_path / ".git").is_dir()
    if module._WINDOWS:
        assert not module._can_edit_acl(tmp_path / "missing")


def test_owner_is_told_when_something_was_moved_aside(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path, [FakeProc(done())])
    (agent.workspace / ".codex").mkdir(parents=True)
    monkeypatch.setattr(module, "_can_edit_acl", lambda path: False)
    out = agent.run("任务", "x", OWNER_CTX).later()
    assert out.ok and ".codex-留下的-" in out.text and "告诉主人" in out.text
    plain, _ = make_agent(tmp_path, [FakeProc(done())])
    assert "留下的" not in plain.run("任务", "x", OWNER_CTX).later().text


# ---------- QQ 全流程 ----------


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        return {"message_id": 9000 + len(self.sent)}

    def texts(self):
        return [
            "".join(s["data"].get("text", "") for s in p["message"] if s["type"] == "text")
            for a, p in self.sent
            if a.startswith("send_")
        ]


def msg(text, user=OWNER, mid=1):
    return {
        "post_type": "message",
        "self_id": int(BOT),
        "user_id": int(user),
        "message_id": mid,
        "message_type": "private",
        "sender": {"nickname": "主人"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_qq_owner_task_with_approval(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    replies = iter(["好【任务：把桌面的表格汇总】", "我去弄一下", "弄好啦，汇总表在工作文件夹里"])
    gateway, chat = scripted_gateway(lambda m, j: next(replies))
    agent, _ = make_agent(tmp_path, [FakeProc([approval(), "WAIT", *done()])])
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder(), skills=[agent])
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], debounce_private_seconds=0)
    bot = QQBot(
        c,
        cfg,
        client=FakeClient(),
        rng=random.Random(1),
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    async def go():
        await bot.handle_event(msg("帮我把桌面的表格汇总一下"))
        for _ in range(200):  # 等审批请求发到主人这里
            await asyncio.sleep(0.02)
            if any("/批准" in t for t in bot.client.texts()):
                break
        question = next(t for t in bot.client.texts() if "/批准" in t)
        code = question.split("/批准 ")[1].split()[0]
        await bot.handle_event(msg(f"/批准 {code}", mid=2))
        await bot.drain()

    try:
        asyncio.run(go())
        texts = bot.client.texts()
        assert texts[:2] == ["好", "我去弄一下"]
        assert "New-Item C:/outside/x.txt" in texts[2]
        assert texts[3] == "好，批准了。"
        assert texts[-1] == "弄好啦，汇总表在工作文件夹里"
        assert "汇总.xlsx" in chat.requests[-1][-1].content  # 结果交给她转述
        mine = [e.content for e in c.log.iter_events() if e.actor.person_id == "self"]
        assert mine[-1] == "弄好啦，汇总表在工作文件夹里"
    finally:
        bot.close()
        c.close()


def test_qq_friend_cannot_use_agent_or_approve(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    gateway, chat = scripted_gateway(lambda m, j: "不行哦【任务：删库】")
    agent, launched = make_agent(tmp_path, [])
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder(), skills=[agent])
    bot = QQBot(
        c,
        QQConfig(bot_account=BOT, owner_accounts=[OWNER], debounce_private_seconds=0),
        client=FakeClient(),
        rng=random.Random(1),
    )

    async def go():
        await bot.handle_event(msg("帮我删库", user=FRIEND))
        await bot.handle_event(msg("/批准 1234", user=FRIEND, mid=2))
        await bot.drain()

    try:
        asyncio.run(go())
        assert launched == [] and "【任务" not in chat.requests[0][0].content
        assert bot.client.texts() == ["不行哦"]  # /批准 对非主人无效，不回应
    finally:
        bot.close()
        c.close()


def test_workspace_relative_to_root(tmp_path):
    agent, _ = make_agent(tmp_path, [])
    agent.cfg.workspace = Path("data/workspace")
    assert agent.workspace == tmp_path / "data" / "workspace"
