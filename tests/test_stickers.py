import asyncio
import base64
import io
import json
import random
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from companion import media
from companion.bodies.qq.bot import QQBot
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.memory.layout import commit_memory, ensure_gitignore
from companion.stickers import StickerBook, split_sticker
from companion.web.server import COOKIE, ConsoleApp
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, FRIEND, GROUP = "1000", "3000", "4000"
T0 = datetime(2026, 10, 24, 20, tzinfo=UTC)


def jpg(tmp_path, name, color):
    p = tmp_path / f"{name}.jpg"
    Image.new("RGB", (32, 32), color).save(p, "JPEG")
    return p


def png_bytes(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def animated_bytes(fmt="GIF", colors=("red", "green", "blue")) -> bytes:
    frames = [Image.new("RGB", (32, 32), color) for color in colors]
    buf = io.BytesIO()
    frames[0].save(buf, fmt, save_all=True, append_images=frames[1:], duration=80, loop=0)
    return buf.getvalue()


def frame_count(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as img:
        return img.n_frames


def write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# ---------- 解析 ----------


@pytest.mark.parametrize(
    ("raw", "text", "emotion"),
    [
        ("好吧好吧【表情：无奈】", "好吧好吧", "无奈"),
        ("晚安呀\n[表情:晚安]", "晚安呀", "晚安"),
        ("就这样", "就这样", None),
        ("【表情：开心】", "", "开心"),
    ],
)
def test_split_sticker(raw, text, emotion):
    assert split_sticker(raw) == (text, emotion)


# ---------- 表情库 ----------


def test_book_add_dedupe_persist_and_remove(tmp_path):
    book = StickerBook(tmp_path / "stickers")
    img = jpg(tmp_path, "a", "red")
    book.add(img, "d1", "一只叹气的猫", "无奈", "xiaoming")
    book.add(img, "d1", "一只叹气的猫", "无奈", "xiaohong")
    s = book.get("d1")
    assert s.seen == 2 and s.from_people == ["xiaoming", "xiaohong"]
    assert book.image_path("d1").exists()

    reloaded = StickerBook(tmp_path / "stickers")
    assert reloaded.get("d1").seen == 2 and reloaded.emotions() == ["无奈"]
    assert reloaded.remove("d1") and not reloaded.image_path("d1").exists()
    assert len(StickerBook(tmp_path / "stickers")) == 0


def test_book_evicts_least_seen(tmp_path):
    book = StickerBook(tmp_path / "s", capacity=2)
    img = jpg(tmp_path, "a", "red")
    book.add(img, "popular", "常用", "开心", None)
    book.add(img, "popular", "常用", "开心", None)
    book.add(img, "rare", "少见", "震惊", None)
    book.add(img, "new", "新的", "疑惑", None)
    assert book.get("popular") is not None and book.get("rare") is None


def test_pick_by_emotion_then_vector(tmp_path):
    book = StickerBook(tmp_path / "s")
    img = jpg(tmp_path, "a", "red")
    book.add(img, "sigh", "一只叹气的猫", "无奈", None)
    book.add(img, "yay", "举手欢呼的小狗", "开心", None)
    rng = random.Random(0)
    assert book.pick("无奈", rng=rng).digest == "sigh"
    assert book.pick("生气", rng=rng) is None  # 没有相同情绪、也没有向量
    emb = FakeEmbedder()
    assert book.pick("开心欢呼", embed=emb.embed, rng=rng, min_similarity=0.1).digest == "yay"


# ---------- 动图原图 ----------


@pytest.mark.parametrize(("fmt", "suffix"), [("GIF", ".gif"), ("WEBP", ".webp"), ("PNG", ".png")])
def test_book_keeps_animated_original(tmp_path, fmt, suffix):
    data = animated_bytes(fmt)
    assert frame_count(data) == 3
    original = write(tmp_path, f"x.orig{suffix}", data)
    book = StickerBook(tmp_path / "s")
    book.add(jpg(tmp_path, "a", "red"), "d1", "摊手的猫", "无奈", None, original=original)

    path = book.best_path("d1")
    assert path.suffix == suffix and path.read_bytes() == data
    assert book.image_path("d1").exists()  # JPEG 还在
    assert StickerBook(tmp_path / "s").best_path("d1") == path  # 重新打开也认得


def still_mpo() -> bytes:
    """手机拍的 JPEG 可能是这种多帧格式，但不是动图。"""
    buf = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    frames[0].save(buf, "MPO", save_all=True, append_images=frames[1:])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    assert frame_count(buf.getvalue()) == 2
    return buf.getvalue()


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("still.orig.png", png_bytes("red")),
        ("still.orig.gif", animated_bytes(colors=("red",))),
        ("photo.orig.png", still_mpo()),  # image_suffix 不认识 MPO，会存成 .png
        ("broken.orig.gif", b"GIF89a broken"),
    ],
    ids=["png", "one-frame-gif", "mpo", "broken"],
)
def test_book_uses_jpeg_when_original_is_not_animated(tmp_path, name, data):
    book = StickerBook(tmp_path / "s")
    original = write(tmp_path, name, data)
    book.add(jpg(tmp_path, "a", "red"), "d1", "一张图", "开心", None, original=original)
    assert book.original_path("d1") is None and book.best_path("d1") == book.image_path("d1")


