"""技能框架：标记解析、可用性判断、执行与使用记录。

她在回复里写【搜索：关键词】这样的标记；程序找到第一个可用的标记，执行对应技能，
把结果作为“技能结果”交回给她，她再接着回复。标记前面的话（如“我查一下”）会先发出去。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from companion.skills.tasks import TaskBook

from companion.config import SkillsConfig

log = logging.getLogger(__name__)

# 【标记：参数】或 [标记:参数]；只写【标记】时参数是 None，只有 bare 的技能才算数。
# “表情”“心情”留给表情包和心情
MARKER = re.compile(r"[【\[]\s*([^\s【】\[\]：:]{1,6})\s*(?:[：:]\s*(.*?)\s*)?[】\]]", re.DOTALL)
RESERVED = {"表情", "心情", "回应", "语音"}
# 【写文件：路径】后面跟着 <<<内容>>>（内容里可以有任何括号，所以不能放在标记里）
BLOCK = re.compile(r"\s*<<<[ \t]*\n?(.*)\n?[ \t]*>>>", re.DOTALL)


class SkillError(Exception):
    """技能执行失败；消息会交回给她（不要放敏感信息）。"""


@dataclass
class SkillContext:
    """这次对话的场合：决定哪些技能可以用。"""

    body: str  # qq / cli / minecraft
    channel: str
    person_id: str
    private: bool  # 私聊或本地命令行
    owner: bool  # 主人本人
    now: datetime | None = None
    notify: Callable[[str], None] | None = None  # 技能执行中给对方发提示（如进度）
    ask: Callable[[str], bool] | None = (
        None  # 向主人请求批准（会阻塞到主人回复或超时）；None = 一律拒绝
    )
    # 向 QQ 要一条消息里的图片（她回头看当时没看的图）；None = 这个身体做不到
    fetch_images: Callable[[str], list[bytes]] | None = None
    # 这一轮读过本机文件、屏幕或剪贴板：不能再打开网页，防止内容被带出去
    touched_local: bool = False
    task_id: str | None = None
    stop_event: threading.Event | None = None
    task_book: TaskBook | None = None
    resume_of: str = ""
    deadline: float | None = None
    request_id: str | None = None  # 真实骰子的请求幂等键；由QQ消息或原生工具编号产生。
    trpg_principal: str | None = None  # 仅服务器专用主持通道签发；绝不从工具/QQ参数反序列化。
    query: str = ""  # 当前要求，供权限范围内的文件发现使用。
    fetch_files: Callable[[str], list[dict]] | None = None  # 仅绑定当前QQ群的文件查找和取回。


@dataclass
class SkillResult:
    text: str  # 交回给她的结果
    images: list[Path] = field(default_factory=list)  # 需要发给对方的图片
    files: list[Path] = field(default_factory=list)  # 需要发给对方的文件
    ok: bool = True
    # 耗时的活（如画图）：先交回 text 让她回一句，真正的活放到后台做，做完把图片直接发出去
    later: Callable[[], SkillResult] | None = None
    report: bool = False  # 后台做完后让她用自己的话把结果告诉对方（而不是直接发图）
    max_chars: int | None = None  # 交回给她时最多多少字（默认按 skills.max_result_chars）
    view: list[Path] = field(default_factory=list)  # 放进这一轮给她看的图片（JPEG）
    temporary: bool = False  # view 里的图这一轮说完就删（屏幕、剪贴板）
    note: str = ""  # 后台做完记成消息时附带的说明（如存在哪）
    task_id: str | None = None
    resume_of: str = ""


@dataclass
class SkillCall:
    skill: str
    marker: str
    arg: str
    ok: bool
    seconds: float
    images: list[Path] = field(default_factory=list)
    later: Callable[[], SkillResult] | None = None  # 放到后台做的活
    files: list[Path] = field(default_factory=list)
    task_id: str | None = None


class Skill:
    """技能基类。子类设置 name / label / markers，并实现 run。"""

    name = ""  # 配置里的键，如 search
    label = ""  # 显示名，如 联网搜索
    markers: ClassVar[dict[str, str]] = {}  # 标记 → 给她看的用法说明
    aliases: ClassVar[tuple[str, ...]] = ()  # 兼容旧标记名：可执行、可剥离，但不写进提示词
    takes_block = False  # 标记后面可以跟 <<<内容>>>，内容接在参数后面（用 ｜ 隔开）
    bare = False  # 不用参数：只写【标记】也算

    def enabled(self) -> bool:
        return True

    def allowed(self, ctx: SkillContext) -> bool:
        """这个场合能不能用（如 Agent 只限主人私聊）。"""
        return True

    def channel_allowed(self, ctx: SkillContext) -> bool:
        """这个频道整体是否开放该工具声明（避免同一群聊因发言人不同而重建持久线程）。"""
        return self.allowed(ctx)

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        raise NotImplementedError

    def status(self) -> str:
        """控制台显示的当前状态（如额度）。"""
        return ""

    def context(self, ctx: SkillContext) -> str:
        """这个场合她用技能前需要知道的情况（如这个人还有哪些提醒没到），写进技能说明。"""
        return ""

    def session_context(self, ctx: SkillContext) -> str:
        """持久会话专用状态块；默认与 context 一致，需在清空时显式覆盖旧槽位的技能可覆写。"""
        return self.context(ctx)

    def markers_for(self, ctx: SkillContext) -> dict[str, str]:
        return self.markers

    def close(self) -> None:
        pass


class Skills:
    def __init__(
        self,
        skills: list[Skill],
        cfg: SkillsConfig,
        usage_path: Path | None = None,
        tasks: TaskBook | None = None,
    ) -> None:
        self.all = skills
        self.cfg = cfg
        self.usage_path = usage_path
        self.tasks = tasks

    def close(self) -> None:
        for s in self.all:
            s.close()

    def available(self, ctx: SkillContext | None) -> list[Skill]:
        if ctx is None or not self.cfg.enabled:
            return []
        return [s for s in self.all if s.enabled() and s.allowed(ctx)]

    def channel_available(self, ctx: SkillContext | None) -> list[Skill]:
        if ctx is None or not self.cfg.enabled:
            return []
        return [s for s in self.all if s.enabled() and s.channel_allowed(ctx)]

    def can_use(self, marker: str, ctx: SkillContext | None) -> bool:
        return any(marker in s.markers_for(ctx) for s in self.available(ctx))

    def prompt(self, ctx: SkillContext | None) -> str | None:
        """写进提示词的技能说明；没有可用技能时返回 None。"""
        stable, dynamic = self.prompt_parts(ctx)
        return "\n".join([stable, *dynamic]) if stable else None

    def prompt_parts(self, ctx: SkillContext | None) -> tuple[str, list[str]]:
        """工具说明按版本复用，任务/群/文件夹等实时状态每轮刷新。"""
        skills = self.available(ctx)
        if not skills:
            return "", []
        lines = [f"- {usage}" for s in skills for usage in s.markers_for(ctx).values()]
        notes = [note for s in skills if (note := s.context(ctx))]
        return self._usage(lines), notes

    def session_prompt_parts(
        self, ctx: SkillContext | None
    ) -> tuple[str, list[tuple[str, str]]]:
        """持久会话专用：工具说明按频道固定，实时状态按技能名独立去重。"""
        channel_skills = self.channel_available(ctx)
        if not channel_skills:
            return "", []
        lines = [f"- {usage}" for s in channel_skills for usage in s.markers_for(ctx).values()]
        active_skills = self.available(ctx)
        notes = [
            (s.name or f"skill_{i}", note)
            for i, s in enumerate(active_skills)
            if (note := s.session_context(ctx))
        ]
        return self._usage(lines), notes

    @staticmethod
    def _usage(lines: list[str]) -> str:
        return (
            "【技能】\n你可以用下面的技能。需要时在回复里单独写一个标记，程序会执行并把结果告诉你，"
            "你再接着回复；标记前可以先说一句（如“我查一下”），会先发出去。\n"
            + "\n".join(lines)
            + "\n不需要时就别用；一次只写一个标记；不要编造技能结果。"
            "要求明确且在权限范围内时，自己完成查找、读取、转换、调用和结果核验，"
            "不要反复确认已授权的常规步骤。先解决可修复的路径或参数问题，再给出结果。"
            "文件正文不在当前上下文时，先查文件并读取，不能直接要求重传或贴正文。"
        )

    def find(self, text: str, ctx: SkillContext | None) -> tuple[str, Skill, str, str] | None:
        """找到第一个可用的技能标记，返回（标记前的话，技能，标记，参数）。"""
        avail = self.available(ctx)
        by_marker = {m: s for s in avail for m in (*s.aliases, *s.markers_for(ctx))}
        for match in MARKER.finditer(text):
            skill = by_marker.get(match.group(1))
            if skill is not None and (match.group(2) is not None or skill.bare):
                arg = (match.group(2) or "").strip()
                if skill.takes_block and (block := BLOCK.match(text, match.end())):
                    arg = f"{arg}｜{block.group(1).rstrip()}"
                return text[: match.start()].strip(), skill, match.group(1), arg
        return None

    def strip_markers(self, text: str) -> str:
        """去掉没被执行的技能标记（不认识的、不可用的、超过次数的）。"""
        known = {m for s in self.all for m in (*s.markers, *s.aliases)}
        bare = {m for s in self.all if s.bare for m in (*s.markers, *s.aliases)}
        for marker in (m for s in self.all if s.takes_block for m in s.markers):
            text = re.sub(rf"[【\[]\s*{marker}\s*[：:][^】\]]*[】\]]{BLOCK.pattern}", "", text,
                          flags=re.DOTALL)  # fmt: skip

        def is_marker(m: re.Match[str]) -> bool:
            if m.group(1) not in known or m.group(1) in RESERVED:
                return False
            return m.group(2) is not None or m.group(1) in bare

        return MARKER.sub(lambda m: "" if is_marker(m) else m.group(0), text).strip()

    def run(
        self, skill: Skill, marker: str, arg: str, ctx: SkillContext
    ) -> tuple[SkillCall, SkillResult]:
        """执行技能；出错时把原因作为结果交回给她。返回调用记录和结果。"""
        source_ctx, ctx = ctx, replace(ctx)
        started = time.monotonic()
        try:
            result = skill.run(marker, arg, ctx)
        except SkillError as e:
            result = SkillResult(f"没成功：{e}", ok=False)
        except Exception:
            log.exception("技能 %s 出错", skill.name)
            result = SkillResult("没成功：程序出了点问题。", ok=False)
        source_ctx.touched_local = source_ctx.touched_local or ctx.touched_local
        seconds = time.monotonic() - started
        if result.later is not None and self.tasks is not None:
            task_id = self.tasks.create(skill.name, marker, arg, ctx, resume_of=result.resume_of)
            work = result.later
            result.task_id = task_id
            ctx.task_id, ctx.task_book, ctx.resume_of = task_id, self.tasks, result.resume_of
            ctx.stop_event = self.tasks.event(task_id)
            result.later = lambda: self.tasks.execute(task_id, work)
            result.text += (
                f"\n任务编号：{task_id}；真实状态：排队中。可用【任务状态：{task_id}】查询。"
            )
            if hasattr(skill, "cancel"):
                ctx.stop_event = self.tasks.event(task_id)
        status = "完成" if result.ok else "失败"
        log.info("技能 %s（%s）%s，%.1f 秒", skill.label, arg[:60], status, seconds)
        call = SkillCall(
            skill.name, marker, arg, result.ok, seconds, list(result.images), result.later,
            list(result.files),
        )  # fmt: skip
        call.task_id = result.task_id
        self._record(call, ctx)
        return call, result

    def feedback(self, skill: Skill, marker: str, result: SkillResult) -> str:
        text = result.text
        limit = result.max_chars or self.cfg.max_result_chars
        if len(text) > limit:
            text = text[:limit] + "\n……（后面省略）"
        return (
            f"【{skill.label}的结果】（你刚才用了「{marker}」）\n{text}\n\n"
            "这是程序返回的结果，不是对方说的话。请根据它接着回复对方，不要重复已经说过的话。"
            + (
                "工具失败先检查路径、现有文件和可用操作，在权限范围内换正确方法继续。"
                "完成自查仍缺必要信息时，只问最关键的一项；不用让用户处理内部参数。"
                if not result.ok
                else ""
            )
        )

    # ---- 使用记录（控制台查看） ----

    def _record(self, call: SkillCall, ctx: SkillContext) -> None:
        if self.usage_path is None:
            return
        row = {
            "ts": (ctx.now or datetime.now(UTC)).isoformat(),
            "skill": call.skill,
            "arg": call.arg[:200],
            "channel": ctx.channel,
            "ok": call.ok,
            "seconds": round(call.seconds, 1),
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def recent_usage(path: Path, limit: int = 30) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[::-1]
