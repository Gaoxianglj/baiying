"""人物基础设定图与当前服装设定图管理。

目录结构（位于记忆文件夹 `self/appearance/`）：
- `base.{png,jpg,jpeg,webp}`：人物基础设定图（不可变基石，只由主人放置或手动替换）。
- `outfit.{png,jpg,jpeg,webp}`：当前带服装设定图（由基础图 + `preferences.md` 穿搭生成）。
- `state.json`：记录当前服装设定图对应的穿搭摘要与哈希，用于感知衣着变化。
- `history/`：衣着变动重新生成新服装图时，自动归档旧服装图。

使用方式：
- 主人确认两张图后放入 `self/appearance/base.png` 和 `self/appearance/outfit.png` 即可立即生效，
  无需手动写 `state.json`（首次读取时自动认领当前穿搭哈希）。
- 后续无论是每周反思更新 `preferences.md`、控制台编辑还是手工修改“穿搭与随身物”，
  程序都会感知哈希变化，在下一次反思或画她自己前用 `base` + 新穿搭描述自动生成新 `outfit`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from companion.memory.layout import MemoryLayout
from companion.models.openai_compat import ModelError

log = logging.getLogger(__name__)

IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
OUTFIT_SECTION = "穿搭与随身物"
SELF_IMAGE_SECTION = "我想象中的自己"


class ImageGenerator(Protocol):
    def generate_image(self, prompt: str, references: Sequence[Path] = ()) -> tuple[Path, str]: ...


def extract_section(markdown: str, heading_keyword: str) -> str:
    """提取 Markdown 中标题包含 heading_keyword 的那一节正文。"""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading_keyword in line:
            body: list[str] = []
            for nxt in lines[i + 1 :]:
                if nxt.startswith("## "):
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return ""


def normalize_outfit_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def outfit_digest(text: str) -> str:
    return hashlib.sha256(normalize_outfit_text(text).encode("utf-8")).hexdigest()


class AppearanceStore:
    """管理伙伴的不可变基础形象图与随穿搭联动的服装设定图。"""

    def __init__(self, layout: MemoryLayout, persona_name: str) -> None:
        self.layout = layout
        self.name = persona_name
        self._lock = threading.RLock()

    @property
    def dir(self) -> Path:
        return self.layout.self_dir / "appearance"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def history_dir(self) -> Path:
        return self.dir / "history"

    def base_path(self) -> Path | None:
        return self._find_image("base")

    def outfit_path(self) -> Path | None:
        return self._find_image("outfit")

    def current_outfit_text(self) -> str:
        """当前穿搭描述：优先取 `preferences.md` 的“穿搭与随身物”，缺失时回退 `seed.md`。"""
        pref_path = self.layout.self_dir / "preferences.md"
        if pref_path.exists():
            sec = extract_section(pref_path.read_text("utf-8"), OUTFIT_SECTION)
            if sec:
                return sec
        seed_path = self.layout.self_dir / "seed.md"
        if seed_path.exists():
            return extract_section(seed_path.read_text("utf-8"), SELF_IMAGE_SECTION)
        return ""

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def adopt_existing_if_untracked(self, *, now: datetime | None = None) -> bool:
        """若主人已放入 `outfit.*` 但还没有 `state.json`，认领其为当前穿搭版本，避免重复重画。"""
        with self._lock:
            outfit = self.outfit_path()
            if outfit is None:
                return False
            state = self.load_state()
            if state.get("outfit_hash"):
                return False
            text = self.current_outfit_text()
            self._save_state(
                {
                    "outfit_hash": outfit_digest(text),
                    "outfit_text": text,
                    "image": outfit.name,
                    "updated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
                    "adopted": True,
                }
            )
            return True

    def needs_outfit_refresh(self) -> bool:
        """是否需要基于人物基础图重新生成当前服装设定图。"""
        with self._lock:
            if self.base_path() is None:
                return False
            self.adopt_existing_if_untracked()
            text = self.current_outfit_text()
            if not text:
                return False
            if self.outfit_path() is None:
                return True
            state = self.load_state()
            return state.get("outfit_hash") != outfit_digest(text)

    def outfit_prompt(self, outfit_text: str | None = None) -> str:
        """基于不可变人物基础设定图生成带新服装设定图的提示词。"""
        raw = outfit_text if outfit_text is not None else self.current_outfit_text()
        desc = normalize_outfit_text(raw)
        return (
            f"角色服装设定图（Character Outfit Sheet）：严格以参考图中的动漫角色"
            f"「{self.name}」为基准，"
            "完整保持参考图的日系二次元动漫五官脸型、轻薄刘海、银白色长发与侧编发、"
            "浅紫罗兰色通透眼眸、身材比例与精致细腻的现代动漫插画画风，不要改变角色相貌与画风。"
            "请完全替换参考图中的素面基础打底衣，根据下面最新的穿搭描述完整设计全新的服装剪裁、"
            f"领口、袖型、裙摆层次、细腻布料质感与随身物：{desc}。"
            "整体保持月白与浅色系的典雅书卷气，与基础打底裙有清晰区分。"
            "构图：左侧为完整全身正面立绘，右侧附一张自然姿态的半身近景展示服装材质与随身物细节；"
            "干净浅灰白棚拍质感背景，自然清透光影与细腻发丝高光，不要水彩涂抹感，不要任何文字、标签或水印。"
        )

    def refresh_outfit(
        self, generator: ImageGenerator, *, now: datetime | None = None
    ) -> Path | None:
        """用人物基础图 + 最新穿搭描述生成新的带服装设定图，并归档旧图。"""
        with self._lock:
            base = self.base_path()
            if base is None:
                return None
            outfit_text = self.current_outfit_text()
            if not outfit_text:
                return None
            ts = now or datetime.now(UTC)
            prompt = self.outfit_prompt(outfit_text)
            generated, revised = generator.generate_image(prompt, [base])
            self.dir.mkdir(parents=True, exist_ok=True)
            ext = generated.suffix.lower() if generated.suffix.lower() in IMAGE_EXTS else ".png"
            target = self.dir / f"outfit{ext}"
            self._archive_existing_outfit(ts)
            tmp = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
            try:
                shutil.copyfile(generated, tmp)
                tmp.replace(target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            # 清理不同后缀的旧 outfit 文件
            for old in self.dir.glob("outfit.*"):
                if old != target and old.suffix.lower() in IMAGE_EXTS:
                    old.unlink(missing_ok=True)
            self._save_state(
                {
                    "outfit_hash": outfit_digest(outfit_text),
                    "outfit_text": outfit_text,
                    "image": target.name,
                    "updated_at": ts.isoformat(timespec="seconds"),
                    "revised_prompt": revised[:400],
                }
            )
            log.info("已根据新穿搭重新生成服装设定图：%s", target.name)
            return target

    def self_draw_reference(
        self, generator: ImageGenerator | None = None, *, now: datetime | None = None
    ) -> Path | None:
        """返回画自己时应使用的参考图（优先最新服装设定图；穿搭变化且有生图器时先自动更新）。"""
        with self._lock:
            self.adopt_existing_if_untracked(now=now)
            if generator is not None and self.needs_outfit_refresh():
                try:
                    refreshed = self.refresh_outfit(generator, now=now)
                    if refreshed is not None:
                        return refreshed
                except (ModelError, OSError) as e:
                    log.warning("自动更新服装设定图失败，先沿用现有参考图：%s", e)
            return self.outfit_path() or self.base_path()

    def status(self) -> dict[str, Any]:
        with self._lock:
            base = self.base_path()
            outfit = self.outfit_path()
            state = self.load_state()
            return {
                "dir": str(self.dir),
                "has_base": base is not None,
                "base_file": base.name if base else None,
                "has_outfit": outfit is not None,
                "outfit_file": outfit.name if outfit else None,
                "needs_refresh": self.needs_outfit_refresh(),
                "updated_at": state.get("updated_at"),
            }

    # ---- 内部 ----

    def _find_image(self, stem: str) -> Path | None:
        if not self.dir.is_dir():
            return None
        for ext in IMAGE_EXTS:
            p = self.dir / f"{stem}{ext}"
            if p.is_file():
                return p
        return None

    def _archive_existing_outfit(self, ts: datetime) -> None:
        existing = self.outfit_path()
        if existing is None:
            return
        self.history_dir.mkdir(parents=True, exist_ok=True)
        stamp = ts.astimezone().strftime("%Y-%m-%d-%H%M")
        dest = self.history_dir / f"outfit-{stamp}{existing.suffix.lower()}"
        if dest.exists():
            dest = self.history_dir / f"outfit-{stamp}-{uuid4().hex[:6]}{existing.suffix.lower()}"
        shutil.copyfile(existing, dest)

    def _save_state(self, state: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.state_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
