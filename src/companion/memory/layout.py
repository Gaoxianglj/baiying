"""记忆文件夹的落盘格式。

    memory/
      manifest.json          格式版本、人格名、派生层由哪些模型生成
      events/YYYY-MM.jsonl   原始事件日志，只追加，唯一真相（按 UTC 月份分文件）
      self/seed.md           种子：只由用户修改
      self/preferences.md    次核心：喜好、穿搭与习惯，每周审视或手动修改
      self/identity.md       自我认知：每周反思更新
      self/style.md          表达风格：每周反思同步更新
      self/journal/          每日反思日记
      people/<person_id>.md  对每个人的了解
      archive/               冷存档：沉睡的记忆
      index.sqlite           派生索引，可随时重建，不进 git

除派生层外，整个文件夹由它自己的 git 仓库做版本记录。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

FORMAT_NAME = "companion-memory"
FORMAT_VERSION = 1

_GITIGNORE = (
    "# 派生层可随时重建，不做版本记录\nindex.sqlite\nindex.sqlite-*\ncache/\n"
    "# 原始事件只追加、不需要版本记录；不进 git 也保证“忘掉这件事”能真正删除\nevents/*.jsonl\n"
    "# 图片体积大、也要能被真正删除\nmedia/\n"
    "# 表情包只记文字信息（index.json），图片本身不进 git\nstickers/*.jpg\nstickers/*.orig.*\n"
    "# 人物设定图与历史穿搭图不进 git（state.json 进 git）\n"
    "self/appearance/*.png\nself/appearance/*.jpg\nself/appearance/*.jpeg\n"
    "self/appearance/*.webp\nself/appearance/history/\n"
)
_SELF_TEMPLATES = ("seed.md", "preferences.md", "identity.md", "style.md")


class MemoryFormatError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryLayout:
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    @property
    def self_dir(self) -> Path:
        return self.root / "self"

    @property
    def journal_dir(self) -> Path:
        return self.self_dir / "journal"

    @property
    def people_dir(self) -> Path:
        return self.root / "people"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def index_path(self) -> Path:
        return self.root / "index.sqlite"

    def person_path(self, person_id: str) -> Path:
        return self.people_dir / f"{person_id}.md"

    def exists(self) -> bool:
        return self.manifest_path.is_file()


def render_template(template: str, **values: str) -> str:
    text = resources.files("companion.memory").joinpath("templates", template).read_text("utf-8")
    return Template(text).safe_substitute(values)


def init_memory(layout: MemoryLayout, persona_name: str, *, git: bool = True) -> list[Path]:
    """创建记忆文件夹。可重复执行：已有文件一律不覆盖。返回新建的文件。"""
    if layout.exists():
        load_manifest(layout)  # 格式不符时报错，而不是在旧数据上继续写

    for d in (layout.events_dir, layout.journal_dir, layout.people_dir, layout.archive_dir):
        d.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []

    def write_new(path: Path, text: str) -> None:
        if not path.exists():
            path.write_text(text, encoding="utf-8", newline="\n")
            created.append(path)

    write_new(layout.manifest_path, _dump_json(_new_manifest(persona_name)))
    for name in _SELF_TEMPLATES:
        write_new(layout.self_dir / name, render_template(name, name=persona_name))
    write_new(layout.root / ".gitignore", _GITIGNORE)
    ensure_gitignore(layout)
    write_new(layout.root / ".gitattributes", "* text=auto eol=lf\n")
    for d in (layout.events_dir, layout.journal_dir, layout.people_dir, layout.archive_dir):
        write_new(d / ".gitkeep", "")

    if git:
        _git_init_and_commit(layout.root, persona_name)
    return created


def ensure_gitignore(layout: MemoryLayout) -> bool:
    """已有记忆文件夹的 .gitignore 不会重新生成：补上后来才有的规则，用户自己加的不动。"""
    path = layout.root / ".gitignore"
    text = path.read_text("utf-8") if path.exists() else ""
    have = set(text.splitlines())
    rules = [line for line in _GITIGNORE.splitlines() if line and not line.startswith("#")]
    missing = [line for line in rules if line not in have]
    if not missing:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n".join(missing) + "\n", encoding="utf-8", newline="\n")
    return True


def load_manifest(layout: MemoryLayout) -> dict[str, Any]:
    try:
        manifest = json.loads(layout.manifest_path.read_text("utf-8"))
    except FileNotFoundError as e:
        raise MemoryFormatError(f"{layout.root} 不是记忆文件夹（缺少 manifest.json）") from e
    if manifest.get("format") != FORMAT_NAME:
        raise MemoryFormatError(f"{layout.manifest_path} 的 format 不是 {FORMAT_NAME}")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise MemoryFormatError(
            f"记忆格式版本 {manifest.get('format_version')} 与程序支持的 {FORMAT_VERSION} 不一致"
        )
    return manifest


def _new_manifest(persona_name: str) -> dict[str, Any]:
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "persona": persona_name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        # 派生层由哪些模型生成；换模型时据此判断要重建什么
        "derived": {"index": None, "embedding_model": None, "extraction_model": None},
    }


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def commit_memory(layout: MemoryLayout, persona_name: str, message: str) -> bool:
    """把记忆文件夹的改动记进它自己的 git；没有 git 仓库或没有改动时什么也不做。"""
    if not (layout.root / ".git").exists() or shutil.which("git") is None:
        return False
    identity = ["-c", f"user.name={persona_name}", "-c", "user.email=companion@localhost"]

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *identity, *args],
            cwd=layout.root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        ).stdout

    run("add", "-A")
    if not run("status", "--porcelain").strip():
        return False
    run("commit", "-m", message)
    return True


def update_manifest(layout: MemoryLayout, **derived: str | None) -> None:
    """记录派生层由哪些模型生成（如 embedding_model）。"""
    manifest = load_manifest(layout)
    changed = False
    for key, value in derived.items():
        if manifest["derived"].get(key) != value:
            manifest["derived"][key] = value
            changed = True
    if changed:
        layout.manifest_path.write_text(_dump_json(manifest), encoding="utf-8", newline="\n")


def _git_init_and_commit(root: Path, persona_name: str) -> None:
    if shutil.which("git") is None:
        raise MemoryFormatError("找不到 git，无法为记忆文件夹做版本记录；可用 git=False 跳过")
    # 记忆的作者是伙伴自己，不依赖本机的 git 身份
    identity = ["-c", f"user.name={persona_name}", "-c", "user.email=companion@localhost"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        # git 输出 UTF-8；中文 Windows 默认按 GBK 解码会出错
        return subprocess.run(
            ["git", *identity, *args],
            cwd=root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )

    if not (root / ".git").exists():
        run("init", "-b", "main")
    run("add", "-A")
    if run("status", "--porcelain").stdout.strip():
        run("commit", "-m", "初始化记忆文件夹")
