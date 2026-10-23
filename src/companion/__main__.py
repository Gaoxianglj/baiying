"""命令行入口：`companion` 或 `python -m companion`。"""

import argparse
import json
from pathlib import Path

from companion import __version__
from companion.config import ConfigError, load_config
from companion.memory import (
    MemoryFormatError,
    MemoryLayout,
    PeopleError,
    PeopleRegistry,
    init_memory,
    load_manifest,
)
from companion.models.openai_compat import ModelError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion", description="长期记忆 AI 伙伴")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--root", type=Path, default=None, help="项目根目录（默认当前目录）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("config", help="显示生效的配置（密钥打码）")

    init = sub.add_parser("init", help="创建记忆文件夹（已有文件不会被覆盖）")
    init.add_argument("--no-git", action="store_true", help="不为记忆文件夹做 git 版本记录")

    people = sub.add_parser("people", help="人物表与跨平台身份绑定")
    people_sub = people.add_subparsers(dest="people_command", required=True)
    people_sub.add_parser("list", help="列出所有人和他们的账号")
    add = people_sub.add_parser("add", help="新建一个人")
    add.add_argument("person_id", help="小写字母、数字、- 或 _，如 xiang")
    add.add_argument("--name", required=True, help="显示名，如 小明")
    bind = people_sub.add_parser("bind", help="把某平台账号绑到某人（视为你的确认）")
    bind.add_argument("platform", help="qq / minecraft / ...")
    bind.add_argument("account", help="QQ 号或 MC 玩家名")
    bind.add_argument("person_id")

    chat = sub.add_parser("chat", help="在命令行里和伙伴聊天")
    chat.add_argument("--as", dest="person_id", required=True, help="你的 person_id，如 xiang")
    chat.add_argument("--name", help="第一次使用时的显示名")
    chat.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "subscription", "fallback", "local"],
        help="优先使用的模型后端（默认 auto：订阅 → 兜底 API → 本地）",
    )

    extract = sub.add_parser("extract", help="把已结束的会话整理成记忆")
    extract.add_argument("--force", action="store_true", help="不等空闲，立刻整理所有未整理的对话")

    sub.add_parser("status", help="模型后端状态、本月花费与记忆数量")
    sub.add_parser("qq", help="连接 NapCat，在 QQ 上运行")
    sub.add_parser("sleep", help="立刻做一次“睡眠”：整理、遗忘、合并、反思、写日记")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    try:
        config = load_config(args.root)
        layout = MemoryLayout(config.memory_dir)
        if args.command == "config":
            print(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2))
        elif args.command == "init":
            created = init_memory(layout, config.persona.name, git=not args.no_git)
            print(f"记忆文件夹：{config.memory_dir}")
            print(f"新建 {len(created)} 个文件" if created else "已存在，未改动任何文件")
        elif args.command == "people":
            load_manifest(layout)
            _people(PeopleRegistry(layout), args)
        elif args.command == "chat":
            from companion.chat_cli import run_chat

            return run_chat(config, args.person_id, args.name, args.backend)
        elif args.command == "extract":
            from companion.core.companion import Companion

            with Companion(config) as c:
                done = c.extract_pending(force=args.force)
                facts = sum(len(x.facts) for x in done)
                print(f"整理了 {len(done)} 段会话，{facts} 条新事实")
        elif args.command == "qq":
            from companion.bodies.qq.runner import run_qq

            return run_qq(config)
        elif args.command == "sleep":
            from companion.core.companion import Companion
            from companion.core.sleep import Sleeper

            with Companion(config) as c:
                report = Sleeper(c).run()
            print(
                f"整理 {report.extracted} 段会话；淡忘 {report.faded}、沉睡 {report.dormant}；"
                f"合并 {report.merged} 条重复事实；更新了 {len(report.people_reflected)} 个人的档案"
                + ("、自我认知" if report.identity_reflected else "")
                + (f"；日记：{report.journal}" if report.journal else "")
            )
            for err in report.errors:
                print(f"  出错：{err}")
        elif args.command == "status":
            from companion.core.companion import Companion

            with Companion(config) as c:
                for s in c.gateway.status():
                    print(f"{'●' if s.available else '○'} {s.name}：{s.detail}")
                n = c.index.counts()
                print(
                    f"记忆：{n['episodes']} 段经历，{n['facts']} 条有效事实"
                    f"（另有 {n['superseded_facts']} 条已更新的旧事实），"
                    f"{len(c.people.list())} 个人"
                )
    except (ConfigError, MemoryFormatError, PeopleError, ModelError) as e:
        print(f"错误：{e}")
        return 1
    return 0


def _people(registry: PeopleRegistry, args: argparse.Namespace) -> None:
    if args.people_command == "list":
        people = registry.list()
        if not people:
            print("还没有任何人")
        for person in people:
            accounts = "，".join(
                f"{platform}:{account}"
                for platform, accounts in person.accounts.items()
                for account in accounts
            )
            print(f"{person.person_id}\t{' / '.join(person.names)}\t{accounts or '（无账号）'}")
    elif args.people_command == "add":
        person = registry.create(args.person_id, args.name)
        print(f"已新建：{person.person_id}（{args.name}）")
    elif args.people_command == "bind":
        registry.bind(args.platform, args.account, args.person_id)
        print(f"已绑定：{args.platform}:{args.account} → {args.person_id}")


if __name__ == "__main__":
    raise SystemExit(main())
