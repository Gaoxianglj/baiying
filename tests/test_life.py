"""E-03 今日状态（AI 版日常）、E-04 心情。"""

import json
from datetime import datetime, timedelta

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper
from companion.life import DayBook, day_key, slot_of, split_mood
from companion.life.mood import current_mood, render_mood
from companion.memory import Episode, MemoryLayout, init_memory
from tests.fakes import FakeEmbedder, scripted_gateway


def local(*args) -> datetime:
    """本地时间（测试不依赖机器所在时区）。"""
    return datetime(*args).astimezone()


NOON = local(2026, 10, 27, 12, 30)

PLAN = {
    "mood": "安安静静的",
    "reading": "《人间草木》",
    "slots": {
        "凌晨": "整理昨天的记忆",
        "早上": "翻了几页书",
        "上午": "读《人间草木》写葡萄的那篇",
        "中午": "看群里大家聊午饭",
        "下午": "抄下几个好听的词",
        "傍晚": "发了会儿呆",
        "晚上": "写一首很短的小诗",
        "深夜": "把今天读到的句子再看一遍",
    },
    "small_things": ["读到“一年好景君须记”"],
}


class Brain:
    def __init__(self) -> None:
        self.reply = "好呀"
        self.plan_calls = 0
        self.plan = PLAN

    def __call__(self, messages, json_mode):
        system = messages[0].content
        if json_mode and "AI 版日常" in system:
            self.plan_calls += 1
            return json.dumps(self.plan, ensure_ascii=False)
        if json_mode:
            return json.dumps({"episode": None, "facts": []})
        if "日记" in system:
            return "今天读了书，心情不错。"
        return self.reply


@pytest.fixture
def c(tmp_path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(config.memory_dir), "白璎", git=False)
    brain = Brain()
    gateway, chat = scripted_gateway(brain)
    comp = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    comp.brain, comp.chat = brain, chat
    yield comp
    comp.close()


def say(c, text, when, *, account="xiang", name="祥", channel=None):
    return c.reply(body="qq", channel=channel or f"private:{account}", account=account,
                   text=text, now=when, display_name=name)  # fmt: skip


def last_system(c) -> str:
    return c.chat.requests[-1][0].content


# ---- 一天的划分 ----


def test_day_starts_at_sleep_hour():
    assert day_key(local(2026, 10, 27, 3, 59), 4).isoformat() == "2026-10-26"
    assert day_key(local(2026, 10, 27, 4, 0), 4).isoformat() == "2026-10-27"
    assert slot_of(local(2026, 10, 27, 4, 0)) == "凌晨"
    assert slot_of(local(2026, 10, 27, 12, 30)) == "中午"
    assert slot_of(local(2026, 10, 27, 23, 59)) == "晚上"
    assert slot_of(local(2026, 10, 28, 1, 0)) == "深夜"


def test_daybook_entries_span_days(tmp_path):
    book = DayBook(tmp_path, 4)
    book.append(local(2026, 10, 26, 22), "moods", {"ts": "a", "word": "累"})
    book.append(local(2026, 10, 27, 9), "moods", {"ts": "b", "word": "开心"})
    assert [e["word"] for e in book.entries(NOON, "moods")] == ["开心"]
    assert [e["word"] for e in book.entries(NOON, "moods", days=2)] == ["累", "开心"]


def test_recent_days_include_journal_only_days(c):
    c.layout.journal_dir.mkdir(parents=True, exist_ok=True)
    (c.layout.journal_dir / "2026-10-25.md").write_text("# 2026-10-25\n\n群里很热闹。\n", "utf-8")
    c.life.note_mood("开心", "读完了书", channel="self:life", person_id=None,
                     now=local(2026, 10, 26, 21))  # fmt: skip
    recent = c.life.recent_text(NOON)
    assert recent.index("2026-10-25\n日记：群里很热闹。") < recent.index("2026-10-26\n心情变化：")


# ---- 心情 ----


