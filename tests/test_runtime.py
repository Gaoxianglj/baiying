import subprocess
import sys
import textwrap

import pytest

from companion.runtime import AlreadyRunning, InstanceLock
from companion.web.server import console_token


def test_lock_blocks_second_holder_in_another_process(tmp_path):
    path = tmp_path / "run" / "qq.lock"
    with InstanceLock(path):
        code = textwrap.dedent(
            f"""
            from pathlib import Path
            from companion.runtime import AlreadyRunning, InstanceLock
            try:
                InstanceLock(Path(r"{path}")).acquire()
                print("got")
            except AlreadyRunning:
                print("blocked")
            """
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "blocked"
    # 释放后可以再拿到
    InstanceLock(path).acquire().release()


def test_lock_same_process_twice(tmp_path):
    lock = InstanceLock(tmp_path / "a.lock").acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path / "a.lock").acquire()
    finally:
        lock.release()


def test_console_token_is_stable_and_rotatable(tmp_path):
    first = console_token(tmp_path)
    assert console_token(tmp_path) == first
    assert console_token(tmp_path, rotate=True) != first
