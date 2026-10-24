"""S-01 技能框架：标记解析、执行循环、权限、次数上限、QQ 先发“我查一下”。"""

import asyncio
import json
import random
from datetime import UTC, datetime

import pytest
from PIL import Image

from companion.bodies.qq.bot import QQBot
from companion.config import QQConfig, SkillsConfig, load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.models.codex_app_server import render_prompt
from companion.models.openai_compat import ChatMessage
from companion.skills import Skill, SkillContext, SkillError, SkillResult, Skills
from tests.fakes import FakeEmbedder, scripted_gateway

BOT, OWNER, FRIEND, GROUP = "1000", "2000", "3000", "4000"
T0 = datetime(2026, 10, 24, 23, tzinfo=UTC)


class Echo(Skill):
    name, label = "echo", "回声"
    markers = {"搜索": "【搜索：关键词】查点东西"}

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.image = None

    def run(self, marker, arg, ctx):
        self.calls.append(arg)
        if arg == "坏掉":
            raise SkillError("网站打不开")
        return SkillResult(f"关于{arg}的结果", images=[self.image] if self.image else [])


class OwnerOnly(Skill):
    name, label = "secret", "主人专用"
    markers = {"干活": "【干活：任务】"}

    def allowed(self, ctx):
        return ctx.owner and ctx.private

    def run(self, marker, arg, ctx):
        return SkillResult("done")


def ctx(**kw):
    base = {"body": "qq", "channel": "private:1", "person_id": "p", "private": True,
            "owner": False}  # fmt: skip
    return SkillContext(**(base | kw))


# ---------- 解析 ----------


def test_find_and_strip():
    skills = Skills([Echo(), OwnerOnly()], SkillsConfig())
    before, skill, marker, arg = skills.find("我查一下【搜索： 今天的天气 】", ctx())
    assert (before, skill.name, marker, arg) == ("我查一下", "echo", "搜索", "今天的天气")
    assert skills.find("[搜索:猫]", ctx())[3] == "猫"
    # 不允许的技能不会被执行，但标记会被去掉；表情标记保留给表情包
    assert skills.find("【干活：删库】", ctx()) is None
    assert skills.strip_markers("好的【干活：删库】【表情：开心】") == "好的【表情：开心】"
    assert skills.find("【干活：x】", ctx(owner=True))[1].name == "secret"


def test_prompt_lists_only_available():
    skills = Skills([Echo(), OwnerOnly()], SkillsConfig())
    text = skills.prompt(ctx())
    assert "【搜索：关键词】" in text and "【干活" not in text
    assert Skills([Echo()], SkillsConfig(enabled=False)).prompt(ctx()) is None
    assert skills.prompt(None) is None


def test_role_skill_rendering(tmp_path):
    assert ChatMessage("skill", "x").to_openai() == {"role": "user", "content": "x"}
    assert "<技能结果>\nx\n</技能结果>" in render_prompt([ChatMessage("skill", "x")], None)


# ---------- 执行循环 ----------


class Brain:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, messages, json_mode):
        self.requests.append(messages)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def make(tmp_path, brain, skills):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    gateway, _ = scripted_gateway(brain)
    return Companion(config, gateway=gateway, embedder=FakeEmbedder(), skills=skills)


