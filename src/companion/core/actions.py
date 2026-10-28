"""回复里的小动作标记：表情回应、戳一戳、用声音说话。

- 【回应：😂】：给对方那条消息点一个表情回应（可以只回应、不说话）；
- 【戳一戳】：戳一下对方（一般是被戳了戳回去）；
- 【语音：要说的话】：这句话用声音发出去。

程序拿掉标记，交给身体去做；身体不支持时（如命令行）就当没写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REACTION_MARK = re.compile(r"[【\[]\s*回应\s*[：:]\s*([^】\]\n]{1,8}?)\s*[】\]]")
POKE_MARK = re.compile(r"[【\[]\s*戳一戳\s*[】\]]")
VOICE_MARK = re.compile(r"[【\[]\s*语音\s*[：:]\s*([^】\]]{1,200}?)\s*[】\]]", re.DOTALL)


@dataclass
class Actions:
    reaction: str | None = None  # 一个表情符号
    poke: bool = False
    voice: str | None = None  # 用声音说的话

    def __bool__(self) -> bool:
        return bool(self.reaction or self.poke or self.voice)


def split_actions(text: str) -> tuple[str, Actions]:
    """拿掉回应、戳一戳、语音的标记；各只认第一个（语音的几段合在一起）。"""
    actions = Actions()
    reactions = [m.group(1).strip() for m in REACTION_MARK.finditer(text)]
    if reactions and emoji_id(reactions[0]):
        actions.reaction = reactions[0]
    actions.poke = bool(POKE_MARK.search(text))
    voices = [m.group(1).strip() for m in VOICE_MARK.finditer(text)]
    if voices:
        actions.voice = "".join(v for v in voices if v) or None
    for mark in (REACTION_MARK, POKE_MARK, VOICE_MARK):
        text = mark.sub("", text)
    return text.strip(), actions


def emoji_id(emoji: str) -> str | None:
    """表情符号 → QQ 表情回应用的编号（第一个字符的码点）；不是表情符号就返回 None。"""
    first = emoji.strip()[:1]
    if not first:
        return None
    cp = ord(first)
    # 表情符号所在的几段：各种符号、箭头、杂项符号与装饰符号，以及 1F000 之后的表情
    if 0x1F000 <= cp <= 0x1FAFF or 0x2190 <= cp <= 0x2BFF:
        return str(cp)
    return None  # 文字、标点、汉字都不算
