import asyncio
import io
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image

from companion import media
from companion.bodies.qq.bot import QQBot
from companion.config import QQConfig, load_config
from companion.core.companion import Companion
from companion.media import MediaError, MediaStore
from companion.memory import MemoryLayout, init_memory
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.openai_compat import ChatMessage, OpenAICompatClient
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, FRIEND, GROUP = "1000", "3000", "4000"
T0 = datetime(2026, 10, 24, 12, tzinfo=UTC)


def png(color=(255, 120, 0, 255), size=(1600, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---------- 图片存储 ----------


def test_store_normalizes_and_dedupes(tmp_path):
    store = MediaStore(tmp_path, max_side=512)
    a = store.save(png())
    b = store.save(png())
    assert a == b and a.path.suffix == ".jpg"
    with Image.open(a.path) as img:
        assert img.mode == "RGB" and max(img.size) == 512


def test_store_takes_first_gif_frame(tmp_path):
    frames = [Image.new("RGB", (40, 40), c) for c in ("red", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    stored = MediaStore(tmp_path).save(buf.getvalue())
    with Image.open(stored.path) as img:
        assert img.getpixel((5, 5))[0] > 200  # 第一帧是红色


def test_store_rejects_junk_and_huge(tmp_path):
    with pytest.raises(MediaError):
        MediaStore(tmp_path).save(b"not an image")
    with pytest.raises(MediaError):
        MediaStore(tmp_path, max_bytes=10).save(png())


def test_clean_description():
    assert media.clean("描述：  一只猫\n在睡觉  ") == "一只猫 在睡觉"
    assert len(media.clean("字" * 500)) == 200


# ---------- 模型层带图 ----------


def test_openai_compat_sends_image_parts(tmp_path):
    img = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(img, "JPEG")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "一张黑图"}}]})

    client = OpenAICompatClient(
        name="fallback", base_url="https://x", model="m", transport=httpx.MockTransport(handler)
    )
    client.chat([ChatMessage("user", "看看", images=(img,))])
    parts = seen["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "看看"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_codex_sends_local_image(tmp_path, monkeypatch):
    img = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(img, "JPEG")
    fake = str(Path(__file__).with_name("fake_codex_app_server.py"))
    backend = CodexAppServerBackend(command=[sys.executable, fake], workdir=tmp_path, timeout=10)
    try:
        result = backend.chat(
            [ChatMessage("system", "设定"), ChatMessage("user", "看", images=(img,))]
        )
        assert result.text == "我看到了 1 张图"
    finally:
        backend.close()


# ---------- QQ 看图规则 ----------


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.messages: dict[int, dict] = {}

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        if action == "get_msg":
            return self.messages.get(params["message_id"])
        return {"message_id": 9000 + len(self.sent)}


def brain(messages, json_mode):
    if messages[-1].images:
        if json_mode:  # 表情包：描述 + 情绪 + 能否收藏
            return json.dumps(
                {"description": "一只橘猫趴在键盘上", "emotion": "困", "usable": True},
                ensure_ascii=False,
            )
        return "一只橘猫趴在键盘上"
    return "好可爱！"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(brain)
    companion = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    fetched: list[str] = []

    def fake_fetch(url, *, max_bytes, timeout=30.0):
        fetched.append(url)
        return png()

    monkeypatch.setattr(media, "fetch", fake_fetch)
    clock = {"now": T0}
    cfg = QQConfig(bot_account=BOT, groups=[GROUP], unsolicited_probability=0)
    b = QQBot(companion, cfg, client=FakeClient(), rng=random.Random(1),
              clock=lambda: clock["now"])  # fmt: skip
    b.fetched, b.clock = fetched, clock
    yield b
    b.close()
    companion.close()


def image_msg(*, group=None, at_bot=False, text=None, reply=None, mid=1, sticker=False):
    segs = []
    if reply:
        segs.append({"type": "reply", "data": {"id": reply}})
    if at_bot:
        segs.append({"type": "at", "data": {"qq": BOT}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    else:
        segs.append({"type": "image", "data": {"url": f"https://img/{mid}", "file": f"f{mid}",
                                               "sub_type": 1 if sticker else 0}})  # fmt: skip
    ev = {"post_type": "message", "self_id": int(BOT), "user_id": int(FRIEND), "message_id": mid,
          "message_type": "group" if group else "private", "message": segs,
          "sender": {"nickname": "小明"}, "time": int(T0.timestamp())}  # fmt: skip
    if group:
        ev["group_id"] = int(group)
    return ev


def run(bot, *events):
    async def go():
        for ev in events:
            await bot.handle_event(ev)

    asyncio.run(go())


def contents(bot):
    return [e.content for e in bot.c.log.iter_events()]


def test_private_image_is_seen_by_main_model(bot):
    run(bot, image_msg())
    assert contents(bot)[0] == "[图片：一只橘猫趴在键盘上]"
    (event,) = [e for e in bot.c.log.iter_events() if e.actor.person_id != "self"]
    (digest,) = event.meta["images"]
    assert bot.c.media.path_for(digest).exists()
    assert event.ts == T0  # 用消息的发出时间


def test_group_image_ignored_when_not_active(bot):
    run(bot, image_msg(group=GROUP, mid=2))
    assert contents(bot) == ["[图片]"] and bot.fetched == []  # 不看也不下载


def test_group_image_seen_while_active(bot):
    run(bot, image_msg(group=GROUP, at_bot=True, text="在吗", mid=3))  # 她回了一句 → 活跃
    run(bot, image_msg(group=GROUP, mid=4, sticker=True))
    assert "[表情包：一只橘猫趴在键盘上]" in contents(bot)
    bot.clock["now"] = T0 + timedelta(seconds=61)
    run(bot, image_msg(group=GROUP, mid=5))
    assert contents(bot)[-1] == "[图片]"


def test_mentioned_with_image_and_quoted_image(bot):
    bot.client.messages[77] = {"message": [{"type": "image", "data": {"url": "https://img/77"}}]}
    run(bot, image_msg(group=GROUP, at_bot=True, text="这是什么", reply="77", mid=6))
    first = contents(bot)[0]
    assert first == "这是什么（引用的图片：一只橘猫趴在键盘上）"
    assert ("get_msg", {"message_id": 77}) in bot.client.sent


def test_forget_also_deletes_image(bot):
    run(bot, image_msg())
    (event,) = [e for e in bot.c.log.iter_events() if e.actor.person_id != "self"]
    path = bot.c.media.path_for(event.meta["images"][0])
    bot.c.forget([])  # 空操作不影响
    removed = bot.c.log.redact({event.id})
    bot.c._drop_orphan_images(removed)
    assert not path.exists()