def test_old_sticker_without_original_still_works_then_gets_it(tmp_path):
    book = StickerBook(tmp_path / "s")
    book.add(jpg(tmp_path, "a", "red"), "old", "摊手的猫", "无奈", None)  # 以前收藏的
    assert book.best_path("old") == book.image_path("old")

    gif = animated_bytes()
    original = write(tmp_path, "x.orig.gif", gif)
    book.add(jpg(tmp_path, "a", "red"), "old", "摊手的猫", "无奈", "p1", original=original)
    assert book.best_path("old").read_bytes() == gif and book.get("old").seen == 2


def test_remove_and_evict_delete_originals(tmp_path):
    book = StickerBook(tmp_path / "s", capacity=2)
    img = jpg(tmp_path, "a", "red")
    original = write(tmp_path, "x.orig.gif", animated_bytes())
    book.add(img, "popular", "常用", "开心", None, original=original)
    book.add(img, "popular", "常用", "开心", None)
    book.add(img, "rare", "少见", "震惊", None, original=original)
    book.add(img, "gone", "要删的", "疑惑", None, original=original)
    assert book.get("rare") is None and not list(book.root.glob("rare.*"))
    assert book.remove("gone") and not list(book.root.glob("gone.*"))
    assert book.best_path("popular").suffix == ".gif"


def test_half_copied_original_is_not_sent(tmp_path):
    book = StickerBook(tmp_path / "s")
    book.add(jpg(tmp_path, "a", "red"), "d1", "一张图", "开心", None)
    (book.root / "d1.orig.tmp").write_bytes(b"half")  # 上次复制到一半
    assert book.best_path("d1") == book.image_path("d1")
    assert book.remove("d1") and not list(book.root.glob("d1.*"))


# ---------- 回复里带表情 ----------


class Brain:
    reply = "好吧好吧【表情：无奈】"

    def __call__(self, messages, json_mode):
        if messages[-1].images:
            if json_mode:
                return json.dumps(
                    {"description": "一只摊手的猫", "emotion": "无奈", "usable": True},
                    ensure_ascii=False,
                )
            return "一张风景照"
        return self.reply


@pytest.fixture
def companion(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[stickers]\nsend_probability = 1.0\nmin_library = 1\n", encoding="utf-8"
    )
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = Brain()
    gateway, chat = scripted_gateway(brain)
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    c.chat = chat
    yield c
    c.close()


def test_no_sticker_hint_when_library_empty(companion):
    r = companion.reply(body="cli", channel="cli:x", account="x", text="唉", now=T0)
    assert r.sticker is None and r.text == "好吧好吧"
    assert "【表情包】" not in companion.chat.requests[-1][0].content


def test_reply_sends_sticker_with_cooldown(companion, tmp_path):
    companion.stickers.add(jpg(tmp_path, "s", "red"), "sigh", "一只摊手的猫", "无奈", None)
    r = companion.reply(body="cli", channel="cli:x", account="x", text="唉", now=T0)
    assert "能表达：无奈" in companion.chat.requests[-1][0].content
    assert r.text == "好吧好吧" and r.sticker.digest == "sigh"
    mine = [e.content for e in companion.log.iter_events() if e.actor.person_id == "self"]
    assert mine == ["好吧好吧", "[表情包：一只摊手的猫]"]
    assert companion.stickers.get("sigh").used == 1
    # 冷却期内不再发
    r2 = companion.reply(
        body="cli", channel="cli:x", account="x", text="唉", now=T0 + timedelta(seconds=30)
    )
    assert r2.sticker is None


