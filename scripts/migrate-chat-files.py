"""归并历史收件。默认只预览，--apply 才复制；保留原文件、旧路径和迁移回执。"""

import argparse
import json
from pathlib import Path

from companion.config import load_config
from companion.skills.file_migration import migrate_chat_files

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
report = migrate_chat_files(load_config(Path(__file__).resolve().parents[1]), apply=args.apply)
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(2 if report["skipped"] else 0)
