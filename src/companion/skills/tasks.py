"""持久任务状态与后台技能任务；查询只显示当前聊天，主人私聊可查看全部。"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

log = logging.getLogger(__name__)
ACTIVE = {"queued", "running", "cancelling"}
LABELS = {"queued": "排队中", "running": "运行中", "completed": "已完成",
          "failed": "失败", "cancelled": "已停止", "cancelling": "正在停止",
          "interrupted": "已中断（没有继续运行）"}


def now() -> str:
    return datetime.now(UTC).isoformat()


class TaskBook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._closers: dict[str, Callable[[], None]] = {}

    def on_cancel(self, task_id: str, close: Callable[[], None] | None) -> None:
        with self._lock:
            if close is None:
                self._closers.pop(task_id, None)
            else:
                self._closers[task_id] = close

    def pending_notifications(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._load().values()
                    if r["state"] not in ACTIVE and not r["notified"] and r["body"] == "qq"
                    and not any(s in {"sending", "unknown", "blocked"}
                                for s in r.get("delivery", {}).values())]

    def delivery_part(self, task_id: str, part: str, state: str | None = None) -> str | None:
        with self._lock:
            rows = self._load()
            delivery = rows[task_id].setdefault("delivery", {})
            if state is not None:
                delivery[part] = state
                self._save(rows)
            return delivery.get(part)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        # 损坏时明确报错，不能把真实任务伪装成“没有任务”。
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data["tasks"]

    def _save(self, rows: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temp.write_text(json.dumps({"version": 1, "tasks": rows}, ensure_ascii=False),
                        encoding="utf-8")
        temp.replace(self.path)

    def create(self, skill: str, marker: str, arg: str, ctx: SkillContext,
               *, resume_of: str = "") -> str:
        with self._lock:
            rows = self._load()
            if resume_of:
                old = rows[resume_of]
                skill, marker, arg = old["skill"], old["marker"], old["arg"]
            task_id = uuid4().hex[:10]
            rows[task_id] = {
                "id": task_id, "skill": skill, "marker": marker, "arg": arg,
                "channel": ctx.channel, "body": ctx.body, "person_id": ctx.person_id,
                "private": ctx.private, "owner": ctx.owner, "state": "queued",
                "created_at": now(), "updated_at": now(), "progress": "等待工作线程启动",
                "steps": 0, "result": "", "files": [], "resume_of": resume_of,
                "notified": False,
                "checkpoint": dict(old.get("checkpoint", {})) if resume_of else {},
                "runtime": {},
                "inputs": [dict(i, status="queued") for i in old.get("inputs", [])
                           if i.get("status") != "rejected"] if resume_of else [],
            }
            self._events[task_id] = threading.Event()
            self._save(rows)
            return task_id

    def event(self, task_id: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(task_id, threading.Event())

    def visible(self, ctx: SkillContext) -> list[dict]:
        with self._lock:
            rows = self._load().values()
            return sorted((dict(r) for r in rows if r["channel"] == ctx.channel
                           or (ctx.owner and ctx.private)), key=lambda r: r["created_at"])

    def get(self, task_id: str, ctx: SkillContext) -> dict:
        candidates = self.visible(ctx)
        if task_id.strip():
            candidates = [r for r in candidates if r["id"] == task_id.strip()]
        if not candidates:
            raise SkillError("当前聊天没有这个任务")
        if not task_id.strip():
            active = [r for r in candidates if r["state"] in ACTIVE]
            if active:
                return active[-1]
        return candidates[-1]

    def update(self, task_id: str, **changes: object) -> None:
        with self._lock:
            rows = self._load()
            rows[task_id].update(changes, updated_at=now())
            self._save(rows)

    def progress(self, task_id: str, text: str, *, step: bool = False) -> None:
        with self._lock:
            rows = self._load()
            row = rows[task_id]
            if row["state"] not in ACTIVE:
                return
            row.update(progress=text[:400], updated_at=now())
            if step:
                row["steps"] += 1
            self._save(rows)

    def checkpoint(self, task_id: str, **changes: object) -> dict:
        with self._lock:
            rows = self._load()
            checkpoint = rows[task_id].setdefault("checkpoint", {})
            checkpoint.update(changes)
            self._save(rows)
            return dict(checkpoint)

    def file_write(self, task_id: str, key: str, record: dict | None = None) -> dict | None:
        with self._lock:
            rows = self._load()
            writes = rows[task_id].setdefault("checkpoint", {}).setdefault("file_writes", {})
            if record is not None:
                writes[key] = dict(record)
                self._save(rows)
            return dict(writes[key]) if key in writes else None

    def runtime(self, task_id: str, method: str, params: dict) -> None:
        if method not in {"turn/started", "turn/completed", "thread/status/changed",
                          "item/started", "item/completed", "error", "request/approval",
                          "request/input"}:
            if method != "backend/changed":
                return
        with self._lock:
            rows = self._load()
            row = rows[task_id]
            if row["state"] not in ACTIVE:
                return
            runtime = row.setdefault("runtime", {})
            tid = params.get("threadId") or (params.get("thread") or {}).get("id")
            turn = params.get("turn") or {}
            if tid:
                runtime["thread_id"] = tid
                row.setdefault("checkpoint", {})["thread_id"] = tid
            if params.get("turnId") or turn.get("id"):
                runtime["turn_id"] = params.get("turnId") or turn["id"]
            phase = {"turn/started": "生成中", "turn/completed": "本轮结束，执行器核对中",
                     "error": "模型报告错误", "request/approval": "等待批准",
                     "request/input": "等待补充信息"}.get(method)
            if method == "backend/changed":
                runtime["backend"] = params.get("backend", "unknown")
                phase = "生成中"
            item = params.get("item") or {}
            if method == "item/started":
                kind = item.get("type", "unknown")
                runtime["item_type"] = kind
                phase = "压缩上下文中" if kind == "contextCompaction" else (
                    "执行工具中" if kind != "agentMessage" else "生成中")
            elif method == "item/completed":
                phase = "生成中"
                if item.get("type") in {"commandExecution", "fileChange"}:
                    row["steps"] += 1
            elif method == "thread/status/changed":
                status = params.get("status") or {}
                flags = status.get("activeFlags", [])
                phase = ("等待批准" if "waitingOnApproval" in flags else
                         "等待补充信息" if "waitingOnUserInput" in flags else
                         {"active": "运行中", "idle": "本轮结束，执行器核对中",
                          "systemError": "模型系统错误", "notLoaded": "线程未加载"}.get(status.get("type")))
            if phase:
                runtime["phase"] = phase
                row["progress"] = phase
            runtime["last_event"] = method
            row["updated_at"] = now()
            self._save(rows)

    def append_input(self, task_id: str, text: str, ctx: SkillContext) -> str:
        if not text.strip() or len(text) > 8000:
            raise SkillError("追加要求不能为空，最多 8000 字")
        with self._lock:
            row = self.get(task_id, ctx)
            if row["body"] != ctx.body or row["channel"] != ctx.channel:
                raise SkillError("请在任务原来的聊天中追加要求")
            if row["state"] not in ACTIVE:
                raise SkillError("任务未在运行；请先继续任务，再追加要求")
            if row["skill"] not in {"background_tasks", "agent", "group_agent"}:
                raise SkillError("这个技能不支持追加要求")
            if row["skill"] in {"agent", "group_agent"} and not ctx.owner:
                raise SkillError("只有主人能更改电脑助手或群沙箱任务")
            item = {"id": uuid4().hex[:12], "text": text.strip(), "status": "queued",
                    "person_id": ctx.person_id, "created_at": now()}
            rows = self._load()
            rows[row["id"]].setdefault("inputs", []).append(item)
            self._save(rows)
            return f"任务 {row['id']}：追加要求已保存，等待执行器接收（编号 {item['id']}）。"

    def pending_inputs(self, task_id: str) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._load()[task_id].get("inputs", [])
                    if i["status"] == "queued"]

    def input_receipt(self, task_id: str, input_id: str, accepted: bool, detail: str) -> None:
        with self._lock:
            rows = self._load()
            for item in rows[task_id].get("inputs", []):
                if item["id"] == input_id:
                    item.update(status="accepted" if accepted else "queued", detail=detail[:200])
            self._save(rows)

    def stop(self, task_id: str, ctx: SkillContext) -> str:
        with self._lock:
            row = self.get(task_id, ctx)
            if row["skill"] in {"agent", "group_agent"} and not ctx.owner:
                raise SkillError("只有主人能停止电脑助手或群沙箱任务")
            if row["state"] not in ACTIVE:
                return f"任务 {row['id']}：{LABELS[row['state']]}。"
            if row["skill"] not in {"background_tasks", "agent", "group_agent", "export_text"}:
                raise SkillError("这个技能不支持中途停止，请等待实际结果")
            self.event(row["id"]).set()
            self.update(row["id"], state="cancelling", progress="已收到停止请求，等待当前调用退出")
            closer = self._closers.get(row["id"])
        if closer:
            try:
                closer()
            except Exception:
                log.exception("任务 %s 停止请求已记录，但中断当前连接暂时失败", row["id"])
        return f"任务 {row['id']}：已请求停止，当前调用结束后退出。"

    def interrupt_active(self) -> None:
        """QQ 服务启动或退出时调用；网页控制台只读，不能改活跃任务状态。"""
        with self._lock:
            rows = self._load()
            changed = False
            for row in rows.values():
                for part, state in list(row.get("delivery", {}).items()):
                    if state == "sending":
                        row["delivery"][part] = "unknown"
                        changed = True
                if row["state"] in ACTIVE:
                    self.event(row["id"]).set()
                    row.update(state="interrupted", progress="服务重启或退出，任务已中断；可继续任务",
                               updated_at=now(), notified=False)
                    changed = True
            if changed:
                self._save(rows)
            closers = list(self._closers.values())
        for close in closers:
            close()

    def execute(self, task_id: str, work: Callable[[], SkillResult]) -> SkillResult:
        with self._lock:
            row = self._load()[task_id]
            if row["state"] == "interrupted":
                return SkillResult("任务已因服务退出而中断", ok=False, report=True,
                                   task_id=task_id)
            if not self.event(task_id).is_set():
                self.update(task_id, state="running", progress="工作线程已启动")
        try:
            if self.event(task_id).is_set() and row["skill"] not in {"agent", "group_agent"}:
                result = SkillResult("任务启动前已停止。", ok=False, report=True)
            else:
                result = work()
        except Exception:
            log.exception("后台任务 %s 出错", task_id)
            result = SkillResult("后台执行出错，任务未完成；请查看程序日志。", ok=False, report=True)
        with self._lock:
            row = self._load()[task_id]
            if row["state"] == "interrupted":
                result = SkillResult("服务退出，任务已中断；需要继续任务才能恢复。",
                                     ok=False, report=True)
            else:
                stopped = self.event(task_id).is_set()
                state = "cancelled" if stopped else "completed" if result.ok else "failed"
                if stopped:
                    result = SkillResult("任务已按要求停止，已生成的文件保留。", ok=False,
                                         report=True, files=list(result.files))
                elif any(i.get("status") == "queued" for i in row.get("inputs", [])):
                    state = "interrupted"
                    result = SkillResult("结束前收到尚未应用的追加要求，已保存；请继续任务。\n"
                                         + result.text, ok=False, report=True, files=list(result.files))
                self.update(task_id, state=state, result=result.text[:12000],
                            files=[str(p) for p in result.files], progress=LABELS[state])
        result.task_id = task_id
        if result.report:
            result.text = f"任务编号：{task_id}\n{result.text}"
        return result

    def summary(self, ctx: SkillContext, task_id: str = "") -> str:
        visible = self.visible(ctx)
        rows = ([self.get(task_id, ctx)] if task_id.strip() else
                [r for r in visible if r["state"] in ACTIVE]
                + [r for r in visible if r["state"] not in ACTIVE][-8:])
        if not rows:
            return "当前聊天没有已登记的后台任务，后台没有在继续执行。"
        lines = []
        for row in rows:
            from companion.trpg.policy import load
            if row["id"] in load(self.path.parent / "trpg-protected.json").get("tasks", []):
                lines.append(f"任务 {row['id']}：{LABELS[row['state']]}；KP资料任务，内容仅在本机管理。")
                continue
            line = (f"任务 {row['id']}：{LABELS[row['state']]}；已执行 {row['steps']} 步；"
                    f"最近更新 {row['updated_at']}；{row['progress']}\n要求：{row['arg'][:300]}")
            if row["result"]:
                line += f"\n结果：{row['result'][:900]}"
            if row["files"]:
                line += "\n文件：" + "、".join(row["files"])
            runtime = row.get("runtime") or {}
            if runtime:
                line += f"\n实际执行阶段：{runtime.get('phase', '未知')}；最近事件：{runtime.get('last_event', '')}"
            if row.get("checkpoint", {}).get("thread_id"):
                line += "\n原任务上下文已保存，可以在原聊天恢复。"
            elif row.get("checkpoint", {}).get("messages"):
                line += "\n执行检查点已保存，可以核对已有产出后继续。"
            pending = sum(i.get("status") == "queued" for i in row.get("inputs", []))
            if pending:
                line += f"\n有 {pending} 条追加要求等待执行器接收。"
            delivery = row.get("delivery", {})
            if any(s in {"unknown", "sending"} for s in delivery.values()):
                line += "\n上次结果发送未获确认，为避免重复发送已暂停自动重投；请核对聊天和产出文件。"
            if "blocked" in delivery.values():
                line += "\n有产出文件因当前权限或文件状态无法发送，可在权限允许时手动取回。"
            lines.append(line)
        return "\n\n".join(lines)


class TaskControl(Skill):
    name = "background_tasks"
    label = "后台任务"
    markers = {
        "任务": "【任务：完整要求和文件路径】把耗时的多步读写、整理、转存交给后台持续执行。"
                "真正登记后才算开始，做完或失败都会通知；普通聊天结束不影响后台任务。",
        "任务状态": "【任务状态：任务编号】查询真实状态；参数写 最近 可查当前聊天最近的任务。",
        "继续任务": "【继续任务：任务编号】从已完成的文件继续中断、停止或失败的任务；先核对产物。",
        "停止任务": "【停止任务：任务编号】停止当前聊天的任务，保留已经生成的文件。",
        "追加任务": "【追加任务：任务编号｜追加要求】把新要求交给正在运行的原任务，权限和发送去向不变。",
        "压缩任务": "【压缩任务：任务编号】让后台技能任务在下一步压缩原线程上下文，文件和检查点保留。",
    }

    def __init__(self, book: TaskBook, worker: Callable[[str, SkillContext], SkillResult],
                 resume: Callable[[dict, SkillContext], SkillResult]) -> None:
        self.book, self.worker, self.resume = book, worker, resume

    def context(self, ctx: SkillContext) -> str:
        return self._render_context(ctx, include_updated_at=True)

    def session_context(self, ctx: SkillContext) -> str:
        # 持久会话省略高频更新时间戳，只在任务状态或步数真正变化时刷新槽位。
        return self._render_context(ctx, include_updated_at=False)

    def _render_context(self, ctx: SkillContext, *, include_updated_at: bool) -> str:
        try:
            rows = self.book.visible(ctx)
            active = [r for r in rows if r["state"] in ACTIVE]
            latest = [r for r in rows if r["state"] not in ACTIVE][-1:]
            # 每轮只给真实状态水位；要求、旧结果与附件在显式查询时再读取。
            status = "\n".join(
                (
                    f"任务 {r['id']}：{LABELS[r['state']]}；{r['steps']}步；更新 {r['updated_at']}"
                    if include_updated_at
                    else f"任务 {r['id']}：{LABELS[r['state']]}；{r['steps']}步"
                )
                for r in [*active[:3], *latest]
            ) or "当前聊天没有已登记的后台任务，后台没有在继续执行。"
        except (OSError, ValueError, KeyError):
            log.exception("任务记录无法读取")
            status = "任务记录暂时无法读取，不能确认后台状态；不要声称任务不存在或已完成。"
        return ("【程序记录的真实后台状态】\n" + status
                + "\n只有成功登记的任务才会在后台继续。不得把打算说成已经开始；"
                "状态不明就用任务状态查询，不要猜。群里的多步工作应先登记【任务】。")

    def markers_for(self, ctx: SkillContext) -> dict[str, str]:
        if ctx.channel.startswith("group:"):
            return self.markers
        return {k: v for k, v in self.markers.items() if k != "任务"}

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        if marker == "追加任务":
            task_id, _, text = arg.replace("|", "｜", 1).partition("｜")
            return SkillResult(self.book.append_input(task_id, text, ctx))
        task_id = "" if arg.strip() in ("最近", "全部", "当前") else arg.strip()
        if marker == "压缩任务":
            row = self.book.get(task_id, ctx)
            if row["channel"] != ctx.channel or row["body"] != ctx.body:
                raise SkillError("请在任务原聊天操作")
            if row["state"] not in ACTIVE or row["skill"] != "background_tasks":
                raise SkillError("只有运行中的后台技能任务支持此操作；原生助手由 Codex 自动压缩")
            self.book.checkpoint(row["id"], compact_requested=True)
            return SkillResult("压缩请求已保存，执行器将在当前轮结束后处理。")
        if marker == "任务状态":
            return SkillResult(self.book.summary(ctx, task_id))
        if marker == "停止任务":
            return SkillResult(self.book.stop(task_id, ctx))
        if ctx.task_id:
            raise SkillError("当前已经在后台执行，请直接做事，不要再创建或续跑嵌套任务")
        if marker == "继续任务":
            row = self.book.get(task_id, ctx)
            from companion.trpg.policy import load
            if row["id"] in load(self.book.path.parent / "trpg-protected.json").get("tasks", []):
                raise SkillError("这个历史任务包含KP资料，不能从普通聊天恢复；请在本机跑团控制台处理")
            if row["channel"] != ctx.channel:
                raise SkillError("请回到任务原来的聊天中继续，避免把结果发错地方")
            if row["state"] in ACTIVE:
                raise SkillError(f"任务 {row['id']} 仍在执行，不要重复启动")
            if any(r["state"] in ACTIVE and r["channel"] == ctx.channel
                   for r in self.book.visible(ctx)):
                raise SkillError("当前聊天已有运行中的任务，请先查询或停止，避免重复恢复")
            result = self.resume(row, ctx)
            result.resume_of = row["id"]
            return result
        if not ctx.channel.startswith("group:"):
            raise SkillError("此后台读写任务用于群聊；主人私聊请使用电脑助手")
        if not arg.strip():
            raise SkillError("请给出完整任务要求与文件路径")
        if any(r["state"] in ACTIVE for r in self.book.visible(ctx)
               if r["channel"] == ctx.channel):
            raise SkillError("当前聊天已有后台任务，请先查询状态或停止，避免重复处理文件")
        result = SkillResult("后台任务已登记；请简短告诉对方任务编号，完成或失败后会通知。",
                             report=True)

        def work() -> SkillResult:
            assert result.task_id
            background = replace(ctx, task_id=result.task_id,
                                 stop_event=self.book.event(result.task_id), notify=None, ask=None)
            return self.worker(arg.strip(), background)

        result.later = work
        return result

    def status(self) -> str:
        return "持久记录编号、状态、进度与结果；支持查询、停止和继续"