def test_look_collects_usable_sticker_once(companion):
    desc, digest = companion.look(png_bytes("blue"), sticker=True, person_id="p1")
    assert desc == "一只摊手的猫" and companion.stickers.get(digest).emotion == "无奈"
    calls = len(companion.chat.requests)
    companion.look(png_bytes("blue"), sticker=True, person_id="p2")
    assert len(companion.chat.requests) == calls  # 收藏过的不再看
    assert companion.stickers.get(digest).seen == 2


def test_look_skips_unusable(companion):
    Brain.reply = "x"
    original = companion.gateway.fallback.client.handler

    def private_photo(messages, json_mode):
        if json_mode:
            return json.dumps({"description": "一张自拍", "emotion": "开心", "usable": False})
        return original(messages, json_mode)

    companion.gateway.fallback.client.handler = private_photo
    _, digest = companion.look(png_bytes("green"), sticker=True)
    assert companion.stickers.get(digest) is None
    Brain.reply = "好吧好吧【表情：无奈】"


def test_look_keeps_animated_sticker(companion):
    gif = animated_bytes()
    _, digest = companion.look(gif, sticker=True, person_id="p1")
    assert companion.stickers.best_path(digest).read_bytes() == gif
    assert companion.stickers.image_path(digest).exists()


def test_look_fills_in_original_for_old_sticker(companion, tmp_path):
    gif = animated_bytes()
    digest = companion.media.save(gif).digest
    companion.media.original_for(digest).unlink()  # 以前图片库也没留原图
    companion.stickers.add(jpg(tmp_path, "s", "red"), digest, "摊手的猫", "无奈", None)
    calls = len(companion.chat.requests)
    companion.look(gif, sticker=True, person_id="p1")
    assert len(companion.chat.requests) == calls  # 还是不用再看一遍
    assert companion.stickers.best_path(digest).read_bytes() == gif


# ---------- 控制台预览 ----------


def test_console_shows_animated_preview(companion, tmp_path):
    gif = animated_bytes()
    _, digest = companion.look(gif, sticker=True)
    companion.stickers.add(jpg(tmp_path, "s", "red"), "old", "摊手的猫", "无奈", None)
    app = ConsoleApp(tmp_path, "tok", companion=companion)
    headers = {"host": "127.0.0.1:8765", "cookie": f"{COOKIE}=tok"}

    status, extra, body = app.handle("GET", f"/api/sticker-image/{digest}", headers, b"")
    assert status == 200 and extra["Content-Type"] == "image/gif" and frame_count(body) == 3

    status, extra, body = app.handle("GET", "/api/sticker-image/old", headers, b"")
    assert status == 200 and extra["Content-Type"] == "image/jpeg"
    assert Image.open(io.BytesIO(body)).format == "JPEG"

    assert app.handle("GET", "/api/sticker-image/none", headers, b"")[0] == 404
    no_cookie = {"host": "127.0.0.1:8765"}
    assert app.handle("GET", f"/api/sticker-image/{digest}", no_cookie, b"")[0] == 401


# ---------- 图片不进记忆文件夹的 git ----------

