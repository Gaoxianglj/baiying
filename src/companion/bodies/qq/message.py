"""OneBot 消息段 ↔ 纯文本。"""

from __future__ import annotations

import re
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

FORWARD_UNREAD = "[聊天记录（没看）]"  # 消息里没带内容、向 NapCat 也没要到
FORWARD_MAX_LINES = 30
FORWARD_MAX_CHARS = 1500
_NESTED_MAX_LINES = 10  # 记录里套着的记录：只展开这一层，篇幅也更小
_NESTED_MAX_CHARS = 500
_FORWARD_LINE_CHARS = 200  # 每行最多这么多字，免得一条长消息占满篇幅

_QQ_NUMBER = r"\d{5,12}(?!\d)"
_NO_MENTION = {"全体成员", "all"}  # 她发的话里不会变成真正的 @


@dataclass(frozen=True)
class ImageRef:
    url: str | None
    file: str | None  # NapCat 的文件标识，没有 url 时可用 get_image 换取
    sticker: bool  # 收藏表情 / 动画表情（不是普通照片）


@dataclass(frozen=True)
class ForwardRef:
    """一段合并转发的聊天记录。"""

    id: str | None  # 没带内容时用它向 NapCat 要（get_forward_msg）
    nodes: list[dict[str, Any]] | None  # NapCat 通常直接带上每条消息


def image_marker(i: int) -> str:
    """正文里第 i 张图的占位符，看完图后替换成描述。"""
    return f"⟦图{i}⟧"


def forward_marker(i: int) -> str:
    """正文里第 i 段聊天记录的占位符，展开后替换成记录内容。"""
    return f"⟦记录{i}⟧"


@dataclass
class Incoming:
    text: str  # 给模型看的文字（图片、聊天记录处是占位符）
    mentioned_self: bool = False
    reply_to: str | None = None  # 引用的消息 id
    at: list[str] = field(default_factory=list)  # 被 @ 的 QQ 号
    images: list[ImageRef] = field(default_factory=list)
    forwards: list[ForwardRef] = field(default_factory=list)

    def fill(self, images: list[str], forwards: list[str] | None = None) -> str:
        """把占位符换成看图结果和展开的聊天记录；没给记录内容的记作“[聊天记录]”。"""
        text = self.text
        for i, desc in enumerate(images):
            text = text.replace(image_marker(i), desc)
        forwards = forwards or []
        for i in range(len(self.forwards)):
            body = forwards[i] if i < len(forwards) else _PLACEHOLDERS["forward"]
            text = text.replace(forward_marker(i), body)
        return text


def parse_segments(message: Any, self_id: str, names: dict[str, str] | None = None) -> Incoming:
    """`names`：QQ 号 → 显示名，用来把 @ 换成名字。"""
    return _parse(message, self_id, names or {}, depth=0)


def _parse(message: Any, self_id: str, names: dict[str, str], depth: int) -> Incoming:
    """`depth`：0 是收到的消息本身，1 是聊天记录里的一条，2 是记录里套着的记录里的一条。"""
    if isinstance(message, str):  # 少数实现用 CQ 码字符串，这里只保留文字
        return Incoming(text=message.strip())
    parts: list[str] = []
    out = Incoming(text="")
    for seg in message or []:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            text = str(data.get("text", ""))
            if depth:  # 记录里的一条消息压成一行
                text = re.sub(r"\s*\n\s*", " ", text)
            parts.append(text)
        elif kind == "at":
            qq = str(data.get("qq", ""))
            if qq == self_id:
                if depth:  # 记录里 @她 的是当时的事，不算现在叫她
                    parts.append("@你 ")
                    continue
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
            ref = ImageRef(
                url=data.get("url") or None,
                file=data.get("file") or None,
                sticker=str(data.get("sub_type", "0")) == "1" or "表情" in summary,
            )
            if depth:  # 记录里的图不下载、不看
                parts.append(f"[{'表情包' if ref.sticker else '图片'}（没看）]")
                continue
            out.images.append(ref)
            parts.append(image_marker(len(out.images) - 1))
        elif kind == "forward":
            content = data.get("content")
            nodes = content if isinstance(content, list) and content else None
            if depth == 0:  # 留个占位符，由 QQ 身体展开（必要时向 NapCat 要内容）
                out.forwards.append(ForwardRef(id=str(data.get("id") or "") or None, nodes=nodes))
                parts.append(forward_marker(len(out.forwards) - 1))
            elif depth == 1 and nodes:
                parts.append(
                    render_forward(
                        nodes,
                        self_id,
                        depth=2,
                        max_lines=_NESTED_MAX_LINES,
                        max_chars=_NESTED_MAX_CHARS,
                    )
                )
            else:
                parts.append(_PLACEHOLDERS["forward"])
        else:
            parts.append(_PLACEHOLDERS.get(str(kind), ""))
    out.text = "".join(parts).strip()
    return out


