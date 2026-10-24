"""技能框架：标记解析、可用性判断、执行与使用记录。

她在回复里写【搜索：关键词】这样的标记；程序找到第一个可用的标记，执行对应技能，
把结果作为“技能结果”交回给她，她再接着回复。标记前面的话（如“我查一下”）会先发出去。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from companion.config import SkillsConfig

log = logging.getLogger(__name__)

# 【标记：参数】或 [标记:参数]；“表情”留给表情包
MARKER = re.compile(r"[【\[]\s*([^\s【】\[\]：:]{1,6})\s*[：:]\s*(.+?)\s*[】\]]", re.DOTALL)
RESERVED = {"表情"}


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
    notify: Callable[[str], None] | None = None  # 技能执行中给对方发提示（如审批请求）


@dataclass
class SkillResult:
    text: str  # 交回给她的结果
    images: list[Path] = field(default_factory=list)  # 需要发给对方的图片
    ok: bool = True


@dataclass
class SkillCall:
    skill: str
    marker: str
    arg: str
    ok: bool
    seconds: float
    images: list[Path] = field(default_factory=list)


class Skill:
    """技能基类。子类设置 name / label / markers，并实现 run。"""

    name = ""  # 配置里的键，如 search
    label = ""  # 显示名，如 联网搜索
    markers: ClassVar[dict[str, str]] = {}  # 标记 → 给她看的用法说明

    def enabled(self) -> bool:
        return True

    def allowed(self, ctx: SkillContext) -> bool:
        """这个场合能不能用（如 Agent 只限主人私聊）。"""
        return True

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        raise NotImplementedError

    def status(self) -> str:
        """控制台显示的当前状态（如额度）。"""
        return ""

    def close(self) -> None:
        pass


class Skills:
    def __init__(
        self, skills: list[Skill], cfg: SkillsConfig, usage_path: Path | None = None
    ) -> None:
        self.all = skills
        self.cfg = cfg
        self.usage_path = usage_path

    def close(self) -> None:
        for s in self.all:
            s.close()

    def available(self, ctx: SkillContext | None) -> list[Skill]:
        if ctx is None or not self.cfg.enabled:
            return []
        return [s for s in self.all if s.enabled() and s.allowed(ctx)]

    def prompt(self, ctx: SkillContext | None) -> str | None:
        """写进提示词的技能说明；没有可用技能时返回 None。"""
        skills = self.available(ctx)
        if not skills:
            return None
        lines = [f"- {usage}" for s in skills for usage in s.markers.values()]
        return (
            "【技能】\n你可以用下面的技能。需要时在回复里单独写一个标记，程序会执行并把结果告诉你，"
            "你再接着回复；标记前可以先说一句（如“我查一下”），会先发出去。\n"
            + "\n".join(lines)
            + "\n不需要时就别用；一次只写一个标记；不要编造技能结果。"
        )

    def find(self, text: str, ctx: SkillContext | None) -> tuple[str, Skill, str, str] | None:
        """找到第一个可用的技能标记，返回（标记前的话，技能，标记，参数）。"""
        by_marker = {m: s for s in self.available(ctx) for m in s.markers}
        for match in MARKER.finditer(text):
            skill = by_marker.get(match.group(1))
            if skill is not None:
                return text[: match.start()].strip(), skill, match.group(1), match.group(2).strip()
        return None

    def strip_markers(self, text: str) -> str:
        """去掉没被执行的技能标记（不认识的、不可用的、超过次数的）。"""
        known = {m for s in self.all for m in s.markers}
        return MARKER.sub(
            lambda m: "" if m.group(1) in known and m.group(1) not in RESERVED else m.group(0),
            text,
        ).strip()

    def run(
        self, skill: Skill, marker: str, arg: str, ctx: SkillContext
    ) -> tuple[SkillCall, SkillResult]:
        """执行技能；出错时把原因作为结果交回给她。返回调用记录和结果。"""
        started = time.monotonic()
        try:
            result = skill.run(marker, arg, ctx)
        except SkillError as e:
            result = SkillResult(f"没成功：{e}", ok=False)
        except Exception:
            log.exception("技能 %s 出错", skill.name)
            result = SkillResult("没成功：程序出了点问题。", ok=False)
        seconds = time.monotonic() - started
        status = "完成" if result.ok else "失败"
        log.info("技能 %s（%s）%s，%.1f 秒", skill.label, arg[:60], status, seconds)
        call = SkillCall(skill.name, marker, arg, result.ok, seconds, list(result.images))
        self._record(call, ctx)
        return call, result

    def feedback(self, skill: Skill, marker: str, result: SkillResult) -> str:
        text = result.text
        limit = self.cfg.max_result_chars
        if len(text) > limit:
            text = text[:limit] + "\n……（后面省略）"
        return (
            f"【{skill.label}的结果】（你刚才用了「{marker}」）\n{text}\n\n"
            "这是程序返回的结果，不是对方说的话。请根据它接着回复对方，不要重复已经说过的话。"
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
