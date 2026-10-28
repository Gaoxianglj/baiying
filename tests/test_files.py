"""收发文件：收到的存进工作文件夹的 inbox，发的只能是工作文件夹里的文件。"""

import asyncio
import random
from datetime import UTC, datetime

import pytest

from companion.bodies.qq.bot import QQBot
from companion.bodies.qq.message import parse_segments, render_forward
from companion.config import FilesSkillConfig, QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.skills import SkillContext, SkillResult
from companion.skills.base import SkillError
from companion.skills.files import FileInbox, SendFile, safe_name
from tests.fakes import FakeEmbedder, scripted_gateway
from tests.test_qq import FakeClient

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"
T0 = datetime(2026, 10, 28, 12, tzinfo=UTC)


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = {"reply": "收到啦"}
    gateway, chat = scripted_gateway(lambda messages, json_mode: brain["reply"])
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=[GROUP],
                   debounce_private_seconds=0, debounce_group_seconds=0)  # fmt: skip
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: T0)
    b.brain, b.chat, b.root = brain, chat, tmp_path
    b.workspace = tmp_path / "data" / "workspace"
    yield b
    b.close()
    companion.close()


def file_msg(name, *, user=FRIEND, group=None, size=12, url=None, at_bot=False, text=None):
    segs = [{"type": "at", "data": {"qq": BOT}}] if at_bot else []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    data = {"file": name, "file_id": "fid-1", "file_size": str(size)}
    if url:
        data["url"] = url
    segs.append({"type": "file", "data": data})
    ev = {"post_type": "message", "self_id": int(BOT), "user_id": int(user), "message_id": 7,
          "message_type": "group" if group else "private", "message": segs,
          "sender": {"nickname": "小明"}}  # fmt: skip
    if group:
        ev["group_id"] = int(group)
    return ev


def run(bot, *events):
    async def go():
        for ev in events:
            await bot.handle_event(ev)
            await bot.drain()

    asyncio.run(go())


def napcat_has(bot, tmp_path, content: bytes, name="note.txt"):
    """让假的 NapCat 在 get_file 时给出一个本地文件。"""
    local = tmp_path / "napcat-cache" / name
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(content)
    bot.client.replies["get_file"] = {"file": str(local), "file_name": name}


# ---- 消息段 ----


def test_file_segment_becomes_placeholder():
    inc = parse_segments([{"type": "text", "data": {"text": "给你 "}},
                          {"type": "file", "data": {"file": "a.pdf", "file_id": "x",
                                                    "file_size": "2048"}}], BOT)  # fmt: skip
    assert inc.text == "给你 ⟦文件0⟧" and inc.files[0].name == "a.pdf" and inc.files[0].size == 2048
    assert inc.fill([]) == "给你 [文件：a.pdf]"
    node = {
        "sender": {"nickname": "小红"},
        "message": [{"type": "file", "data": {"file": "b.zip"}}],
    }
    assert render_forward([node], BOT).endswith("小红：[文件：b.zip]）")


def test_safe_name_drops_paths():
    assert safe_name("../../secret/.env") == "env"  # 开头的点也去掉，不收隐藏文件
    assert safe_name("a<b>:c?.txt") == "a_b__c_.txt"
    assert safe_name("..") == "文件"


# ---- 收 ----


def test_private_text_file_saved_and_previewed(bot, tmp_path):
    napcat_has(bot, tmp_path, "第一行\n第二行".encode())
    run(bot, file_msg("note.txt"))
    saved = bot.workspace / "inbox" / f"{T0.astimezone():%Y-%m-%d}" / "note.txt"
    assert saved.read_text("utf-8") == "第一行\n第二行"
    theirs = [e.content for e in bot.c.log.iter_events() if e.actor.person_id != "self"]
    assert theirs[0].startswith("[文件：note.txt（")
    assert f"inbox/{T0.astimezone():%Y-%m-%d}/note.txt" in theirs[0] and "第二行" in theirs[0]
    assert ("get_file", {"file_id": "fid-1"}) in bot.client.sent


def test_same_name_is_not_overwritten(bot, tmp_path):
    napcat_has(bot, tmp_path, b"v1", name="a.bin")
    run(bot, file_msg("a.bin"))
    napcat_has(bot, tmp_path, b"v2", name="a.bin")
    run(bot, file_msg("a.bin"))
    folder = bot.workspace / "inbox" / f"{T0.astimezone():%Y-%m-%d}"
    assert sorted(p.name for p in folder.iterdir()) == ["a-1.bin", "a.bin"]


