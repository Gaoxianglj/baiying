"""E-01 提示词整理：缓存友好的顺序、末尾提醒、换个说法。"""

from datetime import UTC, datetime, timedelta

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.context import _opening
from companion.memory import MemoryLayout, init_memory
from companion.skills import SkillContext, SkillResult
from tests.fakes import FakeEmbedder, scripted_gateway

T0 = datetime(2026, 10, 26, 12, tzinfo=UTC)


class Brain:
    def __init__(self) -> None:
        self.replies = ["哈哈，好呀", "诶？真的吗", "嗯，我记得", "那就这样吧"]
        self.n = 0

    def __call__(self, messages, json_mode):
        self.n += 1
        return self.replies[(self.n - 1) % len(self.replies)]


@pytest.fixture
def companion(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    layout = MemoryLayout(config.memory_dir)
    init_memory(layout, "白璎", git=False)
    (layout.self_dir / "seed.md").write_text("# 种子\n\n- 我叫白璎，是一个 AI。\n", "utf-8")
    gateway, chat = scripted_gateway(Brain())
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    c.chat = chat
    yield c
    c.close()


def say(c, text, when):
    return c.reply(body="cli", channel="cli:test", account="xiang", text=text, now=when,
                   display_name="祥")  # fmt: skip


def test_stable_parts_first_and_minute_time_last(companion):
    say(companion, "晚上好", T0)
    messages = companion.chat.requests[0]
    system = messages[0].content
    order = ["我叫白璎", "【说话时注意】", "【要不要接话】", "你正在电脑的命令行里和祥私聊"]
    positions = [system.index(s) for s in order]
    assert positions == sorted(positions)
    local = T0.astimezone()
    assert system.rstrip().endswith("你正在电脑的命令行里和祥私聊。")
    assert f"现在是 {local:%Y-%m-%d %H:%M}" in system.splitlines()[-1]
    # 末尾再提醒一次，离生成最近
    assert messages[-1].role == "system" and "动笔前" in messages[-1].content
    assert messages[-2].role == "user" and "晚上好" in messages[-2].content


def test_same_prefix_across_minutes(companion):
    """同一个人、不同时刻：变化只出现在后半段，前面的设定一字不差（能命中前缀缓存）。"""
    say(companion, "在吗", T0)
    say(companion, "在吗", T0 + timedelta(minutes=3))
    a, b = (r[0].content for r in companion.chat.requests)
    stable_end = a.index("【要不要接话】")
    assert a[:stable_end] == b[:stable_end]
    assert a != b


def test_recent_openings_listed(companion):
    for i in range(3):
        say(companion, f"第{i}句", T0 + timedelta(minutes=i))
    assert "【换个说法】" not in companion.chat.requests[2][0].content  # 还不到三条
    say(companion, "第3句", T0 + timedelta(minutes=3))
    system = companion.chat.requests[3][0].content
    assert "「哈哈」「诶」「嗯」" in system


def test_report_has_no_reply_reminder(companion):
    say(companion, "帮我整理一下", T0)
    pid = next(
        e.actor.person_id for e in companion.log.iter_events() if e.actor.person_id != "self"
    )
    ctx = SkillContext("cli", "cli:test", pid, private=True, owner=True, now=T0)
    companion.report(ctx, "agent", SkillResult("整理好了", report=True))
    last = companion.chat.requests[-1]
    assert last[-1].role == "skill"
    assert not any("动笔前" in m.content for m in last)


@pytest.mark.parametrize(
    ("text", "head"),
    [
        ("哈哈，好呀", "哈哈"),
        ("诶？真的吗", "诶"),
        ("今天读完了一整本书", "今天读完了一"),
        ("好", "好"),
    ],
)
def test_opening(text, head):
    assert _opening(text) == head


def test_qq_group_mention_rule(companion):
    def system(body, channel):
        bundle = companion.context.build(
            person_id="xiang", body=body, channel=channel, query="在吗", now=T0
        )
        return bundle.system_prompt

    assert "写“@”加对方的 QQ 号或群里显示的名字" in system("qq", "group:4000")
    assert "真正的 @" not in system("qq", "private:2000")
    assert "真正的 @" not in system("cli", "cli:test")


def test_preferences_style_and_familiarity_in_prompt(companion):
    (companion.layout.self_dir / "preferences.md").write_text(
        "# 白璎 的喜好与习惯\n\n## 穿搭与随身物\n\n- 常穿月白色的长裙。\n", "utf-8"
    )
    (companion.layout.self_dir / "style.md").write_text(
        "# 白璎 的表达风格\n\n## 熟络后的破壳与打趣\n\n- 熟人面前偶尔句末带（）。\n", "utf-8"
    )
    say(companion, "你好呀", T0)
    pid = companion.people.resolve("cli", "xiang")
    companion.people.set_familiarity(pid, "亲近")

    bundle = companion.context.build(
        person_id=pid, body="cli", channel="cli:test", query="今天穿什么", now=T0
    )
    order = [
        "【你的种子（核心性格与底线，必须遵守）】",
        "【你的喜好与习惯",
        "常穿月白色的长裙",
        "【你的表达习惯】",
        "熟人面前偶尔句末带（）",
        "当前熟络度：亲近",
    ]
    positions = [bundle.system_prompt.index(s) for s in order]
    assert positions == sorted(positions)
