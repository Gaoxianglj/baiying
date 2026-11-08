"""白璎的表情包库：群聊和私聊共用。

- 收藏：她看到群友发的表情包（遵循看图规则）时，主模型同时给出描述、情绪词和“适不适合日常聊天里发”，
  适合的才收进来；同一张图按内容哈希去重，只累加“见过几次”。
- 使用：回复末尾写【表情：情绪词】表示想配一张；按情绪词挑最合适的（同情绪里见得多、用得少的优先）。
- 存储：`stickers/index.json`（文字信息，进 git）+ `stickers/<哈希>.jpg`（图片，不进 git）；
  动图再留一份原图 `stickers/<哈希>.orig.gif`（也不进 git），发出去才会动。
  以前收藏的只有 JPEG，照常能发；有人再发这张时把原图补上。
- 库满时淘汰见得少、很久没见过的。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

STICKER_MARK = re.compile(r"\s*[【\[]表情[:：]\s*([^】\]\n]{1,12})[】\]]\s*")

# 这些格式的多帧才是动画；手机拍的 JPEG（MPO）也有多帧，但不是动图
_ANIMATED_FORMATS = {"GIF", "WEBP", "PNG"}


@dataclass
class Sticker:
    digest: str
    description: str
    emotion: str
    first_seen: str
    last_seen: str
    seen: int = 1  # 见过几次（群友发过几次）
    used: int = 0  # 她自己发过几次
    last_used: str | None = None
    from_people: list[str] = field(default_factory=list)


def split_sticker(text: str) -> tuple[str, str | None]:
    """从回复里拿掉【表情：xx】标记，返回（正文，想要的情绪词）。"""
    emotions = [m.group(1).strip() for m in STICKER_MARK.finditer(text)]
    cleaned = STICKER_MARK.sub(" ", text).strip()
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, (emotions[0] if emotions else None)


def is_animated(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            return img.format in _ANIMATED_FORMATS and bool(getattr(img, "is_animated", False))
    except (OSError, EOFError):
        return False


class StickerBook:
    def __init__(self, root: Path, *, capacity: int = 300) -> None:
        self.root = root
        self.capacity = capacity
        self._lock = threading.RLock()
        self._items: dict[str, Sticker] = {}
        self._load()

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def image_path(self, digest: str) -> Path:
        return self.root / f"{digest}.jpg"

    def original_path(self, digest: str) -> Path | None:
        """动图原图；静态图和以前收藏的没有，就是 None。"""
        paths = sorted(self.root.glob(f"{digest}.orig.*"))
        return next((p for p in paths if p.suffix != ".tmp"), None)

    def best_path(self, digest: str) -> Path:
        """发出去和预览用这个：有动图原图就用原图（才会动），否则用 JPEG。"""
        return self.original_path(digest) or self.image_path(digest)

    def __len__(self) -> int:
        return len(self._items)

    def get(self, digest: str) -> Sticker | None:
        return self._items.get(digest)

    def all(self) -> list[Sticker]:
        with self._lock:
            return sorted(self._items.values(), key=lambda s: s.seen + s.used, reverse=True)

    def emotions(self, limit: int = 20) -> list[str]:
        """库里有的情绪词，按常见程度排序（给模型选）。"""
        counts: dict[str, int] = {}
        with self._lock:
            for s in self._items.values():
                counts[s.emotion] = counts.get(s.emotion, 0) + s.seen + s.used
        return [e for e, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)][:limit]

    def add(
        self,
        image: Path,
        digest: str,
        description: str,
        emotion: str,
        person_id: str | None,
        now: datetime | None = None,
        *,
        original: Path | None = None,
    ) -> Sticker:
        """`image` 是 JPEG；`original` 是原图，只有动图才留下（静态图有 JPEG 就够了）。"""
        now_s = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._keep_original(digest, original)  # 以前收藏的没有原图，再见到时补上
            sticker = self._items.get(digest)
            if sticker is None:
                if not self.image_path(digest).exists():
                    shutil.copyfile(image, self.image_path(digest))
                sticker = Sticker(digest, description, emotion.strip()[:12] or "其他", now_s, now_s)
                self._items[digest] = sticker
                self._evict()
            else:
                sticker.seen += 1
                sticker.last_seen = now_s
            if person_id and person_id not in sticker.from_people:
                sticker.from_people.append(person_id)
            self._save()
            return sticker

    def remove(self, digest: str) -> bool:
        with self._lock:
            if self._items.pop(digest, None) is None:
                return False
            self._delete_files(digest)
            self._save()
            return True

    def pick(
        self,
        emotion: str,
        *,
        embed=None,
        rng: random.Random | None = None,
        min_similarity: float = 0.5,
    ) -> Sticker | None:
        """按情绪词挑一张：先找情绪词相同的；没有就用向量找最像的（需要 embed 函数）。"""
        rng = rng or random.Random()
        with self._lock:
            items = list(self._items.values())
        if not items:
            return None
        same = [s for s in items if s.emotion == emotion or emotion in s.emotion]
        vecs = None
        if not same and embed is not None:
            vecs = embed([emotion] + [f"{s.emotion}：{s.description}" for s in items])
        if vecs is not None:
            sims = vecs[1:] @ vecs[0]
            best = int(np.argmax(sims))
            if float(sims[best]) >= min_similarity:
                same = [items[best]]
        if not same:
            return None
        # 见得多的更“通用”，自己用得多的稍微降权，避免总发同一张
        weights = [(1 + s.seen) / (1 + 0.5 * s.used) for s in same]
        return rng.choices(same, weights=weights, k=1)[0]

    def mark_used(self, digest: str, now: datetime | None = None) -> None:
        with self._lock:
            sticker = self._items.get(digest)
            if sticker is None:
                return
            sticker.used += 1
            sticker.last_used = (now or datetime.now(UTC)).isoformat(timespec="seconds")
            self._save()

    # ---- 内部 ----

    def _keep_original(self, digest: str, original: Path | None) -> None:
        if original is None or self.original_path(digest) is not None or not is_animated(original):
            return
        target = self.root / f"{digest}.orig{original.suffix.lower()}"
        tmp = target.with_suffix(".tmp")
        shutil.copyfile(original, tmp)
        tmp.replace(target)

    def _delete_files(self, digest: str) -> None:
        self.image_path(digest).unlink(missing_ok=True)
        for path in self.root.glob(f"{digest}.orig.*"):
            path.unlink(missing_ok=True)

    def _evict(self) -> None:
        while len(self._items) > self.capacity:
            worst = min(self._items.values(), key=lambda s: (s.seen + 2 * s.used, s.last_seen))
            self._items.pop(worst.digest)
            self._delete_files(worst.digest)

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        data = json.loads(self.index_path.read_text("utf-8"))
        self._items = {d["digest"]: Sticker(**d) for d in data.get("stickers", [])}

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        payload = {"stickers": [asdict(s) for s in self._items.values()]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)
