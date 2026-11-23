"""谁能碰哪些文件：读、发、存都走这里，规则只有一份。路径都解析成真实路径后再判断。

- 每个聊天有自己的文件夹（工作文件夹的 chats/聊天/）：这个聊天里能存新文件、能读、能发给任何人。
- 收到的文件与产出共用 chats/聊天/；历史 inbox/日期/聊天/ 路径继续兼容，只在原聊天能读、能发。
- 主人私聊：还能读、发这台电脑上的其他文件；新文件也能存到别的地方（系统和程序的文件夹除外）。
- 群聊：另外可以读控制台指定的文件夹（group_read_paths，只读不发）。
- 黑名单（密钥、记忆、登录信息、浏览器和密码管理器数据）和看起来像密钥的文件，谁都不行。
- 任何地方都不改已有文件：只新建，重名加序号。可执行的文件类型不能写。
- 可执行的文件类型只有主人私聊能发：群助手（在沙箱里，见 group_agent.py）能造出来，但不往外递。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
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
        policy_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.workspace = workspace if workspace.is_absolute() else root / workspace
        self.inbox = cfg.inbox if cfg.inbox.is_absolute() else root / cfg.inbox
        self._deny = deny
        self._policy_path = policy_path or root / "data/run/trpg-protected.json"

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
            return self._legacy_alias(target), member
        for base in (self.folder(ctx.channel), self.workspace):
            target, member = split_zip(base / path)
            if target.exists():
                return self._legacy_alias(target), member
        if path.parts and ".." not in path.parts and path.parts[0] not in {"chats", "inbox"}:
            archive = next(
                (i for i, part in enumerate(path.parts) if part.lower().endswith(".zip")), None
            )
            name = path.parts[archive] if archive is not None else path.name
            matches = [p for p in self.inventory(ctx) if p.name.casefold() == name.casefold()]
            if len(matches) == 1:
                member = "/".join(path.parts[archive + 1 :]) if archive is not None else None
                return matches[0], member or None
            if len(matches) > 1:
                choices = "；".join(self.display(p, ctx) for p in matches[:8])
                raise SkillError(f"找到多个同名文件，请根据当前任务选择路径：{choices}")
        target, member = split_zip(self.folder(ctx.channel) / path)
        return target.resolve(), member

    def inventory(self, ctx: SkillContext, *, limit=1500) -> list[Path]:
        """只枚举当前聊天可读范围，先校验权限，再向模型提供文件名。"""
        roots = [self.folder(ctx.channel), self.inbox / channel_folder(ctx.channel)]
        if self.inbox.exists():
            roots += [
                p / channel_folder(ctx.channel)
                for p in self.inbox.iterdir()
                if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
            ]
        if not ctx.private:
            roots += [
                Path(p) if Path(p).is_absolute() else self.root / p
                for p in self.cfg.group_read_paths
            ]
        found = {}
        deadline = time.monotonic() + 1.5
        for root in roots:
            if not root.is_dir():
                continue
            for folder, dirs, names in os.walk(root, followlinks=False):
                here = Path(folder)
                dirs[:] = [
                    d
                    for d in dirs
                    if not (here / d).is_symlink()
                    and not d.startswith(".")
                    and len((here / d).relative_to(root).parts) <= MAX_DEPTH
                ]
                for name in names:
                    path = (here / name).resolve()
                    try:
                        self.check_read(path, ctx)
                        if path.is_file():
                            found[path] = None
                    except (SkillError, OSError):
                        continue
                    if len(found) >= limit or time.monotonic() > deadline:
                        return list(found)
                if time.monotonic() > deadline:
                    return list(found)
        return list(found)

    def find(self, query: str, ctx: SkillContext, *, limit=20) -> list[Path]:
        words = query.casefold().split()
        files = [
            p
            for p in self.inventory(ctx)
            if all(word in self.display(p, ctx).casefold() for word in words)
        ]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]

    def _legacy_alias(self, path: Path) -> Path:
        """旧聊天记录里的日期路径指向归并后的文件；权限仍由 check_read/check_send 校验。"""
        resolved = path.resolve()
        mapping = self.root / "data" / "run" / "file_aliases.json"
        if mapping.exists() and resolved.is_relative_to(self.inbox.resolve()):
            try:
                aliases = json.loads(mapping.read_text(encoding="utf-8"))["aliases"]
            except (OSError, ValueError, KeyError) as e:
                raise SkillError("历史文件路径映射无法读取，请检查迁移记录") from e
            if target := aliases.get(resolved.as_posix()):
                return Path(target).resolve()
        return resolved

    def display(self, path: Path, ctx: SkillContext) -> str:
        """交回给她的路径：在这个聊天的文件夹里就写相对的（下次直接用）。"""
        resolved = path.resolve()
        for base in (self.folder(ctx.channel), self.workspace):
            if resolved.is_relative_to(base.resolve()):
                return resolved.relative_to(base.resolve()).as_posix()
        return resolved.as_posix()

    # ---- 权限 ----

    def _check_secret(self, path: Path, verb: str = "读") -> None:
        from companion.trpg.policy import check_path

        for denied in self._deny():
            if path.is_relative_to(denied.resolve()):
                raise SkillError(f"这个位置不能{verb}（密钥、记忆、登录信息等）")
        lowered = path.name.lower()
        if any(fnmatch.fnmatch(lowered, pattern) for pattern in SECRET_NAMES):
            raise SkillError(f"这个文件看起来是密钥或登录信息，不能{verb}")
        check_path(path, self._policy_path)

    def in_own_places(self, path: Path, ctx: SkillContext) -> bool:
        """这个聊天自己的文件夹，或这个聊天里收到的文件。"""
        if self.cfg.folder_enabled and path.is_relative_to(self.folder(ctx.channel).resolve()):
            return True
        inbox = self.inbox.resolve()
        if path.is_relative_to(inbox):
            parts = path.relative_to(inbox).parts
            channel = channel_folder(ctx.channel)
            # 兼容没有日期层的收件，以及旧版按日期保存的文件，始终校验聊天编号。
            return (len(parts) >= 2 and parts[0] == channel) or (
                len(parts) >= 3
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]) is not None
                and parts[1] == channel
            )
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
        if self.local(ctx):
            return
        # 群助手能在聊天文件夹里造出可运行的文件；她不该把它们递给别人
        if path.suffix.lower() in RUNNABLE_SUFFIXES or path.name.lower() in RUNNABLE_NAMES:
            raise SkillError(f"{path.suffix or path.name} 这种可以运行的文件不能发到这里")
        if self.in_own_places(path, ctx):
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
        if (ctx.stop_event and ctx.stop_event.is_set()) or (
            ctx.deadline and time.monotonic() > ctx.deadline
        ):
            raise SkillError("任务已停止或达到时限，未开始新的文件写入")
        path = self._target(target, ctx, default_name, suffix)
        if path.suffix.lower() in RUNNABLE_SUFFIXES or path.name.lower() in RUNNABLE_NAMES:
            raise SkillError(
                f"{path.suffix or path.name} 这种文件不能直接写，可以运行的东西交给助手"
            )
        book, task_id = ctx.task_book, ctx.task_id
        fingerprint = hashlib.sha256((str(path) + "\0").encode() + data).hexdigest()
        if book and task_id:
            previous = book.file_write(task_id, fingerprint)
            if previous:
                old_path = Path(previous["path"])
                self.check_read(old_path.resolve(), ctx)
                if old_path.is_file():
                    if hashlib.sha256(old_path.read_bytes()).hexdigest() == previous["sha256"]:
                        book.file_write(task_id, fingerprint, {**previous, "state": "complete"})
                        return old_path
                    raise SkillError("上次生成的文件已改变或写入未完成，保留现场；请换新目标文件名")
                if previous["state"] == "complete":
                    raise SkillError("上次生成的文件已被移走或删除；请核对后使用新目标文件名")
        self._check_room(path, len(data), ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        if book and task_id:
            sha = hashlib.sha256(data).hexdigest()
            n = 0
            while True:
                candidate = path if n == 0 else path.with_name(f"{path.stem}-{n}{path.suffix}")
                if candidate.exists():
                    n += 1
                    continue
                record = {"path": str(candidate), "sha256": sha, "state": "planned"}
                book.file_write(task_id, fingerprint, record)
                try:
                    with candidate.open("xb") as f:
                        f.write(data)
                        f.flush()
                except FileExistsError:
                    n += 1
                    continue
                book.file_write(task_id, fingerprint, {**record, "state": "complete"})
                return candidate
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
