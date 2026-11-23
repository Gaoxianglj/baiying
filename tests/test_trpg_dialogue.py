import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from companion.skills.base import SkillError
from tests.fakes import scripted_gateway
from tests.test_trpg import GM, PC, Dice, game  # noqa: F401
from tests.test_trpg_host import handle, hosted  # noqa: F401


def voices(c, replies):
    answers = iter(replies)
    public, spoken = scripted_gateway(lambda *_: next(answers))
    private, classified = scripted_gateway(lambda *_: '{"action_id":""}')
    c.config.trpg.narration = True
    c.narrator.factory = lambda: public
    c.host.planner.factory = lambda: private
    return spoken, classified


def test_unmatched_spray_becomes_baiying_question_and_followup_uses_history(hosted):  # noqa: F811
    c, s, cid = hosted
    spoken, classified = voices(
        c,
        [
            '{"kind":"reply","text":"你想用哪件物品喷向他？是打算泼洒液体，还是点燃后喷射？"}',
            '{"kind":"reply","text":"明白了，你指的是那瓶液体。先告诉我准备用什么把它喷出去。"}',
        ],
    )
    original = s.campaign(PC)
    first = handle(c, "对他使用易燃物喷射", "spray")
    assert "哪件物品" in first["text"] and first["spoken"]
    assert "不能按现有" not in first["text"]
    assert handle(c, "对他使用易燃物喷射", "spray")["replay"]
    second = handle(c, "就是那瓶液体", "followup")
    assert "喷出去" in second["text"]
    data = json.loads(spoken.requests[-1][-1].content)
    assert any("哪件物品" in str(turn) for turn in data["最近主持问答"])
    assert len(spoken.requests) == 2 and len(classified.requests) == 1
    assert s.campaign(PC) == original
    assert all(
        "ABSOLUTE-KP-SECRET" not in message.content for call in spoken.requests for message in call
    )


def test_system_rejection_goes_back_to_baiying_for_one_repair(hosted):  # noqa: F811
    c, s, cid = hosted
    spoken, _ = voices(
        c,
        [
            '{"kind":"check","skill":"喷火魔法","reason":"喷火"}',
            '{"kind":"reply","text":"你这张卡还没有喷火能力。你是想用手边的物品实现这个动作吗？"}',
        ],
    )
    version = s.campaign(PC)["version"]
    result = handle(c, "对他使用易燃物喷射", "repair")
    assert "手边的物品" in result["text"]
    assert "没有这个技能" in json.loads(spoken.requests[1][-1].content)["你的工具报告"]
    assert len(spoken.requests) == 2
    assert s.campaign(PC)["version"] == version
    with c.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0


def test_baiying_can_propose_new_ordinary_check_and_player_confirms_naturally(hosted):  # noqa: F811
    c, s, cid = hosted
    spoken, _ = voices(
        c,
        [
            '{"kind":"check","skill":"侦查","reason":"观察桌脚附近有没有动静",'
            '"difficulty":"常规","bonus":0,"penalty":0,"minutes":1}',
            "我来判断这个做法。可以先观察桌脚附近，但有可能花一分钟却没看清。要试试吗？",
            "骰点是11，侦查通过了。我接着根据你实际能看到的情况处理。",
        ],
    )
    s.rng = Dice([1, 1])
    before = s.pc(s.campaign(PC), PC.person_id)[1]
    proposed = handle(c, "我趴下看桌脚附近有没有动静", "new-check")
    assert "侦查 60" in proposed["text"] and "投吧" in proposed["text"]
    assert not s.rng.calls
    assert "/团" not in proposed["text"]
    with c.store.connect() as conn:
        assert conn.execute("SELECT status FROM checks").fetchone()[0] == "ready"
    rolled = handle(c, "来吧", "natural-confirm")
    assert "11" in rolled["text"] and len(s.rng.calls) == 2
    assert handle(c, "来吧", "natural-confirm")["replay"]
    assert len(s.rng.calls) == 2 and len(spoken.requests) == 3
    after = s.pc(s.campaign(PC), PC.person_id)[1]
    assert after["HP"] == before["HP"] and after["SAN"] == before["SAN"]
    assert s.campaign(PC)["state"]["scene"] == "hall"
    assert s.campaign(PC)["state"]["known"] == []