def test_skill_round_trip(tmp_path):
    echo = Echo()
    brain = Brain("我查一下【搜索：猫】", "查到了，猫很可爱")
    c = make(tmp_path, brain, [echo])
    said = []
    try:
        r = c.reply(body="cli", channel="cli:x", account="x", text="猫怎么样", now=T0,
                    on_interim=said.append)  # fmt: skip
        assert said == ["我查一下"] and echo.calls == ["猫"]
        assert r.text == "查到了，猫很可爱" and [k.skill for k in r.skill_calls] == ["echo"]
        system, *_, said_msg, result_msg = brain.requests[1]
        assert "【技能】" in system.content and "【搜索：关键词】" in system.content
        assert said_msg.role == "assistant" and said_msg.content == "我查一下【搜索：猫】"
        assert result_msg.role == "skill" and "关于猫的结果" in result_msg.content
        mine = [e for e in c.log.iter_events() if e.actor.person_id == "self"]
        assert [e.content for e in mine] == ["我查一下", "查到了，猫很可爱"]
        assert mine[-1].meta["skills"] == [{"skill": "echo", "arg": "猫", "ok": True}]
        (row,) = [
            json.loads(x)
            for x in (c.config.data_dir / "skills.jsonl").read_text("utf-8").splitlines()
        ]
        assert row["skill"] == "echo" and row["ok"] is True
    finally:
        c.close()


def test_failure_is_reported_back(tmp_path):
    brain = Brain("【搜索：坏掉】", "没查到呢")
    c = make(tmp_path, brain, [Echo()])
    try:
        r = c.reply(body="cli", channel="cli:x", account="x", text="查", now=T0)
        assert r.text == "没查到呢" and r.skill_calls[0].ok is False
        assert "网站打不开" in brain.requests[1][-1].content
    finally:
        c.close()


def test_round_limit_strips_leftover_marker(tmp_path):
    brain = Brain("嗯【搜索：再来】")  # 每次都想再搜
    c = make(tmp_path, brain, [Echo()])
    c.config.skills.max_rounds = 2
    try:
        r = c.reply(body="cli", channel="cli:x", account="x", text="查", now=T0)
        assert len(r.skill_calls) == 2 and len(brain.requests) == 3
        assert r.text == "嗯"
    finally:
        c.close()


def test_not_allowed_skill_never_runs(tmp_path):
    brain = Brain("好【干活：删东西】")
    c = make(tmp_path, brain, [OwnerOnly()])
    try:
        mid = c.observe(body="qq", channel="group:1", account="9", text="帮我删东西", now=T0)
        r = c.respond(mid, now=T0, skill_ctx=ctx(private=False))
        assert r.skill_calls == [] and r.text == "好"
    finally:
        c.close()


# ---------- QQ ----------


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def call(self, action, params, timeout=30.0):
        self.sent.append((action, params))
        return {"message_id": 9000 + len(self.sent)}


@pytest.mark.parametrize("private", [True, False])
def test_qq_sends_interim_then_answer_and_image(tmp_path, private):
    echo = Echo()
    echo.image = tmp_path / "out.jpg"
    Image.new("RGB", (8, 8)).save(echo.image, "JPEG")
    brain = Brain("等我一下【搜索：画】", "好啦")
    c = make(tmp_path, brain, [echo])
    cfg = QQConfig(bot_account=BOT, owner_accounts=[OWNER], groups=[GROUP],
                   unsolicited_probability=0, debounce_private_seconds=0,
                   debounce_group_seconds=0)  # fmt: skip
    bot = QQBot(c, cfg, client=FakeClient(), rng=random.Random(1), clock=lambda: T0)
    segs = [{"type": "text", "data": {"text": "白璎 画个猫"}}]
    ev = {"post_type": "message", "self_id": int(BOT), "user_id": int(FRIEND), "message_id": 1,
          "message_type": "private" if private else "group", "message": segs,
          "sender": {"nickname": "小明"}}  # fmt: skip
    if not private:
        ev["group_id"] = int(GROUP)

    async def go():
        await bot.handle_event(ev)
        await bot.drain()

    try:
        asyncio.run(go())
    finally:
        bot.close()
        c.close()
    sends = [p["message"] for a, p in bot.client.sent if a.startswith("send_")]
    texts = ["".join(s["data"].get("text", "") for s in m if s["type"] == "text") for m in sends]
    assert texts[:2] == ["等我一下", "好啦"]
    assert sends[2][0]["type"] == "image" and "sub_type" not in sends[2][0]["data"]
