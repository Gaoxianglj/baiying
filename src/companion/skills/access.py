"""谁能碰哪些文件：读、发、存都走这里，规则只有一份。路径都解析成真实路径后再判断。

- 每个聊天有自己的文件夹（工作文件夹的 chats/聊天/）：这个聊天里能存新文件、能读、能发给任何人。
- 收到的文件（inbox/日期/聊天/）：只在收到它的聊天里能读、能发。
- 主人私聊：还能读、发这台电脑上的其他文件；新文件也能存到别的地方（系统和程序的文件夹除外）。
- 群聊：另外可以读控制台指定的文件夹（group_read_paths，只读不发）。
- 黑名单（密钥、记忆、登录信息、浏览器和密码管理器数据）和看起来像密钥的文件，谁都不行。
- 任何地方都不改已有文件：只新建，重名加序号。可执行的文件类型不能写。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

from companion.config import FilesSkillConfig
from companion.skills.base import SkillContext, SkillError
from companion.skills.files import channel_folder, human_size, safe_name

# 不管在哪个目录，这些名字都不读：密钥、证书、登录信息
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", ".git-credentials", ".netrc", ".npmrc", ".pypirc", "credentials*", "auth.json",
    "*token*", "*secret*", "*password*", "*密码*", "*密钥*", "cookies*", "login data",
    "ntuser.dat", "sam", "system", "security",
)  # fmt: skip
# 双击就会运行、或系统会自动加载的文件类型，不能写（真要做脚本交给助手，助手有沙箱和审批）
RUNNABLE_SUFFIXES = {
    ".bat", ".cmd", ".com", ".exe", ".scr", ".pif", ".msi", ".msp", ".dll", ".cpl", ".sys",
    ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".reg",
    ".lnk", ".url", ".scf", ".inf", ".jar", ".appref-ms", ".settingcontent-ms", ".library-ms",
}  # fmt: skip
RUNNABLE_NAMES = {"desktop.ini", "autorun.inf"}
# 主人私聊存新文件时也不碰这些地方：系统、程序和开机启动项都在里面
SYSTEM_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "system volume information",
}  # fmt: skip
_DEVICE_NAME = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_DEPTH = 6  # 聊天文件夹里最多套几层


def _clean_path(text: str) -> str:
    """她写的路径：去掉引号、统一斜杠；网络路径和 NTFS 备用数据流直接拒绝。"""
    raw = text.strip().strip("`'\"“”").replace("\\", "/")
    if raw.startswith("//") or ":" in PureWindowsPath(raw).as_posix()[2:]:
        raise SkillError("这个路径不能读")
    if any(_DEVICE_NAME.match(part) for part in PureWindowsPath(raw).parts):
        raise SkillError("这个名字不能用")  # CON、NUL 这些是设备，读它们会一直卡住
    return raw


def split_zip(path: Path) -> tuple[Path, str | None]:
    """`x.zip/a/b.md` 拆成压缩包和里面的路径。"""
    for parent in [path, *path.parents]:
        if parent.suffix.lower() == ".zip" and parent.is_file():
            inner = path.relative_to(parent).as_posix()
            return parent, None if inner == "." else inner
    return path, None


class FileAccess:
    def __init__(
        self,
        cfg: FilesSkillConfig,
        root: Path,
        workspace: Path,
        deny: Callable[[], list[Path]],
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.workspace = workspace if workspace.is_absolute() else root / workspace
        self.inbox = cfg.inbox if cfg.inbox.is_absolute() else root / cfg.inbox
        self._deny = deny

    # ---- 位置 ----

    @property
    def chats(self) -> Path:
        return self.workspace / "chats"

    def folder(self, channel: str) -> Path:
        return self.chats / channel_folder(channel)

    def local(self, ctx: SkillContext) -> bool:
        """主人私聊：这台电脑上的文件也能碰。"""
        return self.cfg.read_local and ctx.owner and ctx.private

    def locate(self, text: str, ctx: SkillContext) -> tuple[Path, str | None]:
        """她写的路径 → （真实路径，zip 里的路径）。还没查权限。

        相对路径先找这个聊天的文件夹，再找工作文件夹（消息里“存在工作文件夹的 inbox/…”）。
        """
        raw = _clean_path(text)
        if not raw:
            raise SkillError("没说是哪个文件")
        path = Path(raw)
        if path.is_absolute():
            target, member = split_zip(path)
            return target.resolve(), member
        for base in (self.folder(ctx.channel), self.workspace):
            target, member = split_zip(base / path)
            if target.exists():
                return target.resolve(), member
        target, member = split_zip(self.folder(ctx.channel) / path)
        return target.resolve(), member

    def display(self, path: Path, ctx: SkillContext) -> str:
        """交回给她的路径：在这个聊天的文件夹里就写相对的（下次直接用）。"""
        resolved = path.resolve()
        for base in (self.folder(ctx.channel), self.workspace):
            if resolved.is_relative_to(base.resolve()):
                return resolved.relative_to(base.resolve()).as_posix()
        return resolved.as_posix()

    # ---- 权限 ----

    def _check_secret(self, path: Path, verb: str = "读") -> None:
        for denied in self._deny():
            if path.is_relative_to(denied.resolve()):
                raise SkillError(f"这个位置不能{verb}（密钥、记忆、登录信息等）")
        lowered = path.name.lower()
        if any(fnmatch.fnmatch(lowered, pattern) for pattern in SECRET_NAMES):
            raise SkillError(f"这个文件看起来是密钥或登录信息，不能{verb}")

    def in_own_places(self, path: Path, ctx: SkillContext) -> bool:
        """这个聊天自己的文件夹，或这个聊天里收到的文件。"""
        if self.cfg.folder_enabled and path.is_relative_to(self.folder(ctx.channel).resolve()):
            return True
        inbox = self.inbox.resolve()
        if path.is_relative_to(inbox):
            parts = path.relative_to(inbox).parts
            return len(parts) >= 2 and parts[1] == channel_folder(ctx.channel)
        return False

    def _group_readable(self, path: Path, ctx: SkillContext) -> bool:
        if ctx.private:
            return False
        for raw in self.cfg.group_read_paths:
            allowed = Path(raw) if Path(raw).is_absolute() else self.root / raw
            if path.is_relative_to(allowed.resolve()):
                return True
        return False

    def check_read(self, path: Path, ctx: SkillContext) -> None:
        self._check_secret(path)
        if self.local(ctx) or self.in_own_places(path, ctx) or self._group_readable(path, ctx):
            return
        raise SkillError("只能读这个聊天里收到的文件和这个聊天的文件夹")

    def check_send(self, path: Path, ctx: SkillContext) -> None:
        self._check_secret(path, "发")
        if self.local(ctx) or self.in_own_places(path, ctx):
            return
        chats, inbox, workspace = (
            self.chats.resolve(),
            self.inbox.resolve(),
            self.workspace.resolve(),
        )
        if (
            path.is_relative_to(workspace)
            and not path.is_relative_to(chats)  # 别的聊天的文件夹、收到的文件，什么设置都不放开
            and not path.is_relative_to(inbox)
            and (ctx.owner or not self.cfg.send_owner_only)
            and (ctx.private or self.cfg.send_in_groups)
        ):
            return
        raise SkillError("只能发这个聊天的文件夹里的文件和这个聊天里收到的文件")

    # ---- 存新文件 ----

    def save_new(
        self,
        target: str,
        data: bytes,
        ctx: SkillContext,
        *,
        default_name: str = "文件.txt",
        suffix: str | None = None,
    ) -> Path:
        """存一个新文件，返回实际路径。永远不覆盖已有文件（重名加序号）。

        target 空着或以 / 结尾时用 default_name；suffix：文件的真实类型（图片按内容判断）。
        """
        path = self._target(target, ctx, default_name, suffix)
        if path.suffix.lower() in RUNNABLE_SUFFIXES or path.name.lower() in RUNNABLE_NAMES:
            raise SkillError(
                f"{path.suffix or path.name} 这种文件不能直接写，可以运行的东西交给助手"
            )
        self._check_room(path, len(data), ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        return _write_exclusive(path, data)

    def _target(
        self, target: str, ctx: SkillContext, default_name: str, suffix: str | None
    ) -> Path:
        raw = _clean_path(target)
        raw = "" if raw in (".", "./") else raw
        if not raw or raw.endswith("/"):
            raw += default_name
        pure = PureWindowsPath(raw)
        if suffix is not None:
            pure = (
                pure.with_suffix(suffix)
                if pure.suffix.lower() in IMAGE_SUFFIXES
                else pure.with_name(pure.name + suffix)
            )
        if pure.anchor:
            if not self.local(ctx):
                raise SkillError("只能存进这个聊天的文件夹，路径写相对的，比如 花册/玫瑰.png")
            path = Path(pure).resolve()
            path = path.with_name(_clean_part(path.name))
            self._check_secret(path, "写")
            if any(part.lower() in SYSTEM_DIRS or part.startswith(".") for part in path.parts):
                raise SkillError("系统文件夹和隐藏文件夹不能写")
            if path.is_relative_to(self.root.resolve()) and not path.is_relative_to(
                self.workspace.resolve()
            ):
                raise SkillError("程序自己的文件夹不能写")
            return path
        parts = [_clean_part(part) for part in pure.parts]
        if len(parts) > MAX_DEPTH:
            raise SkillError(f"文件夹套得太深了，最多 {MAX_DEPTH} 层")
        if not self.cfg.folder_enabled:
            raise SkillError("聊天文件夹没有打开")
        base = self.folder(ctx.channel)
        path = base.joinpath(*parts)
        if not path.resolve().is_relative_to(base.resolve()):  # 文件夹里有符号链接也出不去
            raise SkillError("只能存进这个聊天的文件夹")
        self._check_secret(path.resolve(), "写")
        return path

    def _check_room(self, path: Path, size: int, ctx: SkillContext) -> None:
        if ctx.owner and ctx.private:
            return
        folder = self.folder(ctx.channel)
        used = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
        if used + size > self.cfg.folder_max_mb * 1024 * 1024:
            raise SkillError(f"这个聊天的文件夹满了（已用 {human_size(used)}），让主人清理一下")


def _clean_part(part: str) -> str:
    """相对路径的一层：不能往上走，名字里不能有 Windows 不允许的东西。"""
    if part in (".", ".."):
        raise SkillError("路径不能往上走，写这个聊天的文件夹里的相对路径")
    name = safe_name(part)
    if _DEVICE_NAME.match(name):
        raise SkillError(f"{name} 这个名字不能用")
    return name


def _write_exclusive(path: Path, data: bytes) -> Path:
    """只新建，不覆盖：文件已经存在就在名字后面加序号。"""
    n = 0
    while True:
        candidate = path if n == 0 else path.with_name(f"{path.stem}-{n}{path.suffix}")
        try:
            with candidate.open("xb") as f:
                f.write(data)
            return candidate
        except FileExistsError:
            n += 1