def render_forward(
    nodes: list[dict[str, Any]],
    self_id: str,
    *,
    depth: int = 1,
    max_lines: int = FORWARD_MAX_LINES,
    max_chars: int = FORWARD_MAX_CHARS,
) -> str:
    """把合并转发的聊天记录写成“名字：内容”，一条一行；超出篇幅的只说还剩几条。

    每条按 NapCat 的格式（和消息事件一样：sender、message）；也认 go-cqhttp 的 content。
    不写原来是哪个群的消息。
    """
    lines: list[str] = []
    size = shown = 0
    for node in nodes:
        sender = node.get("sender") or {}
        uid = str(sender.get("user_id") or node.get("user_id") or "")
        who = (
            "你"
            if uid and uid == self_id
            else str(sender.get("card") or sender.get("nickname") or uid or "某人")
        )
        body = _parse(node.get("message", node.get("content")), self_id, {}, depth).text
        entry = [_clip(line, _FORWARD_LINE_CHARS) for line in f"{who}：{body or '……'}".split("\n")]
        cost = sum(len(line) + 1 for line in entry)
        if lines and (len(lines) + len(entry) > max_lines or size + cost > max_chars):
            break
        lines += entry
        size += cost
        shown += 1
    if shown < len(nodes):
        lines.append(f"……（后面还有 {len(nodes) - shown} 条）")
    return f"（转发了一段聊天记录，共 {len(nodes)} 条：\n" + "\n".join(lines) + "）"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_segments(
    text: str,
    reply_to: str | None = None,
    *,
    mentions: dict[str, str] | None = None,
    self_id: str = "",
) -> list[dict[str, Any]]:
    """她要发的话 → 消息段。

    `mentions`（只在群聊给）：最近在群里说过话的人，名字 → QQ 号。给了就把“@QQ号”和
    “@名字”（名字一字不差，长的优先）换成真正的 @，好让别的机器人认出指令；
    不认识的名字照原样发，也不会 @全体成员 或 @她自己。
    """
    segs: list[dict[str, Any]] = []
    if reply_to:
        segs.append({"type": "reply", "data": {"id": reply_to}})
    if mentions is None:
        segs.append({"type": "text", "data": {"text": text}})
        return segs
    names = {
        name: qq
        for name, qq in mentions.items()
        if name and qq.isdigit() and qq != self_id and name not in _NO_MENTION
    }
    alternatives = [re.escape(n) for n in sorted(names, key=len, reverse=True)]
    # “@”前面紧挨着字母数字的（如邮箱）不算
    pattern = re.compile(r"(?<![A-Za-z0-9_.])@(" + "|".join([*alternatives, _QQ_NUMBER]) + ")")
    pos = 0
    for m in pattern.finditer(text):
        target = m.group(1)
        qq = names.get(target, target)
        if qq == self_id:
            continue
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": text[pos : m.start()]}})
        segs.append({"type": "at", "data": {"qq": qq}})
        pos = m.end()
    if pos < len(text) or len(segs) == (1 if reply_to else 0):
        segs.append({"type": "text", "data": {"text": text[pos:]}})
    return segs
