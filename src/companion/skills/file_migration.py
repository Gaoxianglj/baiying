"""把历史收件复制归并到固定聊天目录，保留旧文件并记录路径映射。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from companion.config import Config
from companion.skills.access import _write_exclusive

CHANNEL = re.compile(r"^(group|private)-(\d+)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REFERENCE = re.compile(r"存在工作文件夹的\s+(.+?)(?:[；\]\r\n]|$)")


def alias_path(root: Path) -> Path:
    return root / "data" / "run" / "file_aliases.json"


def _references(config: Config) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in (config.memory_dir / "events").glob("*.jsonl"):
        with path.open(encoding="utf-8") as file:
            for line in file:
                event = json.loads(line)
                if event.get("actor", {}).get("platform") != "qq":
                    continue
                channel = event.get("channel", "").replace(":", "-")
                if not CHANNEL.fullmatch(channel):
                    continue
                for match in REFERENCE.finditer(event.get("content", "")):
                    key = match.group(1).strip().replace("\\", "/")
                    found.setdefault(key, set()).add(channel)
    return found


def migrate_chat_files(config: Config, *, apply: bool = False) -> dict:
    workspace = config.skills.agent.workspace
    workspace = workspace if workspace.is_absolute() else config.root / workspace
    inbox = config.skills.files.inbox
    inbox = inbox if inbox.is_absolute() else config.root / inbox
    chats = workspace / "chats"
    if not chats.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("聊天目录指向工作目录之外，拒绝归并")
    references = _references(config)
    mapping_file = alias_path(config.root)
    aliases = json.loads(mapping_file.read_text(encoding="utf-8"))["aliases"] \
        if mapping_file.exists() else {}
    rows, skipped = [], []
    for source in sorted(inbox.rglob("*")):
        if not source.is_file():
            continue
        linked = any(p.is_symlink() or p.is_junction()
                     for p in [source, *source.parents]
                     if p != inbox and p.is_relative_to(inbox))
        if linked or not source.resolve().is_relative_to(inbox.resolve()):
            skipped.append({"source": str(source), "reason": "路径是链接或越出收件目录"})
            continue
        parts = source.relative_to(inbox).parts
        if len(parts) >= 3 and DATE.fullmatch(parts[0]) and CHANNEL.fullmatch(parts[1]):
            channel, relative = parts[1], Path(*parts[2:])
        elif len(parts) >= 2 and CHANNEL.fullmatch(parts[0]):
            channel, relative = parts[0], Path(*parts[1:])
        else:
            keys = [source.as_posix()]
            if source.is_relative_to(workspace):
                keys.append(source.relative_to(workspace).as_posix())
            owners = set().union(*(references.get(key, set()) for key in keys))
            if len(owners) != 1:
                skipped.append({"source": str(source), "reason": "收件记录无法唯一确定聊天归属"})
                continue
            channel, relative = owners.pop(), Path(source.name)
        channel_root = chats / channel
        target = channel_root / relative
        if (not channel_root.resolve().is_relative_to(chats.resolve())
                or not target.resolve().is_relative_to(channel_root.resolve())):
            skipped.append({"source": str(source), "reason": "目标越出所属聊天文件夹"})
            continue
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        candidate, suffix = target, 0
        while candidate.exists():
            if not candidate.resolve().is_relative_to(channel_root.resolve()):
                raise ValueError(f"目标路径包含越界链接，归并已停止：{candidate}")
            if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
                break
            suffix += 1
            candidate = target.with_name(f"{target.stem}-{suffix}{target.suffix}")
        copied = not candidate.exists()
        if apply:
            if copied:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate = _write_exclusive(candidate, data)
                stat = source.stat()
                os.utime(candidate, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
                raise ValueError(f"复制后内容校验失败：{source}")
            aliases[source.resolve().as_posix()] = candidate.resolve().as_posix()
        rows.append({"source": str(source), "target": str(candidate), "sha256": digest,
                     "copied": copied, "channel": channel})
    report = {"applied": apply, "at": datetime.now(UTC).isoformat(),
              "files": rows, "skipped": skipped, "old_files_preserved": True}
    if apply:
        mapping_file.parent.mkdir(parents=True, exist_ok=True)
        temp = mapping_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"version": 1, "aliases": aliases}, ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        temp.replace(mapping_file)
        receipt = mapping_file.parent / "file-migrations" / (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["receipt"] = str(receipt)
    return report
