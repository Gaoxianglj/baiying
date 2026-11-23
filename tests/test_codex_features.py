import asyncio
import os
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from companion.config import FilesSkillConfig, SkillsConfig, load_config
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.openai_compat import ModelError
from companion.models.runtime import RunControl
from companion.skills.access import FileAccess
from companion.skills.agent import AgentRun
from companion.skills.background import run_background
from companion.skills.base import SkillError, Skills
from companion.skills.documents import Documents
from companion.skills.native import NativeSkills
from companion.skills.reader import ExportText, ReadFile
from companion.skills.tasks import TaskBook, TaskControl
from tests.fakes import scripted_gateway
from tests.test_agent import FakeProc, done
from tests.test_qq import bot  # noqa: F401 -- fixture
from tests.test_reader import pdf_bytes
from tests.test_skills import Echo
from tests.test_tasks import GROUP, Step

NOW = datetime(2026, 11, 10, 14, tzinfo=UTC)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_embedding_ignores_unreachable_system_proxy(monkeypatch, host):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from companion.models.openai_compat import OpenAICompatClient

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"data":[{"index":0,"embedding":[1.0,0.0]}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.delenv(name.lower(), raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    client = OpenAICompatClient(name="embedding", model="test",
                                base_url=f"http://{host}:{server.server_port}/v1", timeout=2)
    try:
        assert client.embed(["检索连接验证"]).tolist() == [[1.0, 0.0]]
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def native(tmp_path, mode):
    return CodexAppServerBackend(
        command=[sys.executable, str(Path(__file__).with_name("fake_codex_runtime.py"))],
        workdir=tmp_path, env={**os.environ, "RUNTIME_MODE": mode}, timeout=5,
    )


def test_structured_rpc_deduplicates_callbacks_and_observes_actual_events(tmp_path):
    from companion.models.openai_compat import ChatMessage

    echo = Echo()
    calls = []
    bridge = NativeSkills(Skills([echo], SkillsConfig()), replace(GROUP),
                          lambda call, result: calls.append(call))
    events = []
    b = native(tmp_path, "tool")
    try:
        result = b.chat([ChatMessage("system", "测试"), ChatMessage("user", "调用工具")],
                        tools=bridge.tools, observer=lambda m, p: events.append(m))
        assert echo.calls == ["hello"] and len(calls) == 1
        assert "hello" in result.text
        assert "item/started" in events and "turn/completed" in events
    finally:
        b.close()


def test_wrong_thread_rpc_cannot_execute_skill(tmp_path):
    from companion.models.openai_compat import ChatMessage

    echo = Echo()
    bridge = NativeSkills(Skills([echo], SkillsConfig()), replace(GROUP), lambda *_: None)
    b = native(tmp_path, "tool-foreign")
    try:
        r = b.chat([ChatMessage("system", "测试")], tools=bridge.tools)
        assert not echo.calls and "拒绝执行" in r.text
    finally:
        b.close()


def test_native_validation_rechecks_current_permission_and_parameter_types():
    from tests.test_skills import OwnerOnly

    ctx = replace(GROUP, private=True, owner=True)
    bridge = NativeSkills(Skills([OwnerOnly()], SkillsConfig()), ctx, lambda *_: None)
    name = bridge.tools.specs[0]["name"]
    ctx.owner = False
    assert bridge.call(name, {"arg": "a", "content": ""}, "x")["success"] is False
    assert bridge.call(name, {"arg": {"bad": 1}}, "y")["success"] is False


def test_mid_turn_steer_and_acknowledgement(tmp_path):
    from companion.models.openai_compat import ChatMessage

    receipts = []
    item = {"id": "extra1", "text": "改成蓝色"}
    control = RunControl(threading.Event(), lambda: [item],
                         lambda *args: receipts.append(args))
    b = native(tmp_path, "steer")
    try:
        r = b.chat([ChatMessage("system", "测试")], control=control)
        assert "改成蓝色" in r.text and receipts == [("extra1", True, "运行中的原任务已接收")]
    finally:
        b.close()


def test_interrupt_does_not_wait_for_normal_response_timeout(tmp_path):
    from companion.models.openai_compat import ChatMessage

    stop = threading.Event()
    stop.set()
    b = native(tmp_path, "interrupt")
    try:
        with pytest.raises(ModelError, match="interrupted"):
            b.chat([ChatMessage("system", "测试")], control=RunControl(stop))
    finally:
        b.close()


def test_task_state_waiting_phase_and_scoped_append(tmp_path):
    book = TaskBook(tmp_path / "tasks.json")
    tid = book.create("background_tasks", "任务", "整理", GROUP)
    book.update(tid, state="running")
    book.runtime(tid, "turn/started", {"threadId": "thread", "turn": {"id": "turn"}})
    book.runtime(tid, "thread/status/changed", {"status": {
        "type": "active", "activeFlags": ["waitingOnApproval"]}})
    assert "等待批准" in book.summary(GROUP)
    book.append_input(tid, "再做目录", GROUP)
    assert book.pending_inputs(tid)[0]["text"] == "再做目录"
    with pytest.raises(SkillError):
        book.append_input(tid, "不该注入", replace(GROUP, channel="group:other"))


def test_background_checkpoint_recovers_completed_steps(tmp_path):
    book = TaskBook(tmp_path / "tasks.json")
    cfg = load_config(tmp_path, environ={})
    tid = book.create("background_tasks", "任务", "两步", GROUP)
    ctx = replace(GROUP, task_id=tid, stop_event=book.event(tid))
    step = Step()
    first = True

    def handler(m, j):
        nonlocal first
        if first:
            first = False
            return "【步骤：1】"
        raise ModelError("模拟断线")

    gateway, _ = scripted_gateway(handler)
    result = book.execute(tid, lambda: run_background(
        "两步", ctx, cfg, book, lambda: gateway, lambda g: [step]))
    assert not result.ok and step.calls == ["1"]
    old = book.get(tid, GROUP)["checkpoint"]
    again = book.create("background_tasks", "任务", "两步", GROUP, resume_of=tid)
    ctx2 = replace(GROUP, task_id=again, stop_event=book.event(again))
    replies = iter(["【步骤：2】", "【任务完成：两步完成】"])
    gw2, chat = scripted_gateway(lambda m, j: next(replies))
    result = book.execute(again, lambda: run_background(
        "两步", ctx2, cfg, book, lambda: gw2, lambda g: [step]))
    assert result.ok and step.calls == ["1", "2"]
    assert any("第 1 步已完成" in m.content for m in chat.requests[0])
    assert book.get(again, GROUP)["checkpoint"]["session_key"] == old["session_key"]


def test_native_agent_resumes_same_thread_and_rechecks_policy(tmp_path):
    class ResumeProc(FakeProc):
        def _request(self, method, params):
            self.sent.append({"method": method, "params": params})
            return ({"thread": {"id": "saved-thread"}} if method.startswith("thread/")
                    else {"turn": {"id": "saved-turn"}})

    book = TaskBook(tmp_path / "tasks.json")
    ctx = replace(GROUP, private=True, owner=True, task_book=book)
    tid = book.create("agent", "任务", "整理", ctx)
    ctx.task_id = tid
    p1 = ResumeProc(done())
    args = dict(workspace=tmp_path, effort="low", ask=None, progress=None,
                timeout=60, progress_every=60, context=ctx)
    assert AgentRun(p1, **args).run("整理").ok
    assert p1.sent[0]["params"]["ephemeral"] is False
    p2 = ResumeProc(done())
    assert AgentRun(p2, **args).run("继续").ok
    assert p2.sent[0]["method"] == "thread/resume"
    assert p2.sent[0]["params"]["threadId"] == "saved-thread"
    with pytest.raises(ModelError, match="权限或模型配置已改变"):
        AgentRun(ResumeProc(done()), **{**args, "workspace": tmp_path / "changed"}).run("继续")


def image_pdf():
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 80), "SCAN TOKEN 87213", fill="black", font_size=36)
    draw.line((100, 700, 650, 700), fill="black", width=4)
    draw.rectangle((150, 450, 250, 700), fill="blue")
    draw.text((150, 720), "A=25", fill="black", font_size=30)
    out = BytesIO()
    image.save(out, "PDF", resolution=100)
    return out.getvalue()


