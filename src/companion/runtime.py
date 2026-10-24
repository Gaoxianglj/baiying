"""后台运行的辅助：单实例锁、日志文件、停止标记。

一键启停脚本把运行状态放在 `data/run/`、日志放在 `data/logs/`。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import IO

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s：%(message)s"


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """进程级排他锁：同一个服务只允许运行一个实例。进程退出时操作系统自动释放。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise AlreadyRunning(f"{self.path.stem} 已经在运行") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt="%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_dir(data_dir: Path) -> Path:
    return data_dir / "run"


def stop_flag(data_dir: Path, name: str) -> Path:
    """停止脚本创建这个文件，服务看到后自行退出（退出前会整理记忆）。"""
    return run_dir(data_dir) / f"{name}.stop"
