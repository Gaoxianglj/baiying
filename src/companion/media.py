"""图片：下载、去重保存、交给主模型看。

看图的结果是一句中文描述，写进事件正文，这样记忆始终是文字，换任何模型都能读懂。
图片统一转成 JPEG、长边缩到上限，按内容哈希命名存在记忆文件夹的 media/ 下（不进 git）。
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage

log = logging.getLogger(__name__)

PROMPT = (
    "用一两句中文客观描述这张图片的内容。图里有文字就原样写出来；"
    "如果是表情包或梗图，说明它想表达的情绪。只输出描述，不要加前缀。"
)


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredImage:
    digest: str  # 内容哈希（前 20 位），也是文件名
    path: Path


class MediaStore:
    def __init__(self, root: Path, *, max_side: int = 1024, max_bytes: int = 10_000_000) -> None:
        self.root = root
        self.max_side = max_side
        self.max_bytes = max_bytes

    def path_for(self, digest: str) -> Path:
        return self.root / f"{digest}.jpg"

    def save(self, data: bytes) -> StoredImage:
        if len(data) > self.max_bytes:
            raise MediaError(f"图片太大（{len(data) // 1024} KB）")
        digest = hashlib.sha256(data).hexdigest()[:20]
        path = self.path_for(digest)
        if path.exists():
            return StoredImage(digest, path)
        try:
            img = Image.open(io.BytesIO(data))
            img.seek(0)  # 动图只取第一帧
            img = img.convert("RGB")
        except (UnidentifiedImageError, OSError, EOFError) as e:
            raise MediaError(f"无法识别的图片格式：{e}") from e
        img.thumbnail((self.max_side, self.max_side))
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=88)
        tmp.replace(path)
        return StoredImage(digest, path)

    def delete(self, digest: str) -> bool:
        path = self.path_for(digest)
        if path.exists():
            path.unlink()
            return True
        return False


def describe(gateway: ModelGateway, image: Path) -> tuple[str, str]:
    """让主模型看图，返回（描述，由哪个后端看的）。失败时抛 ModelError。"""
    result = gateway.chat([ChatMessage("user", PROMPT, images=(image,))], purpose="vision")
    return clean(result.text), result.backend


STICKER_PROMPT = """这是聊天里有人发的表情包。请输出 json：
- description：一两句中文描述画面；有文字就原样写出来。
- emotion：它表达的情绪或态度，用 2~6 个字的中文词，如 开心、无奈、震惊、委屈、得意、疑惑、嫌弃、晚安、点赞。
- usable：适不适合在朋友群的日常聊天里当表情包发。真人的私人照片、聊天截图、含个人信息、不雅或让人不适的内容填 false。"""

STICKER_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "emotion": {"type": "string"},
        "usable": {"type": "boolean"},
    },
    "required": ["description", "emotion", "usable"],
    "additionalProperties": False,
}


def describe_sticker(gateway: ModelGateway, image: Path) -> tuple[str, str, bool]:
    """看表情包：返回（描述，情绪词，是否适合收藏使用）。"""
    data, _ = gateway.chat_json(
        [ChatMessage("user", STICKER_PROMPT, images=(image,))], STICKER_SCHEMA, purpose="vision"
    )
    description = clean(str(data.get("description", "")))
    emotion = clean(str(data.get("emotion", "")), 12) or "其他"
    return description, emotion, bool(data.get("usable"))


def fetch(url: str, *, max_bytes: int, timeout: float = 30.0) -> bytes:
    """下载图片（QQ 的图片链接有时效，收到就下）。"""
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            resp.raise_for_status()
            chunks = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise MediaError("图片太大")
                chunks.append(chunk)
    except httpx.HTTPError as e:
        raise MediaError(f"下载图片失败：{e}") from e
    return b"".join(chunks)


def clean(text: str, limit: int = 200) -> str:
    """压成一行，去掉模型偶尔加的前缀和引号，过长截断。"""
    text = re.sub(r"\s+", " ", text).strip().strip("\"'“”")
    text = re.sub(r"^(图片描述|描述|这张图片)[:：]\s*", "", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