def test_group_file_not_for_her_is_not_downloaded(bot):
    run(bot, file_msg("big.pdf", group=GROUP))
    assert not any(a == "get_file" for a, _ in bot.client.sent)
    theirs = [e.content for e in bot.c.log.iter_events()]
    assert theirs == ["[文件：big.pdf（12B，没收）]"]


def test_too_big_file_is_refused(bot):
    run(bot, file_msg("movie.mp4", size=50 * 1024 * 1024))
    assert not any(a == "get_file" for a, _ in bot.client.sent)
    assert "超过 10 MB，没收" in next(bot.c.log.iter_events()).content


def test_download_failure_is_explicit(bot):
    bot.client.replies["get_file"] = {}
    run(bot, file_msg("x.txt", at_bot=True, group=GROUP))
    assert "没收到" in next(bot.c.log.iter_events()).content


def test_inbox_describe_binary_has_no_preview(tmp_path):
    inbox = FileInbox(FilesSkillConfig(inbox=tmp_path / "ws" / "inbox"), tmp_path, tmp_path / "ws")
    path = inbox.save("photo.png", b"\x89PNG....", T0)
    day = f"{T0.astimezone():%Y-%m-%d}"
    assert inbox.describe(path, "photo.png").endswith(f"存在工作文件夹的 inbox/{day}/photo.png]")


# ---- 发 ----


def put(bot, rel, data=b"a,b\n1,2\n"):
    path = bot.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_send_file_skill_resolves_inside_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "r.csv").write_text("x", "utf-8")
    (tmp_path / "secret.txt").write_text("x", "utf-8")
    skill = SendFile(FilesSkillConfig(max_mb=1), tmp_path, ws)
    assert skill.resolve("out/r.csv") == (ws / "out" / "r.csv").resolve()
    for bad in ("../secret.txt", str(tmp_path / "secret.txt"), "out/none.csv", ""):
        with pytest.raises(SkillError):
            skill.resolve(bad)
    (ws / "big.bin").write_bytes(b"0" * (1024 * 1024 + 1))
    with pytest.raises(SkillError, match="太大"):
        skill.resolve("big.bin")
    owner = SkillContext("qq", "private:1", "p", private=True, owner=True)
    assert skill.allowed(owner)
    assert not skill.allowed(SkillContext("qq", "private:2", "p", private=True, owner=False))
    assert not skill.allowed(SkillContext("qq", "group:1", "p", private=False, owner=True))


def test_owner_gets_file_in_private(bot):
    put(bot, "out/report.csv")
    replies = iter(["好，发给你【发文件：out/report.csv】", "发过去啦，你看看"])
    bot.brain["reply"] = None
    bot.c.gateway.fallback.client.handler = lambda m, j: next(replies)
    run(bot, {**file_msg("ignored", user=OWNER), "message": [
        {"type": "text", "data": {"text": "把表格发我"}}]})  # fmt: skip
    uploads = [p for a, p in bot.client.sent if a == "upload_private_file"]
    assert len(uploads) == 1 and uploads[0]["name"] == "report.csv"
    assert uploads[0]["user_id"] == int(OWNER) and uploads[0]["file"].endswith("report.csv")
    mine = [e.content for e in bot.c.log.iter_events() if e.actor.person_id == "self"]
    assert "[我发的文件：report.csv]" in mine


def test_others_cannot_get_files(bot):
    put(bot, "out/report.csv")
    bot.brain["reply"] = "给你【发文件：out/report.csv】"
    run(bot, {**file_msg("ignored"), "message": [{"type": "text", "data": {"text": "发我"}}]})
    assert not any(a.startswith("upload_") for a, _ in bot.client.sent)


def test_report_can_send_task_output(bot):
    put(bot, "汇总.xlsx", b"xlsx")
    bot.brain["reply"] = "整理好了，表格给你【发文件：汇总.xlsx】"
    owner = bot.c.people.ensure("qq", OWNER, "祥")
    ctx = SkillContext("qq", f"private:{OWNER}", owner, private=True, owner=True, now=T0)
    text, files = bot.c.report(ctx, "agent", SkillResult("产出：汇总.xlsx", report=True))
    assert text == "整理好了，表格给你" and [p.name for p in files] == ["汇总.xlsx"]
    note = bot.chat.requests[-1][-1].content
    assert "【发文件：" in note  # 告诉了她可以把文件发过去
