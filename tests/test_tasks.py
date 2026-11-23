"""真实任务状态、权限隔离、续跑、异常与取消；不调用外部模型。"""

import asyncio
import threading
from dataclasses import replace

import pytest

from companion.config import load_config
from companion.skills.background import run_background
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult, Skills
from companion.skills.tasks import TaskBook, TaskControl
from tests.fakes import scripted_gateway
from tests.test_qq import bot as bot
from tests.test_qq import msg, run

GROUP = SkillContext("qq", "group:4000", "f", private=False, owner=False)
OTHER = replace(GROUP, channel="group:4001")
PRIVATE = replace(GROUP, channel="private:3000", private=True)
OWNER = replace(PRIVATE, channel="private:2000", owner=True)


@pytest.fixture
def book(tmp_path):
    return TaskBook(tmp_path / "run" / "tasks.json")


def test_real_lifecycle_persists_and_query_is_channel_scoped(book):
    task_id = book.create("background_tasks", "任务", "完整转换私密资料", PRIVATE)
    assert book.get(task_id, PRIVATE)["state"] == "queued"
    for ctx in [GROUP, OTHER, replace(GROUP, owner=True)]:
        assert "私密资料" not in book.summary(ctx)
        with pytest.raises(SkillError):
            book.get(task_id, ctx)
    assert "完整转换私密资料" in book.summary(OWNER)

    def work():
        assert book.get(task_id, PRIVATE)["state"] == "running"
        book.progress(task_id, "全文已经保存", step=True)
        return SkillResult("已落盘，待检索", report=True)

    result = book.execute(task_id, work)
    fresh = TaskBook(book.path)
    assert result.task_id == task_id
    assert fresh.get(task_id, PRIVATE)["state"] == "completed"
    assert fresh.get(task_id, PRIVATE)["steps"] == 1
    assert "已落盘" in fresh.summary(PRIVATE)


def test_runtime_context_is_short_and_private_task_cannot_be_replayed(book):
    from companion.trpg.policy import protect
    tid = book.create("background_tasks", "任务", "KP-SECRET" * 500, GROUP)
    book.execute(tid, lambda: SkillResult("KP-SECRET" * 500))
    control = TaskControl(book, None, None)
    context = control.context(GROUP)
    assert tid in context and "KP-SECRET" not in context and len(context) < 500
    protect(book.path.parent / "trpg-protected.json", tasks=[tid])
    assert "KP-SECRET" not in book.summary(OWNER, tid)
    with pytest.raises(SkillError, match="历史任务包含KP资料"):
        control.run("继续任务", tid, GROUP)


def test_errors_are_failed_and_restart_marks_only_active_interrupted(book):
    done = book.create("x", "x", "已完成的", GROUP)
    book.execute(done, lambda: SkillResult("好了"))
    failed = book.create("x", "x", "出错的", GROUP)

    def broken():
        raise RuntimeError("boom")

    assert not book.execute(failed, broken).ok
    assert book.get(failed, GROUP)["state"] == "failed"
    queued = book.create("x", "x", "排队的", GROUP)
    assert TaskBook(book.path).get(queued, GROUP)["state"] == "queued"  # 网页读取不修改它
    book.interrupt_active()
    assert book.get(queued, GROUP)["state"] == "interrupted"
    assert book.get(done, GROUP)["state"] == "completed"
    assert "可继续" in book.get(queued, GROUP)["progress"]


def test_corrupt_state_is_not_silently_replaced_with_no_tasks(book):
    book.path.parent.mkdir(parents=True)
    book.path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        book.summary(GROUP)


def test_background_submission_resume_and_no_duplicate_registration(book):
    requests = []

    def worker(task, ctx):
        requests.append((task, ctx.channel))
        assert ctx.task_id and ctx.stop_event is not None
        return SkillResult("文件已生成", report=True)

    control = TaskControl(book, worker, lambda row, ctx: control.run("任务", row["arg"], ctx))
    skills = Skills([control], load_config(book.path.parent, environ={}).skills, tasks=book)
    call, result = skills.run(control, "任务", "整理规则", GROUP)
    assert call.task_id in result.text
    assert call.task_id in control.context(GROUP)
    assert book.get(call.task_id, GROUP)["state"] == "queued"
    with pytest.raises(SkillError, match="已有后台任务"):
        control.run("任务", "重复", GROUP)
    first = call.later()
    assert first.ok
    again, _ = skills.run(control, "继续任务", call.task_id, GROUP)
    assert again.task_id != call.task_id
    assert book.get(again.task_id, GROUP)["resume_of"] == call.task_id
    assert again.later().ok
    assert requests == [("整理规则", GROUP.channel), ("整理规则", GROUP.channel)]
    with pytest.raises(SkillError, match="原来的聊天"):
        control.run("继续任务", call.task_id, OWNER)


class Step(Skill):
    name, label = "read_files", "读文件"
    markers = {"步骤": "【步骤：编号】"}

    def __init__(self):
        self.calls = []

    def run(self, marker, arg, ctx):
        self.calls.append(arg)
        return SkillResult("第 " + arg + " 步已完成")


