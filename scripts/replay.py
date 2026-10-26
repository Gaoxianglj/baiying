"""回放对比：拿一批真实聊天，用现在的提示词重新生成回复，和她当时说的并排对比。

用途：调人设、调提示词（比如情感部分）之后，用同一批真实消息看看回复有没有变好；
也可以对比 DeepSeek 开 / 关思考模式（--thinking both）。

只读：不往记忆里写任何东西（不记录消息、不记“被回忆”、不记心情）。
注意：检索用的是现在的记忆，可能包含那条消息之后才形成的记忆。

用法：
  uv run python scripts/replay.py                       # 最近 20 轮，默认后端
  uv run python scripts/replay.py --limit 30 --backend fallback --thinking both
  uv run python scripts/replay.py --channel group:123456 --days 3
结果写到 data/replay/<时间>.md。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion.config import load_config
from companion.core.companion import SILENT_MARK, Companion
from companion.life import split_mood
from companion.memory.events import Event
from companion.memory.people import SELF_ID
from companion.models.openai_compat import ModelError
from companion.stickers import split_sticker

ROOT = Path(__file__).resolve().parents[1]
REPLY_WITHIN = timedelta(minutes=3)  # 她在这么久内接了话，才算“回复了这条”


@dataclass
class Turn:
    incoming: Event
    original: Event


def find_turns(c: Companion, *, channel: str | None, days: float, limit: int) -> list[Turn]:
    """别人说了话、她在几分钟内接了的那些轮（只看文字消息）。"""
    since = datetime.now(UTC) - timedelta(days=days)
    by_channel: dict[str, list[Event]] = {}
    for e in c.log.iter_events(since=since):
        if e.kind == "message" and (channel is None or e.channel == channel):
            by_channel.setdefault(e.channel, []).append(e)
    turns = []
    for events in by_channel.values():
        events.sort(key=lambda e: e.ts)
        for prev, nxt in zip(events, events[1:], strict=False):
            if (
                prev.actor.person_id != SELF_ID
                and nxt.actor.person_id == SELF_ID
                and nxt.ts - prev.ts <= REPLY_WITHIN
                and not nxt.content.startswith("[")
                and not nxt.meta.get("proactive")
            ):
                turns.append(Turn(prev, nxt))
    turns.sort(key=lambda t: t.incoming.ts)
    return turns[-limit:]


def regenerate(c: Companion, turn: Turn) -> tuple[str, str]:
    """按那条消息发出时的样子重新组装上下文并生成；返回（回复，备注）。"""
    e = turn.incoming
    assert e.actor.person_id is not None
    mode = "required" if e.channel.startswith("private:") else "optional"
    bundle = c.context.build(
        person_id=e.actor.person_id,
        body=e.body,
        channel=e.channel,
        query=e.content,
        now=e.ts,
        sticker_emotions=c.stickers.emotions() if len(c.stickers) >= 3 else [],
        reply_mode=mode,
    )
    started = time.monotonic()
    try:
        result = c.gateway.chat(bundle.messages, purpose="replay")
    except ModelError as err:
        return "", f"失败：{err}"
    text, mood = split_mood(c.skills.strip_markers(result.text))
    text, sticker = split_sticker(text)
    notes = [f"{result.backend} {time.monotonic() - started:.1f}s"]
    if mood:
        notes.append(f"心情：{mood[0]}" + (f"｜{mood[1]}" if mood[1] else ""))
    if sticker:
        notes.append(f"表情：{sticker}")
    if SILENT_MARK.search(text):
        text = "（不回复）"
    return text.strip(), "；".join(notes)


def main() -> int:
    parser = argparse.ArgumentParser(description="用现在的提示词重放真实聊天，并排对比")
    parser.add_argument("--channel", help="只看某个频道，如 group:123 或 private:456")
    parser.add_argument("--days", type=float, default=7, help="从最近几天里找（默认 7）")
    parser.add_argument("--limit", type=int, default=20, help="最多几轮（默认 20）")
    parser.add_argument("--backend", choices=["auto", "subscription", "fallback"], default="auto")
    parser.add_argument(
        "--thinking",
        choices=["off", "on", "both"],
        default="off",
        help="DeepSeek 思考模式（只对 fallback 有效）；both = 开关各生成一遍",
    )
    args = parser.parse_args()

    config = load_config(ROOT)
    variants = {"off": [False], "on": [True], "both": [False, True]}[args.thinking]
    columns: list[tuple[str, list[tuple[str, str]]]] = []
    turns: list[Turn] = []
    for thinking in variants:
        cfg = config.model_copy(deep=True)
        if args.backend == "fallback" or args.thinking != "off":
            mode = "enabled" if thinking else "disabled"
            cfg.models.fallback.extra_body = {"thinking": {"type": mode}}
        with Companion(cfg) as c:
            c.gateway.prefer = "fallback" if args.thinking != "off" else args.backend
            if not turns:
                turns = find_turns(c, channel=args.channel, days=args.days, limit=args.limit)
                if not turns:
                    print("没找到可以回放的对话。")
                    return 1
                print(f"回放 {len(turns)} 轮……")
            label = "现在" + (
                "（思考开）" if thinking else "（思考关）" if len(variants) > 1 else ""
            )
            outputs = []
            for i, turn in enumerate(turns, 1):
                outputs.append(regenerate(c, turn))
                print(f"  {label} {i}/{len(turns)}")
            columns.append((label, outputs))

    out_dir = config.data_dir / "replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.md"
    lines = [
        f"# 回放对比 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"{len(turns)} 轮；后端：{args.backend}；思考模式：{args.thinking}。"
        "检索用的是现在的记忆，可能含那条消息之后才形成的记忆。",
        "",
    ]
    for i, turn in enumerate(turns):
        e = turn.incoming
        who = e.actor.display_name or e.actor.platform_id
        lines += [
            f"## {i + 1}. {e.ts.astimezone():%m-%d %H:%M} · {e.channel}",
            "",
            f"**{who}**：{e.content}",
            "",
            f"- 当时：{turn.original.content}",
        ]
        for label, outputs in columns:
            text, note = outputs[i]
            lines.append(f"- {label}：{text or '（空）'}  〔{note}〕")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"写好了：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
