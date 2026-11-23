import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from companion.config import DiceSkillConfig, SkillsConfig
from companion.skills.base import SkillContext, SkillError, Skills
from companion.skills.dice import Dice, Expression, check_spec, command, grade, percentile
from companion.skills.native import NativeSkills
from tests.test_qq import GROUP, bot, msg, run  # noqa: F401 -- fixture

CTX = SkillContext("qq", "group:100", "person-a", False, False)


class Draws:
    def __init__(self, values):
        self.values, self.calls = iter(values), []

    def __call__(self, n):
        value = next(self.values)
        assert 0 <= value < n
        self.calls.append(n)
        return value


def rows(dice):
    with sqlite3.connect(dice.path) as connection:
        return connection.execute("SELECT id,result FROM rolls ORDER BY rowid").fetchall()


def test_expression_rolls_each_die_and_respects_parentheses_and_signs():
    draw = Draws([0, 5, 2])
    result = Expression(" (2D6 + 6) - (-d4+1) ").roll(draw)
    assert result["total"] == 15
    assert [t["values"] for t in result["terms"]] == [[1, 6], [3]]
    assert draw.calls == [6, 6, 4]
    assert Expression("d100").roll(lambda _: 99)["total"] == 100


@pytest.mark.parametrize("text", [
    "__import__('os').system('x')", "d6/0", "1d0", "0d6", "101d6", "d1000001",
    "50d6+51d6", "1d6 junk", "d6+", "(d6", "d6)", "d6**999", "1.5d6",
    "d6;d6", "d6d6", "(" * 14 + "d6" + ")" * 14, "1" * 161,
    "1000000000+1", "1000000001-1", "" , "1+" * 40 + "1",
])
def test_invalid_or_excessive_expressions_are_rejected_before_sampling(text):
    with pytest.raises(SkillError):
        Expression(text)


@pytest.mark.parametrize("skill,roll,level", [
    (49, 1, "大成功"), (49, 96, "大失败"), (50, 96, "失败"), (50, 99, "失败"),
    (50, 100, "大失败"), (60, 12, "极难成功"), (60, 13, "困难成功"),
    (60, 30, "困难成功"), (60, 31, "常规成功"), (60, 60, "常规成功"),
    (60, 61, "失败"), (53, 10, "极难成功"), (53, 26, "困难成功"),
    (150, 99, "常规成功"), (150, 100, "大失败"),
])
def test_coc_grade_and_fumble_use_full_skill_value(skill, roll, level):
    result = grade(roll, skill, "极难")
    assert result["level"] == level
    assert result["target"] == skill // 5
    assert result["passed"] is (level in {"大成功", "极难成功"})


def test_success_grade_is_separate_from_required_difficulty():
    assert grade(30, 60, "常规")["passed"]
    assert grade(30, 60, "困难")["passed"]
    assert not grade(30, 60, "极难")["passed"]
    assert grade(1, 1, "极难")["level"] == "大成功"


@pytest.mark.parametrize("mode,expected", [("奖1", 10), ("罚1", 100)])
def test_bonus_penalty_share_units_and_compare_zero_zero_as_one_hundred(mode, expected):
    draw = Draws([0, 0, 1])
    result = percentile(check_spec(f"60 {mode}"), draw)
    assert result["units"] == 0 and result["tens"] == [0, 10]
    assert result["candidates"] == [100, 10] and result["roll"] == expected
    assert draw.calls == [10, 10, 10]


def test_bonus_and_penalty_cancel_before_sampling():
    draw = Draws([7, 9])
    result = percentile(check_spec("60 困难 奖2 罚2 侦查"), draw)
    assert result["roll"] == 97 and len(result["tens"]) == 1
    assert len(draw.calls) == 2
    draw = Draws([4, 5, 2])
    result = percentile(check_spec("60 奖2 罚1"), draw)
    assert result["roll"] == 24 and result["candidates"] == [54, 24]