@pytest.mark.parametrize(
    ("text", "clean", "mood"),
    [
        ("好呀【心情：小得意｜被夸了】", "好呀", ("小得意", "被夸了")),
        ("嗯[心情:开心]", "嗯", ("开心", "")),
        ("【心情：累｜a】哈哈【心情：开心｜b】", "哈哈", ("开心", "b")),
        ("今天心情：不错", "今天心情：不错", None),
    ],
)
def test_split_mood(text, clean, mood):
    assert split_mood(text) == (clean, mood)


def test_mood_decays_and_negative_fades_faster():
    def at(word, minutes):
        entries = [{"ts": NOON.isoformat(), "word": word, "channel": "group:1"}]
        return current_mood(entries, NOON + timedelta(minutes=minutes),
                            half_life_minutes=90, negative_half_life_minutes=45)  # fmt: skip

    assert at("开心", 0).strength == pytest.approx(1.0)
    assert at("开心", 90).strength == pytest.approx(0.5)
    assert at("开心", 300) is None  # 回到基调
    assert at("委屈", 90).strength == pytest.approx(0.25)
    assert at("委屈", 120) is None


def test_mood_cause_stays_private():
    entries = [{"ts": NOON.isoformat(), "word": "开心", "cause": "祥夸我", "channel": "private:1"}]
    mood = current_mood(entries, NOON, half_life_minutes=90, negative_half_life_minutes=45)
    assert "因为祥夸我" in render_mood(mood, None, "private:1")
    other = render_mood(mood, None, "group:9")
    assert "祥" not in other and "因为一件私下的事" in other
    entries[0]["channel"] = "group:2"
    mood = current_mood(entries, NOON, half_life_minutes=90, negative_half_life_minutes=45)
    assert "因为另一个群里的事" in render_mood(mood, None, "group:9")


def test_mood_marker_is_recorded_and_carried_across_chats(c):
    c.brain.reply = "嘿嘿，那我收下啦【心情：小得意｜祥夸我的诗写得好】"
    r = say(c, "你的诗写得真好", NOON)
    assert r.text == "嘿嘿，那我收下啦"
    events = [e.content for e in c.log.iter_events()]
    assert "心情" not in events[-1]

    c.brain.reply = "在呢"
    say(c, "在吗", NOON + timedelta(minutes=10))
    assert "小得意（因为祥夸我的诗写得好）" in last_system(c)
    # 换一个人私聊：心情还在，但不说原因
    say(c, "白璎在吗", NOON + timedelta(minutes=20), account="ming", name="小明")
    system = last_system(c)
    assert "小得意" in system and "祥" not in system.split("【你现在的心情】")[1]
    # 过了几个小时：回到基调
    say(c, "晚上好", NOON + timedelta(hours=6))
    assert "【你现在的心情】" not in last_system(c)


def test_silent_turn_still_records_mood(c):
    c.brain.reply = "【不回复】【心情：无奈｜群里又在吵】"
    event = c.observe(body="qq", channel="group:1", account="a", text="吵什么吵", now=NOON)
    reply = c.respond(event, now=NOON, mode="strict")
    assert reply.silent
    assert c.life.mood(NOON).word == "无奈"


def test_mood_rule_in_stable_part(c):
    say(c, "你好", NOON)
    system = last_system(c)
    assert system.index("【心情：") < system.index("【要不要接话】")


# ---- 今日状态 ----


def test_plan_generated_once_and_shown_up_to_now(c):
    plan = c.life.ensure_plan(NOON)
    assert plan is not None and plan.reading == "《人间草木》"
    assert c.life.ensure_plan(NOON + timedelta(hours=1)) is plan or c.brain.plan_calls == 1
    assert c.brain.plan_calls == 1
    saved = json.loads((c.layout.self_dir / "days" / "2026-10-27.json").read_text("utf-8"))
    assert saved["plan"]["mood"] == "安安静静的"

    say(c, "在干嘛呀", NOON)
    system = last_system(c)
    today = system.split("【你的今天】")[1]
    assert "- 上午：读《人间草木》写葡萄的那篇" in today
    assert "- 中午（现在）：看群里大家聊午饭" in today
    assert "接下来（下午）打算：抄下几个好听的词" in today
    assert "写一首很短的小诗" not in today  # 更晚的安排不提前说
    assert "今天的心情基调：安安静静的" in today
    # 没有心情变化时，心情就是当天的基调
    assert "就是今天的基调：安安静静的" in system


