"""生图：Codex 内置生图工具（gpt-image），只用 GPT 订阅。

- 【画：画面描述】→ 她先回一句（如“等我画一下”），图在后台画，画好直接发出去（约半分钟到一分钟）。
- 用单独的 Codex 进程，画图时不耽误聊天。
- 私聊不限；所有群聊共享“group_window_minutes 分钟内最多 group_limit 张”的限额（重启不清零）。
- 订阅额度到了切换阈值（或没启用订阅）时暂停。生图耗额度约是普通对话的 3–5 倍。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from companion.config import ImageSkillConfig
from companion.models.openai_compat import ModelError
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

log = logging.getLogger(__name__)


class ImagePainter(Protocol):
    """真正画图的东西：Codex 进程（测试时换成假的）。"""

    def generate_image(self, prompt: str) -> tuple[Path, str]: ...

    def close(self) -> None: ...


class ImageGen(Skill):
    name = "image"
    label = "画画"
    markers = {
        "画": "【画：画面描述】画一张图，画好会自动发给对方（要等半分钟到一分钟）。"
        "描述要具体：画什么、什么风格、什么构图；只在对方想要图的时候画",
    }

    def __init__(
        self,
        cfg: ImageSkillConfig,
        painter: Callable[[], ImagePainter] | None,
        state_path: Path,
        *,
        quota_ok: Callable[[], bool] = lambda: True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self._make_painter = painter
        self._painter: ImagePainter | None = None
        self.state_path = state_path
        self.quota_ok = quota_ok
        self.now = clock
        self._lock = threading.Lock()
        self._busy = False

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
        prompt = arg.strip()
        if not prompt:
            raise SkillError("没说要画什么")
        if not self.quota_ok():
            raise SkillError("GPT 订阅额度快用完了，暂时不能画画，等额度恢复再说")
        with self._lock:
            if self._busy:
                raise SkillError("正在画另一张，等那张画完再说")
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
            self._busy = True
        return SkillResult(
            "已经开始画了，大约半分钟到一分钟后画好，会自动发给对方。"
            "现在先简短回一句（比如“等我画一下”），不要说已经画好了，也不要描述画出来的样子。",
            later=lambda: self._paint(prompt, stamp),
        )

    def _paint(self, prompt: str, stamp: datetime | None) -> SkillResult:
        try:
            if self._painter is None:
                assert self._make_painter is not None
                self._painter = self._make_painter()
            path, revised = self._painter.generate_image(prompt)
            log.info("画好了：%s", revised[:80])
            return SkillResult(revised, images=[path])
        except ModelError as e:
            log.warning("画图失败：%s", e)
            self._refund(stamp)
            return SkillResult(f"没画出来：{e}", ok=False)
        except Exception:
            log.exception("画图出错")
            self._refund(stamp)
            return SkillResult("没画出来：程序出了点问题", ok=False)
        finally:
            with self._lock:
                self._busy = False

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
