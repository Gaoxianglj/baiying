"""收发文件。

- 收：别人发来的文件（不超过 max_mb）存进工作文件夹的 inbox/日期/，她看到文件名、大小和存放位置；
  小的文本文件把开头一段直接给她看。助手（Agent）在同一个工作文件夹里，可以接着处理。
- 发：【发文件：路径】把工作文件夹里的文件发给对方（不超过 max_mb）。路径只能落在工作文件夹里，
  默认只给主人发、只在私聊里发（工作文件夹里多是替主人做的东西）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from companion.config import FilesSkillConfig
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".log", ".py", ".js", ".ts", ".html", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".srt", ".lrc",
}  # fmt: skip
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size / 1024 / 1024:.1f}MB"


def safe_name(name: str) -> str:
    """去掉路径和 Windows 不允许的字符；太长的截短（保留扩展名）。"""
    name = _UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
    if not name:
        return "文件"
    stem, suffix = Path(name).stem, Path(name).suffix[:16]
    return stem[:80] + suffix


class FileInbox:
    """收到的文件放在哪、怎么告诉她。"""

    def __init__(self, cfg: FilesSkillConfig, root: Path, workspace: Path) -> None:
        self.cfg = cfg
        self.workspace = workspace if workspace.is_absolute() else root / workspace
        self.folder = cfg.inbox if cfg.inbox.is_absolute() else root / cfg.inbox

    @property
    def max_bytes(self) -> int:
        return int(self.cfg.max_mb * 1024 * 1024)

    def save(self, name: str, data: bytes, now: datetime | None = None) -> Path:
        """存进 inbox/日期/；重名的加序号，不覆盖。"""
        day = (now or datetime.now(UTC)).astimezone()
        folder = self.folder / f"{day:%Y-%m-%d}"
        folder.mkdir(parents=True, exist_ok=True)
        clean = safe_name(name)
        path = folder / clean
        n = 1
        while path.exists():
            path = folder / f"{Path(clean).stem}-{n}{Path(clean).suffix}"
            n += 1
        path.write_bytes(data)
        return path

    def describe(self, path: Path, name: str) -> str:
        """写进消息里给她看：文件名、大小、存在哪；文本文件附上开头一段。"""
        size = path.stat().st_size
        where = self._relative(path)
        text = f"[文件：{name}（{human_size(size)}），存在工作文件夹的 {where}"
        preview = self._preview(path)
        if preview:
            text += f"；开头的内容：\n{preview}\n"
        return text + "]"

    def skipped(self, name: str, size: int | None, why: str) -> str:
        size_text = f"{human_size(size)}，" if size else ""
        return f"[文件：{name}（{size_text}{why}）]"

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _preview(self, path: Path) -> str:
        if path.suffix.lower() not in TEXT_SUFFIXES or self.cfg.preview_chars <= 0:
            return ""
        raw = path.read_bytes()[: self.cfg.preview_chars * 4]
        for encoding in ("utf-8", "gbk"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return ""
        if len(text) > self.cfg.preview_chars:
            text = text[: self.cfg.preview_chars] + "\n……（后面还有）"
        return text.strip()


class SendFile(Skill):
    name = "files"
    label = "发文件"
    markers = {
        "发文件": "【发文件：工作文件夹里的路径】把工作文件夹里的一个文件发给对方"
        "（比如助手做好的表格、收到后整理过的文档），不超过 {mb} MB",
    }

    def __init__(self, cfg: FilesSkillConfig, root: Path, workspace: Path) -> None:
        self.cfg = cfg
        self.workspace = workspace if workspace.is_absolute() else root / workspace
        self.markers = {k: v.format(mb=f"{cfg.max_mb:g}") for k, v in SendFile.markers.items()}

    def enabled(self) -> bool:
        return self.cfg.enabled

    def allowed(self, ctx: SkillContext) -> bool:
        if self.cfg.send_owner_only and not ctx.owner:
            return False
        return ctx.private or self.cfg.send_in_groups

    def status(self) -> str:
        return f"工作文件夹：{self.workspace}"

    def resolve(self, arg: str) -> Path:
        """把她写的路径落到工作文件夹里；越界、不存在、太大都不行。"""
        text = arg.strip().strip("`'\"“”")
        if not text:
            raise SkillError("没说发哪个文件")
        base = self.workspace.resolve()
        path = Path(text)
        path = (path if path.is_absolute() else base / path).resolve()
        if not path.is_relative_to(base):
            raise SkillError("只能发工作文件夹里的文件")
        if not path.is_file():
            raise SkillError(f"工作文件夹里没有这个文件：{text}")
        if path.stat().st_size > self.cfg.max_mb * 1024 * 1024:
            raise SkillError(
                f"文件太大了（{human_size(path.stat().st_size)}），"
                f"只能发 {self.cfg.max_mb:g} MB 以内的"
            )
        return path

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        path = self.resolve(arg)
        return SkillResult(
            f"文件 {path.name}（{human_size(path.stat().st_size)}）会跟着你这句话一起发过去。"
            "简短说一句就好，不要把文件内容再贴一遍。",
            files=[path],
        )
