"""命令行入口：`companion` 或 `python -m companion`。"""

import argparse
import json
from pathlib import Path

from companion import __version__
from companion.config import ConfigError, load_config
from companion.memory import MemoryFormatError, MemoryLayout, init_memory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion", description="长期记忆 AI 伙伴")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--root", type=Path, default=None, help="项目根目录（默认当前目录）")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("config", help="显示生效的配置（密钥打码）")
    init = sub.add_parser("init", help="创建记忆文件夹（已有文件不会被覆盖）")
    init.add_argument("--no-git", action="store_true", help="不为记忆文件夹做 git 版本记录")
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        config = load_config(args.root)
        if args.command == "config":
            print(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2))
        elif args.command == "init":
            created = init_memory(
                MemoryLayout(config.memory_dir), config.persona.name, git=not args.no_git
            )
            print(f"记忆文件夹：{config.memory_dir}")
            print(f"新建 {len(created)} 个文件" if created else "已存在，未改动任何文件")
    except (ConfigError, MemoryFormatError) as e:
        print(f"错误：{e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
