"""OneBot 消息段 ↔ 纯文本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_PLACEHOLDERS = {
    "image": "[图片]",
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


@dataclass
class Incoming:
    text: str  # 给模型看的文字
    mentioned_self: bool = False
    reply_to: str | None = None  # 引用的消息 id
    at: list[str] = field(default_factory=list)  # 被 @ 的 QQ 号


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