def test_background_runs_beyond_old_limit_and_explicitly_finishes(book, tmp_path):
    config = load_config(tmp_path, environ={})
    replies = iter([f"【步骤：{n}】" for n in range(16)]
                   + ["我回头继续", "【任务完成：16 步全部完成】"])
    gateway, chat = scripted_gateway(lambda m, j: next(replies))
    step = Step()
    task_id = book.create("background_tasks", "任务", "做完16步", GROUP)
    ctx = replace(GROUP, task_id=task_id, stop_event=book.event(task_id))
    result = book.execute(task_id, lambda: run_background(
        "做完16步", ctx, config, book, lambda: gateway, lambda g: [step]))
    assert result.ok and len(step.calls) == 16
    assert len(chat.requests) == 18
    assert book.get(task_id, GROUP)["steps"] == 16
    assert "后台任务仍未结束" in chat.requests[-1][-1].content


def test_cancel_during_model_call_prevents_next_write(book, tmp_path):
    config = load_config(tmp_path, environ={})
    task_id = book.create("background_tasks", "任务", "别继续写", GROUP)
    ctx = replace(GROUP, task_id=task_id, stop_event=book.event(task_id))
    step = Step()

    def brain(m, j):
        book.stop(task_id, GROUP)
        return "【步骤：不该执行】"

    gateway, _ = scripted_gateway(brain)
    result = book.execute(task_id, lambda: run_background(
        "别继续写", ctx, config, book, lambda: gateway, lambda g: [step]))
    assert not result.ok and not step.calls
    assert book.get(task_id, GROUP)["state"] == "cancelled"


def test_continue_never_reuses_privileges_from_old_request(book):
    task_id = book.create("agent", "任务", "只能主人做", OWNER)
    book.update(task_id, state="interrupted")
    control = TaskControl(book, lambda *a: None, lambda *a: pytest.fail("不应恢复"))
    for ctx in [PRIVATE, GROUP, OTHER]:
        with pytest.raises(SkillError):
            control.run("继续任务", task_id, ctx)


def test_qq_task_status_is_direct_and_completion_targets_original_group(bot):
    book = bot.c.tasks
    task_id = book.create("background_tasks", "任务", "整理规则", GROUP)
    run(bot, msg(f"/任务状态 {task_id}", group="4000"))
    assert "排队中" in bot.client.texts("send_group_msg")[-1]
    run(bot, msg(f"/任务状态 {task_id}", group="4001", mid=2))
    assert all(p.get("group_id") != 4001 for _, p in bot.client.sent)
    book.update(task_id, state="completed", result="产出已保存")

    async def notify():
        task = asyncio.create_task(bot._task_notification_loop(0.001))
        await asyncio.sleep(0.015)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(notify())
    assert book.get(task_id, GROUP)["notified"]
    assert "产出已保存" in bot.client.texts("send_group_msg")[-1]
    assert not any(a == "send_private_msg" for a, _ in bot.client.sent)


def test_background_worker_does_not_block_main_chat(book):
    entered, release = threading.Event(), threading.Event()
    task_id = book.create("x", "x", "耗时任务", GROUP)

    def worker():
        entered.set()
        release.wait(2)
        return SkillResult("完成")

    async def scenario():
        pending = asyncio.create_task(asyncio.to_thread(book.execute, task_id, worker))
        await asyncio.to_thread(entered.wait, 1)
        assert "运行中" in book.summary(GROUP)
        assert await asyncio.wait_for(asyncio.sleep(0, result="聊天正常"), 0.2) == "聊天正常"
        release.set()
        assert (await pending).ok

    asyncio.run(scenario())


def test_queued_cancellation_never_executes_work(book):
    task_id = book.create("background_tasks", "任务", "不能写的任务", GROUP)
    book.stop(task_id, GROUP)
    result = book.execute(task_id, lambda: pytest.fail("取消排队任务后不能开始工作"))
    assert not result.ok and book.get(task_id, GROUP)["state"] == "cancelled"


def test_qq_can_chat_and_query_while_group_background_is_working(bot):
    entered, release = threading.Event(), threading.Event()
    replies = iter(["好【任务：整理规则】", "任务已登记", "聊天正常", "结果已经保存"])
    bot.c.gateway.fallback.client.handler = lambda m, j: next(replies)

    def background(m, j):
        entered.set()
        release.wait(3)
        return "【任务完成：完成整理】"

    gateway, _ = scripted_gateway(background)
    bot.c.background_gateway_factory = lambda: gateway

    async def scenario():
        try:
            await bot.handle_event(msg("请做任务", group="4000", at_bot=True))
            assert await asyncio.to_thread(entered.wait, 2)
            await bot.handle_event(msg("现在聊聊天", group="4000", at_bot=True, mid=2))
            for _ in range(100):
                if "聊天正常" in bot.client.texts("send_group_msg"):
                    break
                await asyncio.sleep(0.01)
            assert "聊天正常" in bot.client.texts("send_group_msg")
            await bot.handle_event(msg("/任务状态", group="4000", mid=3))
            assert "运行中" in bot.client.texts("send_group_msg")[-1]
        finally:
            release.set()
        await bot.drain()
        assert "已完成" in bot.client.texts("send_group_msg")[-1]

    asyncio.run(scenario())
    assert bot.c.tasks.visible(GROUP)[-1]["notified"]