# 最后一行是用户自己加的，也没有换行
OLD_GITIGNORE = "index.sqlite\nevents/*.jsonl\nmedia/\nstickers/*.jpg\nmy-notes/"


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8", errors="replace"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_new_memory_ignores_sticker_images(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎")
    assert git(layout.root, "check-ignore", "stickers/a.jpg").returncode == 0
    assert git(layout.root, "check-ignore", "stickers/a.orig.gif").returncode == 0
    assert git(layout.root, "check-ignore", "stickers/a.orig.tmp").returncode == 0
    assert git(layout.root, "check-ignore", "stickers/index.json").returncode == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_existing_memory_gitignore_is_upgraded(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    layout = MemoryLayout(config.memory_dir)
    init_memory(layout, "白璎")
    (layout.root / ".gitignore").write_text(OLD_GITIGNORE, encoding="utf-8", newline="\n")
    commit_memory(layout, "白璎", "旧的规则")

    gateway, _ = scripted_gateway(Brain())
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    try:
        _, digest = c.look(animated_bytes(), sticker=True, person_id="p1")
        assert c.stickers.original_path(digest) is not None
        assert commit_memory(layout, "白璎", "收藏了动图")
    finally:
        c.close()

    tracked = git(layout.root, "ls-files", "stickers").stdout.split()
    assert tracked == ["stickers/index.json"]
    assert digest not in git(layout.root, "log", "--all", "--name-only", "--format=").stdout

    rules = (layout.root / ".gitignore").read_text("utf-8").splitlines()
    assert "my-notes/" in rules and rules.count("stickers/*.orig.*") == 1
    assert ensure_gitignore(layout) is False  # 再跑一遍不会重复追加


# ---------- QQ 收藏与发送 ----------


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        return {"message_id": 9000 + len(self.sent)}


def test_qq_collects_and_sends_sticker(companion, monkeypatch):
    monkeypatch.setattr(media, "fetch", lambda url, *, max_bytes, timeout=30.0: png_bytes("purple"))
    cfg = QQConfig(
        bot_account=BOT,
        groups=[GROUP],
        unsolicited_probability=0,
        debounce_private_seconds=0,
        debounce_group_seconds=0,
    )
    bot = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: T0)
    ev = {
        "post_type": "message", "self_id": int(BOT), "user_id": int(FRIEND), "message_id": 1,
        "message_type": "private", "sender": {"nickname": "小明"}, "time": int(T0.timestamp()),
        "message": [{"type": "image", "data": {"url": "https://img/1", "sub_type": 1}}],
    }  # fmt: skip
    try:
        asyncio.run(_handle_and_drain(bot, ev))
    finally:
        bot.close()
    assert len(companion.stickers) == 1
    texts = [p["message"] for a, p in bot.client.sent if a == "send_private_msg"]
    assert texts[0][0]["data"]["text"] == "好吧好吧"
    sticker_seg = texts[1][0]
    assert sticker_seg["type"] == "image" and sticker_seg["data"]["file"].startswith("base64://")
    first = next(iter(companion.log.iter_events()))
    assert first.content == "[表情包：一只摊手的猫]"


def sticker_reply_to(companion, message: list[dict]) -> bytes:
    """小明发来 message，返回她回的那张表情实际发出去的字节。"""
    cfg = QQConfig(
        bot_account=BOT,
        groups=[GROUP],
        unsolicited_probability=0,
        debounce_private_seconds=0,
        debounce_group_seconds=0,
    )
    bot = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: T0)
    ev = {
        "post_type": "message", "self_id": int(BOT), "user_id": int(FRIEND), "message_id": 1,
        "message_type": "private", "sender": {"nickname": "小明"}, "time": int(T0.timestamp()),
        "message": message,
    }  # fmt: skip
    try:
        asyncio.run(_handle_and_drain(bot, ev))
    finally:
        bot.close()
    sent = [p["message"][0] for a, p in bot.client.sent if a == "send_private_msg"]
    segment = next(seg for seg in sent if seg["type"] == "image")
    assert segment["data"]["sub_type"] == 1
    return base64.b64decode(segment["data"]["file"].removeprefix("base64://"))


def test_qq_sends_collected_gif_still_animated(companion, monkeypatch):
    gif = animated_bytes()
    monkeypatch.setattr(media, "fetch", lambda url, *, max_bytes, timeout=30.0: gif)
    sent = sticker_reply_to(
        companion, [{"type": "image", "data": {"url": "https://img/1", "sub_type": 1}}]
    )
    assert sent == gif and frame_count(sent) == 3


def test_qq_sends_jpeg_for_old_sticker(companion, tmp_path):
    companion.stickers.add(jpg(tmp_path, "s", "red"), "old", "摊手的猫", "无奈", None)
    sent = sticker_reply_to(companion, [{"type": "text", "data": {"text": "唉"}}])
    assert Image.open(io.BytesIO(sent)).format == "JPEG"


async def _handle_and_drain(bot, ev):
    await bot.handle_event(ev)
    await bot.drain()
