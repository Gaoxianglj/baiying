"""OneBot 消息段 ↔ 纯文本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_PLACEHOLDERS = {
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[聊天记录]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


@dataclass(frozen=True)
class ImageRef:
    url: str | None
    file: str | None  # NapCat 的文件标识，没有 url 时可用 get_image 换取
    sticker: bool  # 收藏表情 / 动画表情（不是普通照片）


def image_marker(i: int) -> str:
    """正文里第 i 张图的占位符，看完图后替换成描述。"""
    return f"⟦图{i}⟧"


@dataclass
class Incoming:
    text: str  # 给模型看的文字（图片处是占位符）
    mentioned_self: bool = False
    reply_to: str | None = None  # 引用的消息 id
    at: list[str] = field(default_factory=list)  # 被 @ 的 QQ 号
    images: list[ImageRef] = field(default_factory=list)

    def fill_images(self, descriptions: list[str]) -> str:
        text = self.text
        for i, desc in enumerate(descriptions):
            text = text.replace(image_marker(i), desc)
        return text


def parse_segments(message: Any, self_id: str, names: dict[str, str] | None = None) -> Incoming:
    """`names`：QQ 号 → 显示名，用来把 @ 换成名字。"""
    if isinstance(message, str):  # 少数实现用 CQ 码字符串，这里只保留文字
        return Incoming(text=message.strip())
    names = names or {}
    parts: list[str] = []
    out = Incoming(text="")
    for seg in message or []:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            qq = str(data.get("qq", ""))
            if qq == self_id:
                out.mentioned_self = True
                continue  # @她 本身不进正文
            if qq == "all":
                parts.append("@全体成员 ")
                continue
            out.at.append(qq)
            parts.append(f"@{names.get(qq) or data.get('name') or qq} ")
        elif kind == "reply":
            out.reply_to = str(data.get("id", "")) or None
        elif kind == "image":
            summary = str(data.get("summary") or "")
            out.images.append(
                ImageRef(
                    url=data.get("url") or None,
                    file=data.get("file") or None,
                    sticker=str(data.get("sub_type", "0")) == "1" or "表情" in summary,
                )
            )
            parts.append(image_marker(len(out.images) - 1))
        else:
            parts.append(_PLACEHOLDERS.get(str(kind), ""))
    out.text = "".join(parts).strip()
    return out


def text_segments(text: str, reply_to: str | None = None) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    if reply_to:
        segs.append({"type": "reply", "data": {"id": reply_to}})
    segs.append({"type": "text", "data": {"text": text}})
    return segs