@pytest.mark.parametrize("arg", [
    "", "技能60", "-1", "0", "1000", "60 奖3", "60 奖1 奖1", "60 常规 困难",
    "60 奖-1", "60 奖1.5", "60 罚21 奖20", "60 " + "长" * 241,
])
def test_invalid_check_is_rejected_without_sampling(arg):
    with pytest.raises(SkillError):
        check_spec(arg)


def test_check_pipe_syntax_and_net_two_dice():
    spec = check_spec("60｜极难｜奖励5｜惩罚3｜侦查")
    assert spec == {"skill": 60, "difficulty": "极难", "bonus": 5, "penalty": 3,
                    "label": "侦查"}
    result = percentile(spec, Draws([2, 9, 0, 5]))
    assert result["roll"] == 2 and result["tens"] == [90, 0, 50]


def test_audit_reuses_request_after_restart_and_rejects_changed_parameters(tmp_path):
    dice = Dice(DiceSkillConfig(), tmp_path / "dice.sqlite", Draws([3]))
    ctx = replace(CTX, request_id="qq:123")
    first = dice.run("掷骰", "d6", ctx)
    reopened = Dice(DiceSkillConfig(), dice.path, Draws([]))
    assert reopened.run("掷骰", "d6", ctx).text == first.text
    assert len(rows(dice)) == 1 and json.loads(rows(dice)[0][1])["total"] == 4
    with pytest.raises(SkillError, match="参数发生变化"):
        reopened.run("掷骰", "d100", ctx)
    assert len(rows(dice)) == 1


def test_duplicate_requests_are_atomic_under_concurrent_delivery(tmp_path):
    dice = Dice(DiceSkillConfig(), tmp_path / "dice.sqlite", lambda n: 0)
    ctx = replace(CTX, request_id="qq:concurrent")
    with ThreadPoolExecutor(max_workers=6) as pool:
        outputs = list(pool.map(lambda _: dice.run("检定", "60", ctx).text, range(12)))
    assert len(set(outputs)) == 1 and len(rows(dice)) == 1


def test_history_and_request_keys_are_scoped_by_body_chat_and_person(tmp_path):
    dice = Dice(DiceSkillConfig(), tmp_path / "dice.sqlite", lambda _: 0)
    ctx = replace(CTX, request_id="same-id")
    dice.run("掷骰", "d6", ctx)
    rid = rows(dice)[0][0]
    for other in (replace(ctx, channel="group:other"), replace(ctx, body="other")):
        assert "没有" in dice.run("骰记录", rid, other).text
        dice.run("掷骰", "d6", other)
    dice.run("掷骰", "d6", replace(ctx, person_id="person-b"))
    assert len(rows(dice)) == 4
    assert rid in dice.run("骰记录", "", ctx).text
    with pytest.raises(SkillError):
        dice.run("骰记录", "999999", ctx)


def test_failed_audit_commit_never_publishes_a_result(tmp_path):
    dice = Dice(DiceSkillConfig(), tmp_path / "dice.sqlite", lambda _: 0)
    connection = dice._connect()
    connection.execute("CREATE TRIGGER deny_roll BEFORE INSERT ON rolls "
                       "BEGIN SELECT RAISE(FAIL, 'audit unavailable'); END")
    connection.commit()
    connection.close()
    skills = Skills([dice], SkillsConfig())
    _, result = skills.run(dice, "掷骰", "d6", CTX)
    assert not result.ok and "记录 " not in result.text and rows(dice) == []


def test_native_tool_replay_across_bridge_recreation_keeps_original_roll(tmp_path):
    dice = Dice(DiceSkillConfig(), tmp_path / "dice.sqlite", Draws([0]))
    skills = Skills([dice], SkillsConfig())
    bridge = NativeSkills(skills, CTX, lambda *_: None)
    name = next(n for n, marker in bridge._markers.items() if marker == "掷骰")
    first = bridge.tools.call(name, {"arg": "d6"}, "test-call")
    second_bridge = NativeSkills(skills, CTX, lambda *_: None)
    second = second_bridge.tools.call(name, {"arg": "d6"}, "test-call")
    assert first == second and len(rows(dice)) == 1


