import asyncio
import io
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from companion import media
from companion.bodies.qq.bot import QQBot
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.stickers import StickerBook, split_sticker
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


async def _handle_and_drain(bot, ev):
    await bot.handle_event(ev)
    await bot.drain()
