import json
import shutil
import subprocess

import pytest

from companion.memory import MemoryFormatError, MemoryLayout, init_memory, load_manifest


def test_init_creates_layout(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    created = init_memory(layout, "白璎", git=False)

    assert layout.exists()
    for d in (layout.events_dir, layout.journal_dir, layout.people_dir, layout.archive_dir):
        assert d.is_dir()
    manifest = load_manifest(layout)
    assert manifest["persona"] == "白璎"
    assert manifest["derived"]["embedding_model"] is None
    identity = (layout.self_dir / "identity.md").read_text("utf-8")
    assert identity.startswith("# 白璎 的自我认知")
    preferences = (layout.self_dir / "preferences.md").read_text("utf-8")
    assert preferences.startswith("# 白璎 的喜好与习惯")
    assert "## 穿搭与随身物" in preferences and "## 闲下来喜欢做的事" in preferences
    assert layout.manifest_path in created


def test_init_is_idempotent_and_keeps_edits(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎", git=False)
    seed = layout.self_dir / "seed.md"
    seed.write_text("我自己写的种子", encoding="utf-8")

    assert init_memory(layout, "白璎", git=False) == []
    assert seed.read_text("utf-8") == "我自己写的种子"


def test_init_refuses_foreign_manifest(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    layout.root.mkdir()
    layout.manifest_path.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(MemoryFormatError):
        init_memory(layout, "白璎", git=False)


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_init_commits_as_persona(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎")
    init_memory(layout, "白璎")  # 第二次没有改动，不应产生新提交

    log = (
        subprocess.run(
            ["git", "log", "--format=%an|%s"],
            cwd=layout.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    assert log == ["白璎|初始化记忆文件夹"]
    ignored = subprocess.run(
        ["git", "check-ignore", "index.sqlite"], cwd=layout.root, capture_output=True, text=True
    )
    assert ignored.returncode == 0