def test_mixed_pdf_page_numbers_and_ocr_cache(tmp_path):
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(pdf_bytes())))
    writer.append(PdfReader(BytesIO(image_pdf())))
    out = BytesIO()
    writer.write(out)
    calls = []
    docs = Documents(tmp_path / "cache",
                     lambda path, number: calls.append(number) or "扫描书签 87213")
    text = docs.extract(out.getvalue(), replace(GROUP))
    assert "PDF 第 1 页" in text and "PDF 第 2 页" in text and "87213" in text
    assert calls == [2]
    assert docs.extract(out.getvalue(), replace(GROUP)) == text and calls == [2]
    assert "87213" in docs.searchable_pages(out.getvalue())[1]
    _, images = docs.read_pages(out.getvalue(), 2, 2, replace(GROUP))
    assert images[0].is_file()
    with Image.open(images[0]) as image:
        assert max(image.size) <= 2000


def test_scanned_export_is_deferred_and_cannot_bypass_chat_file_boundary(tmp_path):
    cfg = FilesSkillConfig()
    access = FileAccess(cfg, tmp_path, tmp_path / "ws", lambda: [])
    docs = Documents(tmp_path / "cache", lambda p, n: "扫描文字 87213")
    reader = ReadFile(cfg, access, docs)
    exporter = ExportText(reader)
    source = access.save_new("扫描.pdf", image_pdf(), replace(GROUP))
    book = TaskBook(tmp_path / "tasks.json")
    skills = Skills([exporter], SkillsConfig(), tasks=book)
    call, result = skills.run(exporter, "转存文件", f"{source}｜全文.md", replace(GROUP))
    assert result.later and call.task_id
    done = result.later()
    assert done.ok and "87213" in done.files[0].read_text("utf-8")
    with pytest.raises(SkillError):
        reader.run("读页", f"{source}｜1", replace(GROUP, channel="group:other"))


