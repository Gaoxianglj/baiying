"""取消日期分层，收件与产物共用聊天目录；历史归并保持归属、旧路径与数据。"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from companion.config import load_config
from companion.skills.access import FileAccess
from companion.skills.base import SkillContext, SkillError
from companion.skills.file_migration import migrate_chat_files
from companion.skills.files import FileInbox
from companion.skills.reader import ReadFile

GROUP = SkillContext("qq", "group:100", "a", False, False)
OTHER = SkillContext("qq", "group:200", "b", False, False)
PRIVATE = SkillContext("qq", "private:300", "c", True, False)
T0 = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    cfg = load_config(tmp_path, environ={})
    access = FileAccess(cfg.skills.files, tmp_path, cfg.skills.agent.workspace, lambda: [])
    inbox = FileInbox(cfg.skills.files, tmp_path, cfg.skills.agent.workspace)
    reader = ReadFile(cfg.skills.files, access)
    return cfg, access, inbox, reader


def legacy(cfg, day, channel, name, data):
    path = cfg.root / cfg.skills.files.inbox / day / channel.replace(":", "-") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_all_dates_and_generated_files_share_the_same_chat_folder(env):
    cfg, access, inbox, reader = env
    before = inbox.save("旧资料.txt", b"yesterday", T0 - timedelta(days=5), GROUP.channel)
    today = inbox.save("新资料.txt", b"today", T0, GROUP.channel)
    produced = access.save_new("索引.md", b"index", GROUP)
    assert before.parent == today.parent == produced.parent == access.folder(GROUP.channel)
    assert before.parent == cfg.root / "data/workspace/chats/group-100"
    listing = reader.run("看目录", ".", GROUP).text
    assert all(p.name in listing for p in [before, today, produced])
    assert "yesterday" in reader.run("读文件", before.name, GROUP).text
    assert "不" in reader.context(GROUP) and "日期" in reader.context(GROUP)
    for ctx in [OTHER, PRIVATE]:
        with pytest.raises(SkillError):
            reader.run("读文件", str(before), ctx)


def test_migration_is_previewed_copied_verified_and_idempotent(env):
    cfg, access, _, reader = env
    old = legacy(cfg, "2026-11-01", GROUP.channel, "资料.txt", b"old")
    other = legacy(cfg, "2026-11-09", GROUP.channel, "新资料.txt", b"new")
    assert "old" in reader.run("读文件", str(old), GROUP).text
    preview = migrate_chat_files(cfg)
    assert not preview["applied"] and len(preview["files"]) == 2
    assert not access.folder(GROUP.channel).exists()
    first = migrate_chat_files(cfg, apply=True)
    assert len(first["files"]) == 2 and not first["skipped"]
    assert old.read_bytes() == b"old" and other.read_bytes() == b"new"
    assert (access.folder(GROUP.channel) / "资料.txt").read_bytes() == b"old"
    again = migrate_chat_files(cfg, apply=True)
    assert not any(row["copied"] for row in again["files"])
    assert len(list(access.folder(GROUP.channel).iterdir())) == 2
    assert "old" in reader.run("读文件", str(old), GROUP).text


def test_conflicting_dates_keep_every_version_and_old_paths_resolve_correctly(env):
    cfg, access, _, reader = env
    original = access.save_new("同名.md", b"current", GROUP)
    first = legacy(cfg, "2026-11-01", GROUP.channel, "同名.md", b"first")
    second = legacy(cfg, "2026-11-02", GROUP.channel, "同名.md", b"second")
    migrate_chat_files(cfg, apply=True)
    assert original.read_bytes() == b"current"
    assert len(list(original.parent.glob("同名*.md"))) == 3
    assert access.locate(str(first), GROUP)[0].read_bytes() == b"first"
    assert access.locate(str(second), GROUP)[0].read_bytes() == b"second"
    assert "first" in reader.run("读文件", str(first), GROUP).text
    assert len(migrate_chat_files(cfg, apply=True)["files"]) == 2
    assert len(list(original.parent.glob("同名*.md"))) == 3


def test_legacy_unscoped_file_uses_original_receipt_channel(env):
    cfg, access, _, reader = env
    old = cfg.root / cfg.skills.files.inbox / "2026-11-03" / "私事.txt"
    old.parent.mkdir(parents=True)
    old.write_text("private", encoding="utf-8")
    events = cfg.memory_dir / "events" / "2026-11.jsonl"
    events.parent.mkdir(parents=True)
    event = {"channel": PRIVATE.channel, "actor": {"platform": "qq"},
             "content": "[文件：私事.txt，存在工作文件夹的 inbox/2026-11-03/私事.txt]"}
    events.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    report = migrate_chat_files(cfg, apply=True)
    assert not report["skipped"]
    target, _ = access.locate(str(old), PRIVATE)
    assert target.parent == access.folder(PRIVATE.channel)
    assert "private" in reader.run("读文件", str(old), PRIVATE).text
    with pytest.raises(SkillError):
        reader.run("读文件", str(old), GROUP)


def test_unknown_legacy_owner_is_preserved_without_sharing(env):
    cfg, access, _, _ = env
    old = cfg.root / cfg.skills.files.inbox / "2026-11-01" / "unknown.txt"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"unknown")
    report = migrate_chat_files(cfg, apply=True)
    assert len(report["skipped"]) == 1 and not report["files"]
    assert old.read_bytes() == b"unknown"
    assert not access.folder(GROUP.channel).exists()


def test_folder_switch_off_keeps_receipts_readable_without_dates(env):
    cfg, access, inbox, reader = env
    cfg.skills.files.folder_enabled = False
    saved = inbox.save("a.txt", b"inbox", T0, GROUP.channel)
    assert saved.parent == cfg.root / cfg.skills.files.inbox / "group-100"
    assert "inbox" in reader.run("读文件", str(saved), GROUP).text
    with pytest.raises(SkillError):
        reader.run("读文件", str(saved), OTHER)


def test_concurrent_uploads_never_overwrite_each_other(env):
    _, access, inbox, _ = env
    with ThreadPoolExecutor(max_workers=6) as pool:
        paths = list(pool.map(lambda n: inbox.save("same.txt", str(n).encode(), T0,
                                                   GROUP.channel), range(12)))
    assert len(set(paths)) == 12
    assert {p.read_bytes() for p in paths} == {str(n).encode() for n in range(12)}
    assert all(p.parent == access.folder(GROUP.channel) for p in paths)
