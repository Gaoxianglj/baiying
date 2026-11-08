"""收发文件，写新文件。

- 收：别人发来的文件（不超过 max_mb）存进工作文件夹的 inbox/日期/，她看到文件名、大小和存放位置；
  小的文本文件把开头一段直接给她看。助手（Agent）在同一个工作文件夹里，可以接着处理。
- 发：【发文件：路径】发给对方（不超过 max_mb）。能发什么看 skills/access.py：这个聊天的文件夹和
  这个聊天收到的文件谁都能发；主人私聊还能发这台电脑上的其他文件（黑名单除外）。
- 写：【写文件：路径】后面跟 <<<内容>>>，在这个聊天的文件夹里新建文本文件，不改已有的文件。
"""

from __future__ import annotations

import codecs
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from companion.config import FilesSkillConfig
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

if TYPE_CHECKING:
    from companion.skills.access import FileAccess

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


def channel_folder(channel: str) -> str:
    """聊天对应的子文件夹名：private:123 → private-123。"""
    return channel.replace(":", "-")


def _decode_start(raw: bytes) -> str:
    """解开文件开头的一段。末尾可能正好切在一个字中间，用增量解码，半个字留着不报错。"""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        codecs_to_try = ("utf-16",)
    else:
        codecs_to_try = ("utf-8-sig", "gb18030")
    for name in codecs_to_try:
        try:
            return codecs.getincrementaldecoder(name)().decode(raw, final=False)
        except UnicodeDecodeError:
            continue
    return ""


class FileInbox:
    """收到的文件放在哪、怎么告诉她。"""

    def __init__(self, cfg: FilesSkillConfig, root: Path, workspace: Path) -> None:
        self.cfg = cfg
        self.workspace = workspace if workspace.is_absolute() else root / workspace
        self.folder = cfg.inbox if cfg.inbox.is_absolute() else root / cfg.inbox

    @property
    def max_bytes(self) -> int:
        return int(self.cfg.max_mb * 1024 * 1024)

    def save(self, name: str, data: bytes, now: datetime | None, channel: str) -> Path:
        """存进 inbox/日期/聊天/；重名的加序号，不覆盖。按聊天分开，读文件时才分得清是谁的。"""
        day = (now or datetime.now(UTC)).astimezone()
        folder = self.folder / f"{day:%Y-%m-%d}" / channel_folder(channel)
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
        raw = path.read_bytes()
        whole = len(raw) <= self.cfg.preview_chars * 4
        text = _decode_start(raw[: self.cfg.preview_chars * 4])
        if not text:
            return ""
        if len(text) > self.cfg.preview_chars or not whole:
            text = text[: self.cfg.preview_chars].rstrip() + "\n……（后面还有，可以读全文）"
        return text.strip()


class SendFile(Skill):
    name = "files"
    label = "发文件"
    markers = {
        "发文件": "【发文件：路径】把这个聊天的文件夹里的文件、或这个聊天里收到的文件发给对方"
        "（不超过 {mb} MB）；路径写相对的就好。发图片本身用【发图】",
    }
    _local_usage = (
        "在这里（主人私聊）也能【发文件】这台电脑上的其他文件，路径写完整，如 D:/资料/报告.pdf"
    )

    def __init__(self, cfg: FilesSkillConfig, access: FileAccess) -> None:
        self.cfg = cfg
        self.access = access
        self.markers = {k: v.format(mb=f"{cfg.max_mb:g}") for k, v in SendFile.markers.items()}

    def enabled(self) -> bool:
        return self.cfg.enabled

    def allowed(self, ctx: SkillContext) -> bool:
        if self.cfg.folder_enabled:  # 每个聊天都有自己的文件夹可以发
            return True
        return (ctx.owner or not self.cfg.send_owner_only) and (
            ctx.private or self.cfg.send_in_groups
        )

    def status(self) -> str:
        return f"工作文件夹：{self.access.workspace}"

    def context(self, ctx: SkillContext) -> str:
        return self._local_usage if self.access.local(ctx) else ""

    def resolve(self, arg: str, ctx: SkillContext) -> Path:
        """把她写的路径落到能发的地方；越界、不存在、太大都不行。"""
        text = arg.strip().strip("`'\"“”")
        if not text:
            raise SkillError("没说发哪个文件")
        path, member = self.access.locate(text, ctx)
        self.access.check_send(path, ctx)
        if member is not None or not path.is_file():
            raise SkillError(f"没有这个文件：{text}")
        if path.stat().st_size > self.cfg.max_mb * 1024 * 1024:
            raise SkillError(
                f"文件太大了（{human_size(path.stat().st_size)}），"
                f"只能发 {self.cfg.max_mb:g} MB 以内的"
            )
        return path

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        path = self.resolve(arg, ctx)
        return SkillResult(
            f"文件 {path.name}（{human_size(path.stat().st_size)}）会跟着你这句话一起发过去。"
            "简短说一句就好，不要把文件内容再贴一遍。",
            files=[path],
        )


class WriteFile(Skill):
    name = "write_files"
    label = "写文件"
    takes_block = True
    markers = {
        "写文件": "【写文件：路径】在这个聊天的文件夹里新建一个文本文件（如 花册/目录.md），"
        "紧接着写内容：\n<<<\n内容\n>>>\n只能新建，重名会自动加序号；要发出去再用【发文件】",
    }
    _local_usage = (
        "在这里（主人私聊）新文件也能存到别处，路径写完整，如 D:/笔记/周报.md（系统文件夹除外）"
    )

    def __init__(self, cfg: FilesSkillConfig, access: FileAccess) -> None:
        self.cfg = cfg
        self.access = access

    def enabled(self) -> bool:
        return self.cfg.enabled and self.cfg.folder_enabled

    def status(self) -> str:
        return f"存在工作文件夹的 chats/ 下，每个聊天一个文件夹；每个最多 {self.cfg.folder_max_mb:g} MB"

    def context(self, ctx: SkillContext) -> str:
        return self._local_usage if self.access.local(ctx) else ""

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        target, sep, content = arg.partition("｜")
        if not sep or not content.strip():
            raise SkillError("没写内容：路径写在标记里，内容写在紧跟着的 <<< 和 >>> 中间")
        data = content.encode("utf-8")
        if len(data) > self.cfg.max_mb * 1024 * 1024:
            raise SkillError(f"内容太长了，只能写 {self.cfg.max_mb:g} MB 以内")
        name = target.strip()
        if name and not Path(name).suffix:
            name += ".txt"
        path = self.access.save_new(name, data, ctx)
        where = self.access.display(path, ctx)
        return SkillResult(
            f"写好了，存在 {where}（{human_size(len(data))}）。要发给对方就用【发文件：{where}】；"
            "不要把内容再贴一遍。"
        )