def test_pdf_cache_does_not_authorize_cross_chat_and_page_limit(tmp_path):
    cfg = FilesSkillConfig(pdf_batch_pages=1)
    access = FileAccess(cfg, tmp_path, tmp_path / "ws", lambda: [])
    docs = Documents(tmp_path / "cache", lambda p, n: "文字")
    reader = ReadFile(cfg, access, docs)
    source = access.save_new("扫描.pdf", image_pdf(), replace(GROUP))
    assert reader.run("读页", f"{source}｜1", replace(GROUP)).view
    with pytest.raises(SkillError, match="最多看 1 页"):
        reader.run("读页", f"{source}｜1-2", replace(GROUP))
    with pytest.raises(SkillError):
        reader.run("查文档", f"{source}｜文字", replace(GROUP, channel="group:other"))


def test_task_file_writes_reuse_verified_artifact_after_resume(tmp_path):
    access = FileAccess(FilesSkillConfig(), tmp_path, tmp_path / "ws", lambda: [])
    book = TaskBook(tmp_path / "tasks.json")
    tid = book.create("background_tasks", "任务", "写文件", GROUP)
    ctx = replace(GROUP, task_id=tid, task_book=book)
    first = access.save_new("目录.md", b"same-content", ctx)
    second = access.save_new("目录.md", b"same-content", ctx)
    assert second == first
    book.update(tid, state="interrupted")
    resume = book.create("background_tasks", "任务", "写文件", GROUP, resume_of=tid)
    assert access.save_new("目录.md", b"same-content", replace(ctx, task_id=resume)) == first
    first.write_bytes(b"external-change")
    with pytest.raises(SkillError, match="已改变"):
        access.save_new("目录.md", b"same-content", replace(ctx, task_id=resume))