def test_dialogue_can_choose_existing_action_without_showing_raw_tool_report(hosted):  # noqa: F811
    c, s, cid = hosted
    spoken, _ = voices(
        c,
        [
            '{"kind":"action","plan":{"action_id":"drawer"}}',
            "我顺着你的意思检查了抽屉，里面那条线索现在能看清了。",
        ],
    )
    result = handle(c, "查看这边的柜子", "pick-action")
    assert result["changed"] and result["spoken"]
    assert result["text"] == "我顺着你的意思检查了抽屉，里面那条线索现在能看清了。"
    assert s.campaign(PC)["state"]["known"] == ["clue"]
    assert len(spoken.requests) == 2


def test_generic_check_cannot_inject_damage_items_or_admin_authority(hosted):  # noqa: F811
    c, s, cid = hosted
    voices(
        c,
        [
            '{"kind":"check","skill":"侦查","reason":"观察","HP":999,"actor":"gm"}',
            '{"kind":"reply","text":"我先确认你实际要观察的位置，再决定怎么检定。"}',
        ],
    )
    before = s.campaign(PC)
    handle(c, "观察周围", "no-injection")
    assert s.campaign(PC) == before
    with pytest.raises(SkillError):
        s.execute(
            PC,
            "主持检定",
            {"actor": PC.person_id, "plan": {"kind": "check", "skill": "侦查", "reason": "观察"}},
        )


def test_all_public_queries_are_processed_by_baiying_not_raw_status(hosted):  # noqa: F811
    c, s, cid = hosted
    spoken, _ = voices(c, ["我看过了，你目前的体力和理智都保持原样，我们还在走廊。"])
    result = handle(c, "查看状态", "status-in-voice")
    assert result["text"].startswith("我看过了")
    assert cid not in result["text"] and "/团" not in result["text"]
    assert "HP" in json.loads(spoken.requests[0][-1].content)["本次程序结论"]


def test_pause_and_status_still_work_while_baiying_dialogue_is_waiting(hosted):  # noqa: F811
    c, s, cid = hosted
    entered, release = threading.Event(), threading.Event()

    def respond(*_):
        entered.set()
        assert release.wait(5)
        return '{"kind":"reply","text":"你准备使用哪件物品？"}'

    c.config.trpg.narration = True
    c.narrator.factory = lambda: scripted_gateway(respond)[0]
    c.host.planner.factory = lambda: scripted_gateway(lambda *_: '{"action_id":""}')[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(handle, c, "易燃物喷射", "waiting")
        try:
            assert entered.wait(3)
            status = pool.submit(handle, c, "查看状态", "status").result(timeout=2)
            assert "HP 10/10" in status["text"]
            paused = pool.submit(handle, c, "暂停跑团", "pause").result(timeout=2)
            assert "跑团已暂停" in paused["text"]
        finally:
            release.set()
        slow.result(timeout=2)
    assert s.campaign(PC)["status"] == "paused"


def test_check_announcement_reuses_baiying_proposal_and_keeps_real_threshold(hosted):  # noqa: F811
    c, s, cid = hosted
    spoken, _ = voices(
        c,
        [
            '{"kind":"check","skill":"侦查","reason":"观察桌脚",'
            '"difficulty":"困难","say":"我想先仔细看看桌脚，可以吗？"}',
        ],
    )
    s.rng = Dice()
    result = handle(c, "仔细观察桌脚", "one-public-call")
    assert "我想先仔细看看桌脚" in result["text"]
    assert "目标≤30" in result["text"]
    assert len(spoken.requests) == 1 and not s.rng.calls


def test_unrolled_check_cannot_be_presented_as_a_dice_result(hosted):  # noqa: F811
    c, s, cid = hosted
    voices(
        c,
        [
            '{"kind":"check","skill":"侦查","reason":"观察桌脚","say":"骰点是11，我替你投过了。"}',
        ],
    )
    s.rng = Dice()
    result = handle(c, "观察桌脚", "no-fake-dice")
    assert "骰点是11" not in result["text"] and "等你同意后再投" in result["text"]
    assert not s.rng.calls


def test_restore_can_be_confirmed_with_a_natural_yes(hosted):  # noqa: F811
    from companion.trpg.store import Store

    c, s, cid = hosted
    s.execute(GM, "暂停")
    with c.store.connect(write=True) as conn:
        campaign = s.campaign(GM, conn=conn)
        campaign["state"]["needs_confirmation"] = True
        Store.save(conn, campaign)
    handle(c, "继续跑团", "natural-restore-prompt")
    handle(c, "好的", "natural-restore-yes")
    assert s.campaign(PC)["status"] == "active"