def test_plan_failure_backs_off(c):
    c.brain.plan = {"mood": "x", "reading": "y", "slots": {}, "small_things": []}
    assert c.life.ensure_plan(NOON) is None
    assert c.life.ensure_plan(NOON + timedelta(minutes=10)) is None
    assert c.brain.plan_calls == 1  # 30 分钟内不再重试
    c.brain.plan = PLAN
    assert c.life.ensure_plan(NOON + timedelta(minutes=40)) is not None


def add_episode(c, summary, start, channel, participants):
    ep = Episode(started_at=start, ended_at=start + timedelta(minutes=20), body="qq",
                 channel=channel, summary=summary, participants=participants)  # fmt: skip
    c.index.add_episode(ep, now=start + timedelta(minutes=50))  # 会话结束 30 分钟后才整理


def test_happenings_respect_privacy(c):
    say(c, "早", local(2026, 10, 27, 8))  # 给祥建档
    pid_xiang = next(e.actor.person_id for e in c.log.iter_events() if e.actor.person_id != "self")
    add_episode(c, "和大家在群里聊披萨尺寸", local(2026, 10, 27, 9), "group:1", ["self"])
    add_episode(c, "祥私下说了考试的烦恼", local(2026, 10, 27, 10), "private:xiang",
                ["self", pid_xiang])  # fmt: skip
    add_episode(c, "昨天的事", local(2026, 10, 26, 20), "group:1", ["self"])
    c.life.days.append(NOON, "activities", {"ts": local(2026, 10, 27, 11).isoformat(),
                                            "kind": "explore", "title": "查了雪花为什么是六角形",
                                            "summary": "水分子的形状决定的"})  # fmt: skip

    say(c, "今天怎么样", NOON)
    mine = last_system(c).split("【你的今天】")[1]
    assert "09:00 和大家在群里聊披萨尺寸" in mine
    assert "考试的烦恼" in mine
    assert "（你自己）查了雪花为什么是六角形：水分子的形状决定的" in mine
    assert "昨天的事" not in mine

    say(c, "今天怎么样", NOON, account="ming", name="小明")
    theirs = last_system(c).split("【你的今天】")[1]
    assert "披萨" in theirs and "雪花" in theirs
    assert "考试" not in theirs  # 别人的私聊不提


# ---- 睡眠 ----


def test_sleep_runs_once_per_day_and_dates_journal(c):
    sleeper = Sleeper(c)
    evening = local(2026, 10, 26, 21)
    say(c, "晚安", evening)
    c.life.note_mood("开心", "读完了一本书", channel="self:life", person_id=None, now=evening)
    add_episode(c, "和祥道了晚安", evening, "private:xiang", ["self"])

    morning = local(2026, 10, 27, 4, 30)
    assert sleeper.due(morning)
    report = sleeper.run(now=morning)
    journal = c.layout.journal_dir / "2026-10-26.md"  # 写的是前一天
    assert report.journal == str(journal)
    assert "开心" in c.chat.requests[-2][-1].content  # 日记素材里有当天的心情
    assert report.planned and c.life.days.plan(morning) is not None
    assert not sleeper.due(local(2026, 10, 27, 15))  # 重启后不会再睡一次
    assert sleeper.due(local(2026, 10, 28, 4, 5))


def test_journal_appends_instead_of_overwriting(c):
    sleeper = Sleeper(c)
    path = c.layout.journal_dir / "2026-10-27.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 2026-10-27\n\n早上写的日记。\n", encoding="utf-8")
    add_episode(c, "下午在群里聊天", local(2026, 10, 27, 15), "group:1", ["self"])
    c.index.set_meta("sleep:last", local(2026, 10, 27, 4, 30).isoformat())
    sleeper.run(now=local(2026, 10, 27, 20))
    text = path.read_text("utf-8")
    assert text.startswith("# 2026-10-27\n\n早上写的日记。")
    assert "（20:00 补记）" in text and "今天读了书" in text
