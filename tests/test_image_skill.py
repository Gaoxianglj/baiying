"""S-03 画画：后台生成、群聊共享限额、失败退还、额度不足暂停、QQ 先回话后发图。"""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from companion.bodies.qq.bot import QQBot
from companion.config import ImageSkillConfig, QQConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.models.openai_compat import ModelError
from companion.skills import SkillContext, SkillError
from companion.skills.image import ImageGen
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, FRIEND, GROUP = "1000", "3000", "4000"
T0 = datetime(2026, 10, 24, 23, tzinfo=UTC)
GROUP_CTX = SkillContext(body="qq", channel="group:1", person_id="p", private=False, owner=False)
PRIVATE_CTX = SkillContext(body="qq", channel="private:1", person_id="p", private=True, owner=False)


class FakePainter:
    def __init__(self, tmp_path, fail=False):
        self.tmp_path = tmp_path
        self.fail = fail
        self.prompts: list[str] = []
        self.references: list[tuple] = []

    def generate_image(self, prompt, references=()):
        self.prompts.append(prompt)
        self.references.append(tuple(references))
        if self.fail:
            raise ModelError("内容不合适")
        path = self.tmp_path / f"gen{len(self.prompts)}.png"
        Image.new("RGB", (64, 48), "orange").save(path, "PNG")
        return path, f"水彩：{prompt}"

    def close(self):
        pass


def make_skill(tmp_path, painter, clock, **cfg):
    return ImageGen(ImageSkillConfig(**cfg), lambda: painter, tmp_path / "quota.json",
                    clock=lambda: clock["now"])  # fmt: skip


def test_paints_in_background(tmp_path):
    painter = FakePainter(tmp_path)
    skill = make_skill(tmp_path, painter, {"now": T0})
    result = skill.run("画", "橘猫睡在键盘上", PRIVATE_CTX)
    assert painter.prompts == []  # 还没开始画
    assert "不要说已经画好了" in result.text and result.later is not None
    done = result.later()
    assert done.ok and done.images[0].exists() and done.text == "水彩：橘猫睡在键盘上"


def test_paintings_queue_up(tmp_path):
    painter = FakePainter(tmp_path)
    skill = make_skill(tmp_path, painter, {"now": T0}, queue_limit=2)
    first = skill.run("画", "玫瑰", PRIVATE_CTX)
    second = skill.run("画", "百合", PRIVATE_CTX)
    assert "前面还有 1 张" in second.text
    with pytest.raises(SkillError, match="已经排了 2 张"):
        skill.run("画", "第三张", PRIVATE_CTX)
    assert first.later().ok and second.later().ok
    assert painter.prompts == ["玫瑰", "百合"]
    assert skill.run("画", "第三张", PRIVATE_CTX).later is not None  # 画完就又能排了


def test_group_limit_shared_and_persisted(tmp_path):
    clock = {"now": T0}
    painter = FakePainter(tmp_path)
    skill = make_skill(tmp_path, painter, clock, group_limit=2, group_window_minutes=20)
    for i in range(2):
        skill.run("画", f"图{i}", GROUP_CTX).later()
    with pytest.raises(SkillError, match="20 分钟最多 2 张"):
        skill.run("画", "第三张", GROUP_CTX)
    skill.run("画", "私聊不限", PRIVATE_CTX).later()
    # 重启后限额还在
    again = make_skill(tmp_path, painter, clock, group_limit=2, group_window_minutes=20)
    assert "已画 2 / 2" in again.status()
    clock["now"] = T0 + timedelta(minutes=21)
    assert again.run("画", "过了窗口", GROUP_CTX).later().ok


def test_failure_is_refunded(tmp_path):
    skill = make_skill(tmp_path, FakePainter(tmp_path, fail=True), {"now": T0}, group_limit=1)
    done = skill.run("画", "x", GROUP_CTX).later()
    assert not done.ok and "内容不合适" in done.text
    assert "已画 0 / 1" in skill.status()


def test_paused_without_quota_or_subscription(tmp_path):
    skill = ImageGen(ImageSkillConfig(), lambda: FakePainter(tmp_path), tmp_path / "q.json",
                     quota_ok=lambda: False)  # fmt: skip
    with pytest.raises(SkillError, match="额度"):
        skill.run("画", "x", PRIVATE_CTX)
    assert not ImageGen(ImageSkillConfig(), None, tmp_path / "q.json").enabled()


# ---------- 接入聊天 ----------


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        return {"message_id": 9000 + len(self.sent)}


def test_qq_replies_first_then_sends_picture(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    replies = iter(["【画：一只橘猫】", "等我画一下哦"])
    gateway, chat = scripted_gateway(lambda m, j: next(replies))
    painter = FakePainter(tmp_path)
    skill = make_skill(tmp_path, painter, {"now": T0})
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder(), skills=[skill])
    cfg = QQConfig(bot_account=BOT, debounce_private_seconds=0)
    bot = QQBot(c, cfg, client=FakeClient(), rng=random.Random(1))  # 和回复一样用真实时钟
    ev = {"post_type": "message", "self_id": int(BOT), "user_id": int(FRIEND), "message_id": 1,
          "message_type": "private", "sender": {"nickname": "小明"},
          "message": [{"type": "text", "data": {"text": "画只猫给我"}}]}  # fmt: skip

    async def go():
        await bot.handle_event(ev)
        await bot.drain()

    try:
        asyncio.run(go())
        sends = [p["message"][0] for a, p in bot.client.sent if a == "send_private_msg"]
        assert sends[0]["data"]["text"] == "等我画一下哦"
        assert sends[1]["type"] == "image"
        assert "已经开始画了" in chat.requests[1][-1].content
        mine = [e for e in c.log.iter_events() if e.actor.person_id == "self"]
        assert [e.content for e in mine] == ["等我画一下哦", "[我画的图：水彩：一只橘猫]"]
        assert c.media.path_for(mine[-1].meta["images"][0]).exists()
    finally:
        bot.close()
        c.close()