@pytest.mark.parametrize("text,expected", [
    (".r2d6+6", ("掷骰", "2d6+6")), (".ra60 困难 奖1", ("检定", "60 困难 奖1")),
    ("/r d100", ("掷骰", "d100")), ("/骰记录", ("骰记录", "")),
    (".rabbit", None), ("文字 .r d6", None), ("/random", None),
])
def test_direct_command_recognition(text, expected):
    assert command(text) == expected


def test_qq_dice_command_bypasses_model_and_replay_keeps_one_roll(bot):  # noqa: F811
    dice = next(s for s in bot.c.skills.all if isinstance(s, Dice))
    dice.randbelow = Draws([0, 5])
    run(bot, msg(".r 2d6+6", group=GROUP, mid=500))
    assert len(rows(dice)) == 1 and "= 13" in bot.client.texts()[0]
    assert not bot.chat.requests
    assert any("= 13" in e.content and e.actor.platform == "self"
               for e in bot.c.log.iter_events())
    run(bot, msg(".r 2d6+6", group=GROUP, mid=500))
    assert bot.client.texts()[0] == bot.client.texts()[1] and len(rows(dice)) == 1
    run(bot, msg("/骰记录", group=GROUP, mid=501))
    assert "= 13" in bot.client.texts()[-1]


def test_qq_check_is_exact_and_no_other_group_can_read_history(bot):  # noqa: F811
    dice = next(s for s in bot.c.skills.all if isinstance(s, Dice))
    dice.randbelow = Draws([0, 9, 0])
    run(bot, msg("/检定 49｜极难｜罚1｜侦查", group=GROUP, mid=500))
    text = bot.client.texts()[-1]
    assert "D100=100" in text and "大失败" in text and "目标9" in text
    run(bot, msg("/骰记录", group="999", mid=501))
    assert len(bot.client.texts()) == 1
    run(bot, msg("/骰记录", mid=502))
    assert "没有" in bot.client.texts()[-1]


def test_disabled_and_rate_limited_dice_do_not_sample(bot):  # noqa: F811
    dice = next(s for s in bot.c.skills.all if isinstance(s, Dice))
    dice.randbelow = Draws([])
    bot.c.config.skills.dice.enabled = False
    run(bot, msg("/掷骰 d6", group=GROUP, mid=500))
    assert "关闭" in bot.client.texts()[-1] and not dice.path.exists()
    bot.c.config.skills.dice.enabled = True
    bot.cfg.max_replies_per_minute = 1
    run(bot, msg("/掷骰 d6", group=GROUP, mid=501))
    assert not dice.path.exists()


def test_model_can_find_dice_markers_without_enabling_native_shell(tmp_path):
    dice = Dice(DiceSkillConfig(), tmp_path / "dice.sqlite", lambda _: 0)
    skills = Skills([dice], SkillsConfig())
    found = skills.find("【检定：60｜困难｜奖1｜侦查】", CTX)
    _, result = skills.run(found[1], found[2], found[3], CTX)
    assert result.ok and "D100=100" in result.text


def test_qq_retry_after_delivery_failure_reuses_persisted_result(bot):  # noqa: F811
    dice = next(s for s in bot.c.skills.all if isinstance(s, Dice))
    dice.randbelow = Draws([2])
    bot.client.replies["send_group_msg"] = TimeoutError("test connection failure")
    run(bot, msg("/掷骰 d6", group=GROUP, mid=500))
    bot.client.replies.pop("send_group_msg")
    run(bot, msg("/掷骰 d6", group=GROUP, mid=500))
    assert len(rows(dice)) == 1 and "= 3" in bot.client.texts()[-1]
