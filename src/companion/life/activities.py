"""额度充足时的自主活动。

什么时候做（在记忆核心的线程里判断，见 Life.prepare_activity）：
- 只用 GPT 订阅：订阅暂停（正在用 DeepSeek）时一律不做；
- 5 小时窗口距离重置不到 trigger_minutes_before_reset 分钟、用量又低于 trigger_used_below% 时，
  开始一段“自主时间”；这段时间里最多花掉 budget_percent%（按窗口用量的增加算）；
- 7 天窗口用到 weekly_max_percent% 就不做；两件事之间至少隔 min_gap_minutes 分钟。

怎么做（在后台线程里，见 ActivityRunner.perform）：用单独的 Codex 进程（能联网搜索、能画图），
先按兴趣、今天的安排和心情挑一件事，再去做；结果交回记忆核心记下（见 Companion.finish_activity）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from companion.config import ActivityConfig
from companion.models.codex_app_server import RateLimits, RateWindow
from companion.models.openai_compat import ChatMessage, ChatResult, ModelError

log = logging.getLogger(__name__)

KINDS = ("explore", "write", "draw", "chat_owner")

LIFE_INSTRUCTIONS = """你在陪一个叫“{name}”的 AI 角色度过她的空闲时间：
替她挑事情、查资料、写东西、画画。可以用联网搜索和生图工具；不要执行命令，不要读写文件。
按要求输出；要求 JSON 时只输出 JSON。"""


class LifeBackend(Protocol):
    """做事用的 Codex 进程（测试时换成假的）。"""

    def chat(
        self, messages: list[ChatMessage], *, json_schema: dict[str, Any] | None = None
    ) -> ChatResult: ...

    def generate_image(self, prompt: str) -> tuple[Path, str]: ...

    def close(self) -> None: ...


# ---- 额度 ----


@dataclass
class QuotaSession:
    """一段自主时间：属于哪个 5 小时窗口（按重置时间区分）、开始时用了多少、做了几件。"""

    window: str
    start_used: float
    count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> QuotaSession | None:
        try:
            return cls(**json.loads(raw)) if raw else None
        except (TypeError, ValueError):
            return None


def _windows(limits: RateLimits, *, short: bool) -> RateWindow | None:
    """5 小时窗口（short）或 7 天窗口；同类有好几份时取用得最多的。"""
    day = 24 * 60
    same = [
        w
        for w in limits.windows
        if w.window_minutes is not None and ((w.window_minutes < day) == short)
    ]
    return max(same, key=lambda w: w.used_percent, default=None)


def quota_check(
    limits: RateLimits, session: QuotaSession | None, cfg: ActivityConfig, now: datetime
) -> tuple[QuotaSession | None, str]:
    """返回（可以继续的自主时间，不行时的原因）。"""
    five = _windows(limits, short=True)
    week = _windows(limits, short=False)
    if five is None or five.resets_at is None:
        return None, "读不到 5 小时窗口"
    if week is not None and week.used_percent >= cfg.weekly_max_percent:
        return None, f"7 天额度已用 {week.used_percent:.0f}%"
    key = five.resets_at.isoformat()
    if session is not None and session.window == key:
        spent = five.used_percent - session.start_used
        if spent >= cfg.budget_percent:
            return None, f"这个窗口的自主活动已经花了 {spent:.0f}%"
        if session.count >= cfg.max_per_window:
            return None, f"这个窗口已经做了 {session.count} 件事"
        return session, ""
    left = (five.resets_at - now).total_seconds() / 60
    if left > cfg.trigger_minutes_before_reset:
        return None, f"离 5 小时窗口重置还有 {left:.0f} 分钟"
    if five.used_percent >= cfg.trigger_used_below:
        return None, f"5 小时额度已用 {five.used_percent:.0f}%"
    return QuotaSession(key, five.used_percent), ""


# ---- 做事 ----


@dataclass
class ActivityPrep:
    """挑事情要用的材料（在记忆核心线程里准备好，后台线程只读）。"""

    name: str
    owner_name: str | None
    kinds: list[str]
    context: str
    now: datetime


@dataclass
class ActivityResult:
    kind: str  # explore / write / draw / chat_owner / rest
    topic: str = ""
    why: str = ""
    title: str = ""  # 一句话：做了什么
    text: str = ""  # 笔记、写的东西
    sources: list[str] = field(default_factory=list)
    share: bool = False  # 想跟主人分享
    mood: tuple[str, str] | None = None
    image: Path | None = None
    seconds: float = 0.0


CHOOSE_PROMPT = """你是{name}，一个 AI。现在你有一段空闲时间，可以自己找点事做。请输出 json。
可以选的事（kind）：
{kinds}
- rest：什么都不做，发会儿呆。
按【你的兴趣】【你的今天】【你现在的心情】来选，和【最近做过的事】别重复；
可以从【最近大家在聊的】里找灵感。topic 要具体（比如“雪花为什么是六角形”，而不是“科学”）。
json：{{"kind": "...", "topic": "...", "why": "一句话，为什么想做这个"}}"""

KIND_HELP = {
    "explore": "- explore：上网查一个你此刻真的好奇的东西，或者找点有趣的小事、冷知识",
    "write": "- write：写一首很短的小诗或一小段随笔（topic 写题目）",
    "draw": "- draw：画一张安静的小画（topic 写画面）",
    "chat_owner": "- chat_owner：主动找{owner}聊一个话题（topic 写想聊什么）",
}

CHOOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "topic": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["kind", "topic", "why"],
    "additionalProperties": False,
}

EXPLORE_PROMPT = """你是{name}。你想查一查：{topic}（{why}）。
用联网搜索查一查，挑一两个最有意思、最确定的点，输出 json：
- title：一句话写你做了什么，如“查了雪花为什么是六角形”；
- text：用你自己的口吻写的小笔记，150 字以内，只写查到的、确定的内容；
- sources：参考的网址，一两个；
- share：值不值得随口跟{owner}分享（真的有趣才 true）；
- mood：查完后的心情，写成“词｜原因”；没什么变化就留空。"""

WRITE_PROMPT = """你是{name}，一个安静、喜欢读书的文学少女 AI。请写：{topic}（{why}）。
一首很短的小诗或一小段随笔，300 字以内，用你自己的语气，不要堆砌辞藻。输出 json：
- title：作品的题目；
- text：正文；
- share：想不想拿给{owner}看（写得满意才 true）；
- mood：写完后的心情，写成“词｜原因”；没什么变化就留空。"""

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "text": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "share": {"type": "boolean"},
        "mood": {"type": "string"},
    },
    "required": ["title", "text", "sources", "share", "mood"],
    "additionalProperties": False,
}

DRAW_STYLE = "安静、柔和的插画风格，白色与浅色为主，画面干净，不要文字"


class ActivityRunner:
    def __init__(self, make_backend: Callable[[], LifeBackend] | None) -> None:
        self._make = make_backend
        self._backend: LifeBackend | None = None

    @property
    def available(self) -> bool:
        return self._make is not None

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    def perform(self, prep: ActivityPrep) -> ActivityResult | None:
        """挑一件事并做完（几十秒到几分钟）；出错返回 None。"""
        started = time.monotonic()
        try:
            result = self._perform(prep)
        except ModelError as e:
            log.warning("自主活动没做成：%s", e)
            return None
        except Exception:
            log.exception("自主活动出错")
            return None
        result.seconds = time.monotonic() - started
        log.info("自主活动（%s，%.0f 秒）：%s", result.kind, result.seconds, result.title or "发呆")
        return result

    def _perform(self, prep: ActivityPrep) -> ActivityResult:
        if self._backend is None:
            assert self._make is not None
            self._backend = self._make()
        b = self._backend
        owner = prep.owner_name or "主人"
        kinds = "\n".join(KIND_HELP[k].format(owner=owner) for k in prep.kinds)
        choice = self._json(
            b, CHOOSE_PROMPT.format(name=prep.name, kinds=kinds), prep.context, CHOOSE_SCHEMA
        )
        kind = str(choice.get("kind", "rest"))
        topic = str(choice.get("topic", "")).strip()[:80]
        why = str(choice.get("why", "")).strip()[:80]
        if kind not in prep.kinds or not topic:
            return ActivityResult("rest")
        if kind == "chat_owner":
            return ActivityResult(kind, topic, why, title=f"想和{owner}聊聊{topic}", share=True)
        if kind == "draw":
            path, revised = b.generate_image(f"{topic}。{DRAW_STYLE}")
            return ActivityResult(
                kind, topic, why, title=f"画了{topic}", text=revised[:200], share=True, image=path
            )
        prompt = EXPLORE_PROMPT if kind == "explore" else WRITE_PROMPT
        data = self._json(
            b, prompt.format(name=prep.name, topic=topic, why=why, owner=owner), "", RESULT_SCHEMA
        )
        title = str(data.get("title", "")).strip()[:60] or topic
        if kind == "write":
            title = f"写了《{title.strip('《》')}》"
        return ActivityResult(
            kind,
            topic,
            why,
            title=title,
            text=str(data.get("text", "")).strip()[:600],
            sources=[str(s) for s in data.get("sources") or []][:3],
            share=bool(data.get("share")) and prep.owner_name is not None,
            mood=_mood(data.get("mood")),
        )

    @staticmethod
    def _json(b: LifeBackend, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        messages = [ChatMessage("system", system)]
        if user:
            messages.append(ChatMessage("user", user))
        text = b.chat(messages, json_schema=schema).text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ModelError(f"没有返回合法 JSON：{text[:200]}") from e
        if not isinstance(data, dict):
            raise ModelError("返回的 JSON 不是对象")
        return data


def _mood(raw: Any) -> tuple[str, str] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    word, _, cause = text.replace("|", "｜").partition("｜")
    word = word.strip()[:12]
    return (word, cause.strip()[:60]) if word else None