def test_task_delivery_receipts_prevent_duplicate_text_and_files(bot, tmp_path):  # noqa: F811
    from companion.bodies.qq.bot import Incoming, Source

    ctx = replace(GROUP)
    tid = bot.c.tasks.create("background_tasks", "任务", "发文件", ctx)
    sender = bot.c._file_sender(ctx)
    path = sender.access.save_new("产出.txt", b"done", ctx)
    bot.c.tasks.update(tid, state="completed", files=[str(path)], result="好了")
    source = Source("group", "", "4000", "", None, Incoming(text=""))
    asyncio.run(bot._send_task_result(source, tid, "任务完成", [path]))
    asyncio.run(bot._send_task_result(source, tid, "任务完成", [path]))
    assert len([a for a, _ in bot.client.sent if a == "send_group_msg"]) == 1
    assert len([a for a, _ in bot.client.sent if a == "upload_group_file"]) == 1


def test_uncertain_delivery_survives_restart_without_blind_resend(tmp_path):
    book = TaskBook(tmp_path / "tasks.json")
    tid = book.create("background_tasks", "任务", "发文件", GROUP)
    book.update(tid, state="completed")
    book.delivery_part(tid, "text", "sending")
    book.interrupt_active()
    assert book.delivery_part(tid, "text") == "unknown"
    assert book.pending_notifications() == []
    assert "发送未获确认" in book.summary(GROUP)


def test_scanned_export_can_stop_before_writing_output(tmp_path):
    access = FileAccess(FilesSkillConfig(), tmp_path, tmp_path / "ws", lambda: [])
    docs = Documents(tmp_path / "cache", lambda p, n: "识别文字")
    exporter = ExportText(ReadFile(FilesSkillConfig(), access, docs))
    source = access.save_new("扫描.pdf", image_pdf(), replace(GROUP))
    book = TaskBook(tmp_path / "tasks.json")
    call, result = Skills([exporter], SkillsConfig(), tasks=book).run(
        exporter, "转存文件", f"{source}｜全文.md", replace(GROUP))
    book.stop(call.task_id, replace(GROUP))
    assert not result.later().ok
    assert not (source.parent / "全文.md").exists()
    assert book.get(call.task_id, GROUP)["state"] == "cancelled"


def test_group_shell_selection_changes_only_child_environment(tmp_path, monkeypatch):
    from companion.config import GroupAgentConfig, SubscriptionConfig
    from companion.skills.group_agent import GroupAgent

    bundled = tmp_path / "bundled-powershell"
    bundled.mkdir()
    (bundled / "pwsh.exe").write_bytes(b"fake executable")
    original_path = str(bundled) + os.pathsep + os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", original_path)
    monkeypatch.setenv("PSModulePath", "private-modules")
    monkeypatch.setattr("companion.skills.group_agent.stuck_mcp_servers", lambda home: [])
    monkeypatch.setattr(CodexAppServerBackend, "from_codex", lambda *a, **kw: kw)
    access = FileAccess(FilesSkillConfig(), tmp_path, tmp_path / "ws", lambda: [])
    agent = GroupAgent(GroupAgentConfig(), tmp_path, SubscriptionConfig(), access,
                       lambda: [], tmp_path / "run", quota_ok=lambda: True)
    launched = agent._launch(tmp_path / "ws", tmp_path / "cwd")
    child_env = launched["env"]
    assert str(bundled) not in child_env["PATH"].split(os.pathsep)
    assert not any(key.lower() == "psmodulepath" for key in child_env)
    assert os.environ["PATH"] == original_path
    assert os.environ["PSModulePath"] == "private-modules"


