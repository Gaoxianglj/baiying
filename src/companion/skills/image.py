"""生图：Codex 内置生图工具（gpt-image），只用 GPT 订阅。

- 【画：画面描述｜参考：图片编号｜存到：花册/玫瑰】→ 她先回一句（如“等我画一下”），图在后台画，
  画好直接发出去（约半分钟到一分钟）。参考、存到都可以不写。
- 【画动图：画面描述】：工具本身出不了多帧，这里让它画一张 4×4 的精灵图，程序切开合成 GIF，
  和画一张图花一样的额度。
- 用单独的 Codex 进程，画图时不耽误聊天；接连点了几张就排队，一张一张画。
- 私聊不限；所有群聊共享“group_window_minutes 分钟内最多 group_limit 张”的限额（重启不清零）。
- 订阅额度到了切换阈值（或没启用订阅）时暂停。生图耗额度约是普通对话的 3–5 倍。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from companion import media
from companion.config import ImageSkillConfig
from companion.models.openai_compat import ModelError
from companion.skills.access import FileAccess
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult
from companion.skills.history import ChatImages

log = logging.getLogger(__name__)

MAX_REFERENCES = 4
GIF_KEEP_SECONDS = 86400.0  # 合成的动图发出去后就存进图片库了，临时文件留一天
_SPLIT_REFS = re.compile(r"[,，、\s]+")
_OPTION = re.compile(r"(参考图?|存到|保存到)\s*[：:]\s*(.+)", re.DOTALL)

GIF_PROMPT = (
    "一张 4×4 共 16 格的精灵图（sprite sheet），之后会被切开做成循环动画。"
    "16 格大小完全相同、排列整齐，格子之间没有边框、线条和间隙，图上没有任何文字。"
    "从左到右、从上到下是连续的 16 帧动画：{prompt}。"
    "每一格里主体的位置、大小、背景、画风完全一致，只有要动的部分一点一点地变化，"
    "最后一帧能自然接回第一帧。"
)


class ImagePainter(Protocol):
    """真正画图的东西：Codex 进程（测试时换成假的）。"""

    def generate_image(self, prompt: str, references: Sequence[Path] = ()) -> tuple[Path, str]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _Job:
    prompt: str
    animated: bool
    references: tuple[Path, ...]
    save_to: str | None
    stamp: datetime | None  # 占用的群聊限额（没画出来要退）


class ImageGen(Skill):
    name = "image"
    label = "画画"
    markers = {
        "画": "【画：画面描述】画一张图，画好会自动发给对方（要等半分钟到一分钟）。"
        "描述要具体：画什么、什么风格、什么构图；只在对方想要图的时候画。"
        "照着某张图画就加｜参考：图片编号（可以几张，也可以写文件路径）；"
        "画好要留着就加｜存到：花册/玫瑰。一次要画好几张就接着写下一个，会排队一张张画",
        "画动图": "【画动图：画面描述】画一张会动的循环 GIF（16 帧，比如眨眼、摆尾、飘雪）。"
        "写清楚什么在动、怎么动，动作小一点效果好；｜参考、｜存到 和【画】一样",
    }

    def __init__(
        self,
        cfg: ImageSkillConfig,
        painter: Callable[[], ImagePainter] | None,
        state_path: Path,
        *,
        quota_ok: Callable[[], bool] = lambda: True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        images: ChatImages | None = None,
        access: FileAccess | None = None,
    ) -> None:
        self.cfg = cfg
        self._make_painter = painter
        self._painter: ImagePainter | None = None
        self.state_path = state_path
        self.quota_ok = quota_ok
        self.now = clock
        self.images = images
        self.access = access
        self._lock = threading.Lock()
        self._paint_lock = threading.Lock()  # 一次只画一张，后面的排队
        self._queued = 0

    # ---- 可用性 ----

    def enabled(self) -> bool:
        return self.cfg.enabled and self._make_painter is not None

    def status(self) -> str:
        if self._make_painter is None:
            return "需要 GPT 订阅"
        used = len(self._recent_group_times())
        return (
            f"群聊 {self.cfg.group_window_minutes:g} 分钟内已画 {used} / {self.cfg.group_limit} 张"
        )

    # ---- 执行 ----

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        prompt, refs, save_to = _parse(arg)
        if not prompt:
            raise SkillError("没说要画什么")
        if not self.quota_ok():
            raise SkillError("GPT 订阅额度快用完了，暂时不能画画，等额度恢复再说")
        references = self._references(refs, ctx)
        with self._lock:
            if self._queued >= self.cfg.queue_limit:
                raise SkillError(f"已经排了 {self._queued} 张了，等前面的画完再说")
            stamp = None
            if not ctx.private:
                times = self._recent_group_times()
                if len(times) >= self.cfg.group_limit:
                    wait = times[0] + timedelta(minutes=self.cfg.group_window_minutes) - self.now()
                    minutes = max(1, int(wait.total_seconds() // 60) + 1)
                    raise SkillError(
                        f"群里画得有点多了（{self.cfg.group_window_minutes:g} 分钟最多 "
                        f"{self.cfg.group_limit} 张），{minutes} 分钟后才能再画"
                    )
                stamp = self.now()
                self._save_group_times([*times, stamp])
            ahead = self._queued
            self._queued += 1
        job = _Job(prompt, marker == "画动图", references, save_to, stamp)
        queue = f"前面还有 {ahead} 张在画，" if ahead else ""
        return SkillResult(
            f"已经开始画了，{queue}大约半分钟到一分钟一张，画好会自动发给对方。"
            "现在先简短回一句（比如“等我画一下”），不要说已经画好了，也不要描述画出来的样子。",
            later=lambda: self._paint(job, ctx),
        )

    def _references(self, refs: list[str], ctx: SkillContext) -> tuple[Path, ...]:
        if not refs:
            return ()
        if self.images is None:
            raise SkillError("现在不能用参考图")
        if len(refs) > MAX_REFERENCES:
            raise SkillError(f"参考图最多 {MAX_REFERENCES} 张")
        return tuple(self.images.find(ref, ctx).view for ref in refs)

    def _paint(self, job: _Job, ctx: SkillContext) -> SkillResult:
        try:
            with self._paint_lock:
                if self._painter is None:
                    assert self._make_painter is not None
                    self._painter = self._make_painter()
                prompt = GIF_PROMPT.format(prompt=job.prompt) if job.animated else job.prompt
                path, revised = self._painter.generate_image(prompt, job.references)
            description = revised
            if job.animated:
                path = self._to_gif(path)
                description = job.prompt  # 改写后的描述讲的是精灵图怎么排，记下来没用
            log.info("画好了：%s", description[:80])
            return SkillResult(description, images=[path], note=self._keep(path, job, ctx))
        except ModelError as e:
            log.warning("画图失败：%s", e)
            self._refund(job.stamp)
            return SkillResult(f"没画出来：{e}", ok=False)
        except Exception:
            log.exception("画图出错")
            self._refund(job.stamp)
            return SkillResult("没画出来：程序出了点问题", ok=False)
        finally:
            with self._lock:
                self._queued -= 1

    def _to_gif(self, sheet: Path) -> Path:
        folder = self.state_path.parent / "animated"
        if folder.is_dir():
            cutoff = time.time() - GIF_KEEP_SECONDS
            for old in folder.glob("*.gif"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
        return media.sheet_to_gif(
            sheet,
            folder / f"{uuid4().hex}.gif",
            frame_ms=self.cfg.gif_frame_ms,
            max_side=self.cfg.gif_side,
        )

    def _keep(self, path: Path, job: _Job, ctx: SkillContext) -> str:
        """｜存到：画好的图存进文件夹。存不了不算没画成，图照发，在记录里说明。"""
        if job.save_to is None or self.access is None:
            return ""
        data = path.read_bytes()
        try:
            saved = self.access.save_new(
                job.save_to, data, ctx, default_name="图片.png", suffix=media.image_suffix(data)
            )
        except (SkillError, OSError) as e:
            log.warning("画好的图没存成（%s）：%s", job.save_to, e)
            return f"（想存到 {job.save_to}，没存成：{e}）"
        return f"（存在 {self.access.display(saved, ctx)}）"

    # ---- 群聊限额（存在文件里，重启不清零） ----

    def _recent_group_times(self) -> list[datetime]:
        cutoff = self.now() - timedelta(minutes=self.cfg.group_window_minutes)
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            times = [datetime.fromisoformat(t) for t in raw.get("group_times", [])]
        except (OSError, ValueError):
            times = []
        return sorted(t for t in times if t > cutoff)

    def _save_group_times(self, times: list[datetime]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"group_times": [t.isoformat() for t in times]}
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    def _refund(self, stamp: datetime | None) -> None:
        """没画出来的不算进限额。"""
        if stamp is None:
            return
        with self._lock:
            self._save_group_times([t for t in self._recent_group_times() if t != stamp])

    def close(self) -> None:
        if self._painter is not None:
            self._painter.close()


def _parse(arg: str) -> tuple[str, list[str], str | None]:
    """画面描述｜参考：a、b｜存到：花册/玫瑰 → （描述，参考图，存到哪）。不认识的段算描述的一部分。"""
    parts = [p.strip() for p in arg.replace("|", "｜").split("｜")]
    prompt = [parts[0]]
    refs: list[str] = []
    save_to: str | None = None
    for part in parts[1:]:
        option = _OPTION.fullmatch(part)
        if option and option.group(1).startswith("参考"):
            value = option.group(2)
            # 路径里可能有空格，看起来是路径就不拆
            refs += [value] if "/" in value or "\\" in value else _SPLIT_REFS.split(value)
        elif option:
            save_to = option.group(2)
        elif part:
            prompt.append(part)
    return "，".join(p for p in prompt if p), [r for r in refs if r], save_to
