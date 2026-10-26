"""心情：她在回复末尾写【心情：词｜原因】表示心情变了；之后随时间回落到当天的基调。

- 心情是跨场合的：群里被逗乐了，私聊里还带着一点；
- 强度按半衰期衰减（难过、生气这类回落得更快），低于门槛就回到基调；
- 新心情直接替换旧的，不叠加，所以谁也没法把她的情绪一路推到极端；
- 隐私：起因来自别的私聊时，只说“因为一件私下的事”；来自别的群时，只说“另一个群里的事”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MOOD_MARK = re.compile(
    r"[【\[]\s*心情\s*[：:]\s*([^｜|】\]\n]{1,16}?)\s*(?:[｜|]\s*([^】\]\n]{0,80}?))?\s*[】\]]"
)

NEGATIVE = (
    "难过", "伤心", "生气", "委屈", "烦", "低落", "失落", "郁闷", "不开心", "害怕", "焦虑",
    "累", "无奈", "尴尬", "沮丧", "孤单", "寂寞", "失望", "紧张", "不爽", "恼", "怕", "哭",
    "心酸", "闷", "慌", "愧", "疚",
)  # fmt: skip

STRONG = 0.6  # 强度高于它算“现在正……”，低于它是“还有一点……”
FLOOR = 0.25  # 低于它就算回到基调了

RULE = (
    "- 如果这轮对话让你的心情明显变了，就在回复最后单独写【心情：一两个词｜一句话原因】，"
    "比如【心情：小得意｜画被夸好看】；没变化就不写，大多数时候都不用写。你性子温和，心情起伏轻一点。"
)


def split_mood(text: str) -> tuple[str, tuple[str, str] | None]:
    """拿掉【心情：…】标记，返回（正文，(心情词, 原因)）；写了几个只认最后一个。"""
    found = list(MOOD_MARK.finditer(text))
    cleaned = MOOD_MARK.sub("", text).strip()
    if not found:
        return cleaned, None
    m = found[-1]
    word = m.group(1).strip()[:12]
    cause = (m.group(2) or "").strip()[:60]
    return cleaned, (word, cause) if word else None


def is_negative(word: str) -> bool:
    return any(n in word for n in NEGATIVE)


@dataclass(frozen=True)
class MoodNow:
    word: str
    cause: str
    strength: float  # 0~1
    channel: str
    at: datetime


def current_mood(
    entries: list[dict[str, Any]],
    now: datetime,
    *,
    half_life_minutes: float,
    negative_half_life_minutes: float,
) -> MoodNow | None:
    """最近一次心情变化衰减到现在还剩多少；回到基调了就返回 None。"""
    parsed = []
    for e in entries:
        try:
            parsed.append((datetime.fromisoformat(e["ts"]), e))
        except (KeyError, TypeError, ValueError):
            continue
    past = [(ts, e) for ts, e in parsed if ts <= now]
    if not past:
        return None
    ts, e = max(past, key=lambda p: p[0])
    word = str(e.get("word", "")).strip()
    if not word:
        return None
    half = negative_half_life_minutes if is_negative(word) else half_life_minutes
    minutes = (now - ts).total_seconds() / 60
    strength = 0.5 ** (minutes / max(half, 1.0))
    if strength < FLOOR:
        return None
    return MoodNow(word, str(e.get("cause", "")), strength, str(e.get("channel", "")), ts)


def render_mood(mood: MoodNow | None, baseline: str | None, channel: str) -> str:
    if mood is None and not baseline:
        return ""
    if mood is None:
        text = f"平平常常，就是今天的基调：{baseline}。"
    else:
        cause = _cause(mood, channel)
        text = ("" if mood.strength >= STRONG else "还有一点") + mood.word
        text += f"（{cause}）" if cause else ""
        if mood.strength < STRONG and baseline:
            text += f"，正慢慢回到今天的基调：{baseline}"
        text += "。"
    return (
        "【你现在的心情】\n"
        + text
        + "\n心情会自然地影响你的语气和要不要配表情包，但不用说出来，被问到时再说。"
    )


def _cause(mood: MoodNow, channel: str) -> str:
    if not mood.cause:
        return ""
    if mood.channel == channel or mood.channel.startswith("self:"):
        return mood.cause if mood.cause.startswith("因为") else "因为" + mood.cause
    if mood.channel.startswith("group:"):
        return "因为另一个群里的事"
    return "因为一件私下的事"
