"""命令行对话：`companion chat --as <person_id>`。"""

from __future__ import annotations

from companion.config import Config
from companion.core.companion import Companion, Reply
from companion.models.openai_compat import ModelError

HELP = """命令：
  /status            各模型后端的状态与本月花费
  /backend <名字>    auto（默认）/ subscription / fallback / local
  /memory            上一轮想起了哪些记忆
  /extract           立刻把这段对话整理成记忆
  /quit              退出（退出时会自动整理记忆）"""


def run_chat(config: Config, person_id: str, name: str | None, backend: str) -> int:
    with Companion(config) as c:
        c.gateway.prefer = backend  # type: ignore[assignment]
        display = _ensure_person(c, person_id, name)
        channel = f"cli:{person_id}"

        done = c.extract_pending()
        if done:
            print(f"（整理了 {len(done)} 段之前的对话）")
        _print_status(c)
        print(f"和{c.name}聊天吧。输入 /help 查看命令。\n")

        last: Reply | None = None
        while True:
            try:
                text = input("你：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text.startswith("/"):
                cmd, _, arg = text[1:].partition(" ")
                if cmd in ("quit", "exit", "q"):
                    break
                _command(c, cmd, arg.strip(), last, channel)
                continue
            c.extract_pending(channel=channel)  # 之前那段会话若已空闲够久，先整理掉
            try:
                last = c.reply(
                    body="cli",
                    channel=channel,
                    account=person_id,
                    text=text,
                    display_name=display,
                    on_interim=lambda said: print(f"{c.name}：{said}", flush=True),
                    ask=_ask,
                )
            except ModelError as e:
                print(f"（出错了：{e}）")
                continue
            for call in last.skill_calls:
                print(f"  （用了技能 {call.skill}：{call.arg[:60]}，{call.seconds:.0f} 秒）")
            if last.silent:
                print(f"（{c.name}没有回复）\n")
            else:
                extras = "".join(f"\n{c.name}：[图片] {p}" for p in last.images)
                extras += "".join(f"\n{c.name}：[文件] {p}" for p in last.files)
                if last.sticker:
                    extras += (
                        f"\n{c.name}：[表情包·{last.sticker.emotion}] {last.sticker.description}"
                    )
                print(f"{c.name}：{last.text}   [{last.result.backend}]{extras}\n")
            for call in last.later:  # 命令行里直接等它做完
                assert call.later is not None and last.skill_ctx is not None
                print("（后台进行中，请稍候……）", flush=True)
                done = call.later()
                if done.report:
                    text, files = c.report(last.skill_ctx, call.skill, done)
                    print(f"{c.name}：{text}\n")
                    for path in files:
                        print(f"{c.name}：[文件] {path}\n")
                    continue
                for path in c.deliver("cli", channel, done):
                    print(f"{c.name}：[图片] {path}\n")
                if not done.ok:
                    print(f"（{done.text}）\n")

        print("正在整理这次的对话……")
        try:
            done = c.extract_pending(channel=channel, force=True)
            facts = sum(len(x.facts) for x in done)
            print(f"整理完成：{len(done)} 段经历，{facts} 条新事实。")
        except ModelError as e:
            print(f"整理失败（下次启动会重试）：{e}")
    return 0


def _ask(question: str) -> bool:
    answer = input(f"\n{question}\n批准吗？(y/n) ").strip().lower()
    return answer in ("y", "yes", "是", "批准")


def _ensure_person(c: Companion, person_id: str, name: str | None) -> str:
    """命令行账号就是 person_id 本身；第一次使用时建档并绑定。"""
    person = c.people.get(person_id)
    if person is None:
        person = c.people.create(person_id, name or person_id)
    if c.people.resolve("cli", person_id) != person.person_id:
        c.people.bind("cli", person_id, person.person_id)
    return person.names[0]


def _command(c: Companion, cmd: str, arg: str, last: Reply | None, channel: str) -> None:
    if cmd == "help":
        print(HELP)
    elif cmd == "status":
        _print_status(c)
    elif cmd == "backend":
        if arg not in ("auto", "subscription", "fallback", "local"):
            print("可选：auto / subscription / fallback / local")
            return
        c.gateway.prefer = arg  # type: ignore[assignment]
        print(f"（之后优先使用 {arg}）")
    elif cmd == "memory":
        if last is None:
            print("（还没有对话）")
            return
        standing = [c.index.get_fact(fid) for fid in last.bundle.standing_fact_ids]
        print(f"常驻的事实（{len(standing)} 条）：")
        for f in standing:
            if f is not None:
                print(f"  - {f.statement}")
        print(f"检索想起的记忆（{len(last.bundle.recalled)} 条）：")
        for r in last.bundle.recalled:
            mark = "（淡）" if r.faded else ""
            print(f"  {r.score:.2f} [{r.when.astimezone():%Y-%m-%d}] {r.text}{mark}")
    elif cmd == "extract":
        try:
            done = c.extract_pending(channel=channel, force=True)
        except ModelError as e:
            print(f"（整理失败：{e}）")
            return
        print(f"（整理了 {len(done)} 段，{sum(len(x.facts) for x in done)} 条新事实）")
    else:
        print(f"不认识的命令 /{cmd}，输入 /help 查看")


def _print_status(c: Companion) -> None:
    for s in c.gateway.status():
        print(f"  {'●' if s.available else '○'} {s.name}：{s.detail}")