def test_resume_requires_no_other_active_task(tmp_path):
    book = TaskBook(tmp_path / "tasks.json")
    tid = book.create("background_tasks", "任务", "第一个", GROUP)
    book.update(tid, state="interrupted")
    book.create("background_tasks", "任务", "第二个", GROUP)
    control = TaskControl(book, lambda *a: None, lambda *a: None)
    with pytest.raises(SkillError, match="已有运行中的任务"):
        control.run("继续任务", tid, replace(GROUP))


def test_old_cli_can_initialize_without_experimental_capability(tmp_path, monkeypatch):
    from companion.models.openai_compat import ChatMessage
    from tests.test_models import codex

    b = codex(tmp_path, monkeypatch, FAKE_REJECT_EXPERIMENTAL="1")
    try:
        assert b.chat([ChatMessage("system", "普通聊天")]).text == "你好呀"
        assert not b.native_tools_supported
    finally:
        b.close()


def test_stop_prevents_gateway_from_starting_fallback_call(tmp_path):
    from companion.models.gateway import ModelGateway
    from companion.models.openai_compat import ChatMessage
    from tests.test_models import deepseek
    from tests.test_proactive import FakeSubscription

    stop = threading.Event()
    calls = []

    def interrupted(messages):
        stop.set()
        raise ModelError("原生调用中断")

    gw = ModelGateway(subscription=FakeSubscription(interrupted), fallback=deepseek(calls=calls))
    try:
        with pytest.raises(ModelError, match="已停止"):
            gw.chat([ChatMessage("system", "测试")], control=RunControl(stop))
        assert calls == []
    finally:
        gw.close()


@pytest.mark.parametrize("declared,artifact,expected", [
    (False, "real.txt", False), (True, "missing.txt", False), (True, "real.txt", True),
])
def test_native_engine_completion_is_not_automatically_task_success(
    tmp_path, declared, artifact, expected
):
    import json

    (tmp_path / "real.txt").write_text("done", "utf-8")
    env = {**os.environ, "RUNTIME_MODE": "agent-result", "RUNTIME_RESULT": json.dumps({
        "completed": declared, "summary": "实际结果", "artifacts": [artifact],
    })}
    b = CodexAppServerBackend(
        command=[sys.executable, str(Path(__file__).with_name("fake_codex_runtime.py"))],
        workdir=tmp_path, env=env, timeout=5,
    )
    try:
        result = AgentRun(b, workspace=tmp_path, effort="low", ask=None, progress=None,
                          timeout=10, progress_every=10, structured_result=True).run("测试")
        assert result.ok is expected
    finally:
        b.close()

def test_private_codex_home_is_passed_only_to_child(tmp_path, monkeypatch):
    personal = tmp_path / "personal"
    isolated = tmp_path / "isolated"
    personal.mkdir()
    isolated.mkdir()
    (personal / "config.toml").write_text(
        '[mcp_servers.personal_only]\ncommand = "codex"\n', encoding="utf-8",
    )
    (isolated / "config.toml").write_text(
        '[mcp_servers.baiying_only]\ncommand = "codex"\n', encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(personal))
    monkeypatch.setattr("companion.models.codex_app_server.shutil.which", lambda _: "codex.exe")
    inherited = {"CODEX_HOME": str(personal), "CODEX_SQLITE_HOME": str(personal)}
    backend = CodexAppServerBackend.from_codex(
        "codex", tmp_path / "work", codex_home=isolated, env=inherited,
    )

    assert backend._env == {"CODEX_HOME": str(isolated), "CODEX_SQLITE_HOME": str(isolated)}
    assert inherited == {"CODEX_HOME": str(personal), "CODEX_SQLITE_HOME": str(personal)}
    assert os.environ["CODEX_HOME"] == str(personal)
    assert "mcp_servers.baiying_only.enabled=false" in backend._command
    assert "mcp_servers.personal_only.enabled=false" not in backend._command
