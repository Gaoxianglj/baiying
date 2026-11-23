import json
from dataclasses import replace

import pytest

from companion.skills.base import SkillError
from companion.trpg import rules
from companion.trpg.host_rules import actions_for
from companion.trpg.store import Store
from tests.fakes import scripted_gateway
from tests.test_qq import bot, msg, run  # noqa: F401
from tests.test_trpg import GM, OTHER, PC, Dice, game  # noqa: F401


@pytest.fixture
def hosted(game):  # noqa: F811
    c, s, cid = game
    c.narrator.factory = lambda: scripted_gateway(lambda *_: "我来接着处理。")[0]
    path = c.access.folder(PC.channel) / "模组.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## PDF 第 1 页\nPRIVATE-SOURCE-SECRET。抽屉里有纸。", "utf-8")
    c.library.import_file(GM, cid, path, "kp")
    with s.store.connect() as conn:
        sha = conn.execute("SELECT sha FROM documents LIMIT 1").fetchone()[0]
    s.execute(
        GM,
        "主持授权",
        {
            "enabled": True,
            "allow_private_model": True,
            "sources": [sha],
            "consent": "用户明确授权专用模型读取本模组",
        },
    )
    return c, s, cid


def handle(c, text, key, ctx=PC):
    return c.host.handle(replace(ctx, request_id=key), text)


def test_host_authority_is_internal_scoped_and_not_player_admin(hosted):
    c, s, cid = hosted
    campaign = s.campaign(PC)
    internal = s.host_context(campaign, PC)
    assert s.role(campaign, internal) == "kp"
    assert s.role(campaign, PC) == "player"
    assert s.role(campaign, replace(PC, person_id=internal.person_id)) == "observer"
    for operation, args in [
        ("成员", {"person": "intruder", "role": "kp"}),
        ("改卡", {"person": PC.person_id}),
        ("主持授权", {"enabled": False}),
    ]:
        with pytest.raises(SkillError):
            s.execute(internal, operation, args)
    with pytest.raises(SkillError):
        s.host_context(campaign, OTHER)
    with pytest.raises(SkillError):
        s.host_context(campaign, replace(PC, channel="group:other"))
    with pytest.raises(SkillError):
        s.execute(
            PC,
            "主持行动",
            {"actor": PC.person_id, "plan": {"action_id": "drawer"}, "trpg_principal": cid},
        )


def test_natural_restore_needs_one_confirmation_and_never_rolls(hosted):
    c, s, cid = hosted
    s.execute(GM, "暂停")
    with s.store.connect(write=True) as conn:
        campaign = s.campaign(GM, conn=conn)
        campaign["state"]["needs_confirmation"] = True
        Store.save(conn, campaign)
    s.rng = Dice()
    result = handle(c, "继续跑团", "resume-1")
    assert "确认继续" in result["text"] and "HP 10/10" in result["text"]
    assert s.campaign(PC)["status"] == "paused"
    result = handle(c, "确认继续", "resume-2")
    assert "白璎已接手" in result["text"]
    assert s.campaign(PC)["status"] == "active"
    assert s.campaign(PC)["state"]["needs_confirmation"] is False
    assert not s.rng.calls and c.store.metrics() == []
    old = s.campaign(PC)["version"]
    assert handle(c, "确认继续", "resume-2")["replay"]
    assert s.campaign(PC)["version"] == old


def test_player_natural_pause_resume_and_check_confirmation_survive_restart(hosted):
    c, s, cid = hosted
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "hall",
                "title": "走廊",
                "public": "走廊",
                "actions": [
                    {
                        "id": "search",
                        "title": "搜索抽屉",
                        "aliases": ["搜索抽屉"],
                        "skill": "侦查",
                        "risk": "失败花费一分钟",
                        "failure": [{"type": "minutes", "delta": 1}],
                    }
                ],
            }
        },
    )
    result = handle(c, "搜索抽屉", "a")
    assert "尚未掷骰" in result["text"] and "确认投骰" in result["text"]
    handle(c, "暂停跑团", "pause")
    assert s.campaign(PC)["status"] == "paused"
    handle(c, "继续跑团", "resume")
    s.rng = Dice([1, 1])
    rolled = handle(c, "确认投骰", "roll")
    assert "骰点 11/60" in rolled["text"]
    assert handle(c, "确认投骰", "roll")["replay"]
    assert len(s.rng.calls) == 2


def test_unknown_action_uses_private_planner_and_public_model_never_receives_secret(hosted):
    c, s, cid = hosted
    c.config.trpg.narration = True
    public, public_fake = scripted_gateway(lambda *_: "抽屉被打开，里面的线索显露出来。")
    private, private_fake = scripted_gateway(lambda *_: '{"action_id":"drawer"}')
    c.narrator.factory = lambda: public
    c.host.planner.factory = lambda: private
    result = handle(c, "翻找木柜附近能看见的东西", "unusual")
    assert len(private_fake.requests) == len(public_fake.requests) == 1
    assert "ABSOLUTE-KP-SECRET" in "".join(m.content for m in private_fake.requests[0])
    assert "ABSOLUTE-KP-SECRET" not in "".join(m.content for m in public_fake.requests[0])
    assert "ABSOLUTE-KP-SECRET" not in result["text"]
    assert handle(c, "翻找木柜附近能看见的东西", "unusual")["replay"]
    assert len(private_fake.requests) == len(public_fake.requests) == 1


def test_planner_cannot_inject_arbitrary_damage_role_or_secret_reply(hosted):
    c, s, cid = hosted
    c.config.trpg.narration = True
    private, fake = scripted_gateway(
        lambda *_: json.dumps(
            {"action_id": "drawer", "damage": 999, "private": "ABSOLUTE-KP-SECRET"}
        )
    )
    c.host.planner.factory = lambda: private
    version = s.campaign(PC)["version"]
    with pytest.raises(SkillError, match="不允许的字段"):
        handle(c, "使用特殊方式探索", "bad")
    assert s.campaign(PC)["version"] == version
    assert s.pc(s.campaign(PC), PC.person_id)[1]["HP"] == 10


def test_scene_horror_prompts_once_and_cannot_cancel_sanity(hosted):
    c, s, cid = hosted
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "dark",
                "title": "暗室",
                "public": "首次可见的异象",
                "sanity": {"id": "dark-first", "success_loss": "0", "failure_loss": "1d3"},
            }
        },
    )
    with s.store.connect(write=True) as conn:
        camp = s.campaign(GM, conn=conn)
        camp["state"]["scenes"]["hall"]["actions"].append(
            {
                "id": "enter",
                "title": "进入暗室",
                "aliases": ["进入暗室"],
                "success": [{"type": "scene", "name": "dark"}],
            }
        )
        Store.save(conn, camp)
    result = handle(c, "进入暗室", "enter")
    assert "理智损失" in result["text"]
    pending, _ = c.host.pending(s.campaign(PC), PC)
    with pytest.raises(SkillError, match="不能取消"):
        s.execute(PC, "取消检定", {"id": pending["id"]})
    s.rng = Dice([1, 1])
    handle(c, "确认投骰", "san")
    assert s.campaign(PC)["state"]["flags"]["san:dark-first"]


@pytest.mark.parametrize("attacks", [1, 2])
def test_attack_and_natural_defense_use_program_npc_sheet(hosted, attacks):
    c, s, cid = hosted
    s.execute(
        GM,
        "NPC",
        {"name": "牢房内的人", "card": {"DEX": 40, "POW": 0, "skills": {"斗殴": 40, "闪避": 25}}},
    )
    with s.store.connect(write=True) as conn:
        camp = s.campaign(GM, conn=conn)
        npc = next(k for k, v in camp["state"]["characters"].items() if v["npc"])
        camp["state"]["scenes"]["hall"]["npcs"] = [npc]
        camp["state"]["module"] = {
            "manifest_sha": None,
            "weapons": {"空手": {"skill": "斗殴", "damage": "1d3"}},
            "npcs": {
                npc: {"damage": "1d4", "skill": "斗殴", "source": "模组页10", "attacks": attacks}
            },
        }
        Store.save(conn, camp)
    proposed = handle(c, "我攻击他", "attack")
    assert "尚未掷骰" in proposed["text"]
    s.rng = Dice([8, 8, 8, 8])  # PC and NPC defense both fail
    result = handle(c, "确认投骰", "attack-roll")
    assert "直接说“闪避”" in result["text"]
    assert len(s.rng.calls) == 4  # NPC攻击尚未出目
    s.rng = Dice([8, 8, 1, 1])  # NPC attack fails, player dodge succeeds
    result = handle(c, "闪避", "dodge")
    assert "防守方" in result["text"]
    assert s.pc(s.campaign(PC), PC.person_id)[1]["HP"] == 10
    if attacks == 2:
        assert s.campaign(PC)["state"]["combat"]["turn"] == npc
        s.rng = Dice([8, 8, 8, 1, 1])
        handle(c, "闪避", "dodge-2")
    assert s.campaign(PC)["state"]["combat"]["turn"] == s.pc(s.campaign(PC), PC.person_id)[0]


def test_revocation_and_material_edits_stop_private_calls(hosted):
    c, s, cid = hosted
    c.config.trpg.narration = True
    private, fake = scripted_gateway(lambda *_: '{"action_id":"drawer"}')
    c.host.planner.factory = lambda: private
    internal = s.host_context(s.campaign(PC), PC)
    s.execute(GM, "主持授权", {"enabled": False})
    with pytest.raises(SkillError):
        handle(c, "用不寻常的方法探索", "after-revoke")
    with pytest.raises(SkillError):
        s.execute(internal, "主持行动", {"actor": PC.person_id, "plan": {"action_id": "drawer"}})
    assert not fake.requests


def test_material_edit_requires_new_private_consent(hosted):
    c, s, cid = hosted
    c.config.trpg.narration = True
    private, fake = scripted_gateway(lambda *_: '{"action_id":"drawer"}')
    c.host.planner.factory = lambda: private
    scene = s.campaign(PC)["state"]["scenes"]["hall"]
    scene["private"] = "newly-added-private-material"
    s.execute(GM, "场景", {"scene": scene})
    with pytest.raises(SkillError, match="未明确授权"):
        handle(c, "用不寻常的方法探索", "edited")
    assert not fake.requests


def test_failed_action_only_offers_declared_push_not_a_fresh_roll(hosted):
    c, s, cid = hosted
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "hall",
                "title": "走廊",
                "public": "走廊",
                "actions": [
                    {
                        "id": "search",
                        "title": "搜索抽屉",
                        "aliases": ["搜索抽屉"],
                        "skill": "侦查",
                        "risk": "失败没有发现",
                        "push": {
                            "risk": "再失败多消耗三分钟",
                            "failure": [{"type": "minutes", "delta": 3}],
                        },
                    }
                ],
            }
        },
    )
    handle(c, "搜索抽屉", "try")
    s.rng = Dice([8, 8])
    handle(c, "确认投骰", "fail")
    choices = actions_for(s.campaign(PC)["state"], PC.person_id)
    assert [a["id"] for a in choices] == ["push:search"]
    proposal = handle(c, "孤注一掷", "push")
    assert "再失败多消耗三分钟" in proposal["text"]
    s.rng = Dice([8, 8])
    handle(c, "确认投骰", "push-fail")
    assert actions_for(s.campaign(PC)["state"], PC.person_id) == []
    assert s.campaign(PC)["state"]["minutes"] == 3
    with s.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 2


def test_darkness_clock_uses_confirmed_story_minutes(hosted):
    c, s, cid = hosted
    with s.store.connect(write=True) as conn:
        camp = s.campaign(GM, conn=conn)
        camp["state"]["module"] = {"darkness_clock": True}
        camp["state"]["minutes"] = 15
        Store.save(conn, camp)
    result = handle(c, "打开抽屉", "clock")
    assert "灯光忽然熄灭" in result["text"]
    assert s.campaign(PC)["state"]["flags"]["lights_off"]
    result = handle(c, "等灯光恢复", "wait")
    assert "黑暗结束" in result["text"]
    assert not s.campaign(PC)["state"]["flags"]["lights_off"]
    assert c.store.metrics() == []


def test_bout_processing_keeps_hp_san_and_records_actual_dice(hosted):
    c, s, cid = hosted
    with s.store.connect(write=True) as conn:
        camp = s.campaign(GM, conn=conn)
        _, card = s.pc(camp, PC.person_id)
        card["conditions"].append("临时疯狂，发作症状由KP裁定")
        Store.save(conn, camp)
    s.rng = Dice([3, 5])
    result = handle(c, "处理发作", "bout")
    assert "持续4轮" in result["text"] and "6小时" in result["text"]
    card = s.pc(s.campaign(PC), PC.person_id)[1]
    assert card["HP"] == 10 and card["SAN"] == 70
    assert card["bout_resolved"] and rules.can_act(card)
    with s.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 1


def qq_host(bot):  # noqa: F811
    c, s = bot.c.trpg, bot.c.trpg.service
    c.narrator.factory = lambda: scripted_gateway(lambda *_: "我来接着处理。")[0]
    c.config.trpg.narration = False
    bot.cfg.max_replies_per_minute = 30
    owner = bot.c.people.ensure("qq", "2000")
    player = bot.c.people.ensure("qq", "3000")
    gm = replace(GM, channel="group:4000", person_id=owner)
    pc = replace(PC, channel="group:4000", person_id=player)
    cid = s.execute(gm, "创建", {"name": "QQ自然入口"})["campaign"]
    s.execute(gm, "成员", {"person": player})
    s.execute(pc, "建卡", {"name": "玩家角色", "card": {}})
    s.execute(
        gm,
        "场景",
        {
            "scene": {
                "id": "start",
                "title": "走廊",
                "public": "已知现场",
                "actions": [
                    {
                        "id": "look",
                        "title": "观察桌子",
                        "aliases": ["看看桌子"],
                        "success_text": "桌子上没有新东西。",
                    }
                ],
            }
        },
    )
    path = c.access.folder(pc.channel) / "模组.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("KP私有资料", "utf-8")
    c.library.import_file(gm, cid, path, "kp")
    with s.store.connect() as conn:
        sha = conn.execute("SELECT sha FROM documents LIMIT 1").fetchone()[0]
    s.execute(
        gm,
        "主持授权",
        {
            "enabled": True,
            "allow_private_model": True,
            "sources": [sha],
            "consent": "主人明确授权本团专用模型",
        },
    )
    s.execute(gm, "开团")
    return c, s, gm, pc, cid


def test_qq_player_can_resume_without_becoming_kp_and_ooc_still_chats(bot):  # noqa: F811
    c, s, gm, pc, cid = qq_host(bot)
    player = pc.person_id
    s.execute(gm, "暂停")
    with s.store.connect(write=True) as conn:
        camp = s.campaign(gm, conn=conn)
        camp["state"]["needs_confirmation"] = True
        Store.save(conn, camp)
    for index, text in enumerate(["继续跑团", "确认继续", "看看桌子", "查看状态", "暂停跑团"], 900):
        run(bot, msg(text, user="3000", group="4000", mid=index))
    texts = "\n".join(bot.client.texts())
    assert "确认继续" in texts and "白璎已接手主持" in texts and "桌子上没有新东西" in texts
    assert not bot.chat.requests
    assert s.campaign(pc)["state"]["members"][player]["role"] == "player"
    run(bot, msg("团外：白璎早上好", user="3000", group="4000", mid=910))
    assert len(bot.chat.requests) == 1
    assert s.campaign(pc)["status"] == "paused"


def test_approved_package_installation_keeps_pc_and_needs_explicit_consent(
    hosted, tmp_path, monkeypatch
):
    import importlib.util
    from pathlib import Path

    c, s, cid = hosted
    s.execute(GM, "暂停")
    before = s.pc(s.campaign(PC), PC.person_id)[1]
    with s.store.connect() as conn:
        sha = conn.execute("SELECT sha FROM documents LIMIT 1").fetchone()[0]
    pack = {
        "schema": 1,
        "name": "测试审核包",
        "source_sha": sha,
        "scenes": [{"id": "hall", "title": "走廊", "public": "公开场景", "npcs": ["test"]}],
        "facts": [],
        "flags": {},
        "npcs": {
            "test": {"name": "对手", "card": {"POW": 0}, "damage": "1d4", "source": "已核对原文"}
        },
        "weapons": {},
        "adoptions": [],
    }
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(pack), "utf-8")
    spec = importlib.util.spec_from_file_location(
        "host_installer", Path(__file__).parents[1] / "scripts/ops/enable-ai-host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_config", lambda _: c.config)
    version = s.campaign(PC)["version"]
    assert module.enable(c.config.root, cid, path)["apply"] is False
    with pytest.raises(ValueError, match="明确授权"):
        module.enable(c.config.root, cid, path, apply=True)
    assert s.campaign(PC)["version"] == version
    result = module.enable(
        c.config.root,
        cid,
        path,
        apply=True,
        consent="主人明确同意专用模型读取指定模组",
        authorized=True,
    )
    assert (
        result["ai_host_enabled"] and module.enable(c.config.root, cid, path)["already_installed"]
    )
    assert s.pc(s.campaign(PC), PC.person_id)[1] == before
    assert s.campaign(PC)["status"] == "paused"
    assert s.campaign(PC)["state"]["members"][PC.person_id]["role"] == "player"


def test_private_gateway_has_no_paid_or_local_fallback(hosted, monkeypatch):
    from pydantic import SecretStr

    from companion.trpg import narrator

    c, s, cid = hosted
    c.config.trpg.allow_paid_fallback = True
    c.config.models.fallback.api_key = SecretStr("fake-only")
    c.config.models.local.enabled = True
    observed = []
    monkeypatch.setattr(
        narrator, "build_gateway", lambda cfg, **kw: observed.append((cfg, kw)) or object()
    )
    c.host.planner._gateway()
    cfg, kwargs = observed[0]
    assert cfg.models.fallback.api_key is None and not cfg.models.local.enabled
    assert kwargs["session_store"].name == "trpg-kp-sessions.sqlite"
    assert c.config.models.fallback.api_key is not None


def test_regrant_uses_new_private_thread_scope(hosted):
    from companion.models.openai_compat import ChatResult

    class Gateway:
        def __init__(self):
            self.keys = []

        def chat(self, messages, **kwargs):
            self.keys.append(kwargs["session_key"])
            return ChatResult(text='{"action_id":"drawer"}', backend="subscription", model="fake")

    c, s, cid = hosted
    c.config.trpg.narration = True
    gateway = Gateway()
    c.host.planner.factory = lambda: gateway
    for _ in range(2):
        campaign = s.campaign(PC)
        c.host._plan(PC, campaign, "查找柜子周围", actions_for(campaign["state"], PC.person_id))
        s.execute(
            GM,
            "主持授权",
            {
                "enabled": True,
                "allow_private_model": True,
                "sources": campaign["state"]["ai_kp"]["sources"],
                "consent": "主人重新确认来源范围",
            },
        )
    assert len(gateway.keys) == 2 and gateway.keys[0] != gateway.keys[1]
    assert all("/kp/" in key for key in gateway.keys)


def test_luck_successful_transition_still_requires_new_scene_sanity(hosted):
    c, s, cid = hosted
    s.execute(GM, "房规", {"name": "luck_spending", "enabled": True})
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "next",
                "title": "下一场景",
                "public": "新的景象",
                "sanity": {"id": "new-horror", "success_loss": "1", "failure_loss": "1d3"},
            }
        },
    )
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "hall",
                "title": "走廊",
                "public": "走廊",
                "actions": [
                    {
                        "id": "go",
                        "title": "尝试前进",
                        "aliases": ["尝试前进"],
                        "skill": "侦查",
                        "risk": "失败未能前进",
                        "success": [{"type": "scene", "name": "next"}],
                    }
                ],
            }
        },
    )
    handle(c, "尝试前进", "luck-go")
    s.rng = Dice([6, 6])
    handle(c, "确认投骰", "luck-roll")
    result = handle(c, "花6点幸运", "luck-spend")
    assert "理智损失" in result["text"]
    pending, spec = c.host.pending(s.campaign(PC), PC)
    assert spec["kind"] == "sanity" and spec["mandatory"] and pending["status"] == "ready"
    assert s.pc(s.campaign(PC), PC.person_id)[1]["LUCK"] == 44


def test_resolved_bout_does_not_freeze_following_npc_turn(hosted):
    c, s, cid = hosted
    s.execute(GM, "NPC", {"name": "对手", "card": {"DEX": 40}})
    with s.store.connect(write=True) as conn:
        camp = s.campaign(GM, conn=conn)
        npc = next(k for k, v in camp["state"]["characters"].items() if v["npc"])
        _, card = s.pc(camp, PC.person_id)
        card["conditions"] = ["临时疯狂（本次发作结束）"]
        card["bout_resolved"] = True
        camp["state"]["scenes"]["hall"]["npcs"] = [npc]
        camp["state"]["module"] = {
            "weapons": {"空手": {"skill": "斗殴", "damage": "1d3"}},
            "npcs": {npc: {"damage": "1d4", "skill": "斗殴"}},
        }
        Store.save(conn, camp)
    handle(c, "攻击他", "after-bout")
    s.rng = Dice([8, 8, 8, 8])
    result = handle(c, "确认投骰", "after-bout-roll")
    assert "直接说“闪避”" in result["text"]
    assert s.campaign(PC)["status"] == "active"


def test_tool_json_status_formats_keep_requester_permissions(hosted):
    c, s, cid = hosted
    for field, value in [("操作", "状态"), ("action", "status"), ("operation", "状态")]:
        result = c.command(PC, json.dumps({field: value}))
        assert result["campaign"] == cid
        assert "ABSOLUTE-KP-SECRET" not in result["text"]
    with pytest.raises(SkillError):
        c.command(PC, json.dumps({"操作": "成员", "person": "intruder", "role": "kp"}))
    with pytest.raises(SkillError):
        c.parse('{"操作":"状态","operation":"status"}')
    assert c.parse('{"operation":"行动","action":"drawer"}') == ("行动", {"action": "drawer"})


def test_failed_private_plan_is_logged_and_new_message_can_retry_after_cooldown(
    hosted, monkeypatch
):
    from companion.models.openai_compat import ModelError
    from companion.trpg import narrator

    c, s, cid = hosted
    c.config.trpg.narration = True
    now = [100.0]
    monkeypatch.setattr(narrator.time, "monotonic", lambda: now[0])
    attempts = []

    def reply(*_):
        attempts.append(True)
        if len(attempts) == 1:
            raise ModelError("test timeout", retryable=True)
        return '{"action_id":"drawer"}'

    c.host.planner.factory = lambda: scripted_gateway(reply)[0]
    c.narrator.factory = lambda: scripted_gateway(lambda *_: "抽屉里的线索显露出来。")[0]
    with pytest.raises(SkillError, match="30秒"):
        handle(c, "换个方式找东西", "failed-a")
    with c.store.connect() as conn:
        row = conn.execute("SELECT * FROM host_requests WHERE key=?", ("failed-a",)).fetchone()
        assert row["request"] == "换个方式找东西" and row["response"] and row["plan"] is None
    assert handle(c, "换个方式找东西", "failed-a")["replay"]
    with pytest.raises(SkillError, match="30秒"):
        handle(c, "换个方式找东西", "failed-b")
    assert len(attempts) == 1
    now[0] += 31
    assert handle(c, "换个方式找东西", "new-attempt")["changed"]
    assert len(attempts) == 2
    assert not c.host.planner._retry_after > now[0]


def test_status_and_pause_bypass_running_plan_and_invalidate_its_action(hosted):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    c, s, cid = hosted
    c.config.trpg.narration = True
    entered, release = threading.Event(), threading.Event()

    def reply(*_):
        entered.set()
        assert release.wait(5)
        return '{"action_id":"drawer"}'

    c.host.planner.factory = lambda: scripted_gateway(reply)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(handle, c, "换个方式找东西", "slow")
        try:
            assert entered.wait(3)
            with c.store.connect() as conn:
                assert conn.execute(
                    "SELECT request FROM host_requests WHERE key=?", ("slow",)
                ).fetchone()
            status = pool.submit(handle, c, "查看状态", "query").result(timeout=2)
            assert "正在跑团" in status["text"]
            paused = pool.submit(handle, c, "暂停跑团", "pause").result(timeout=2)
            assert "跑团已暂停" in paused["text"]
        finally:
            release.set()
        with pytest.raises(SkillError):
            pending.result(timeout=3)
    campaign = s.campaign(PC)
    assert campaign["status"] == "paused" and not campaign["state"]["known"]
    with c.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 0


def test_qq_slow_host_does_not_queue_chatter_or_block_pause_and_private_chat(bot):  # noqa: F811
    import asyncio
    import threading

    c, s, gm, pc, cid = qq_host(bot)
    c.config.trpg.narration = True
    entered, release = threading.Event(), threading.Event()

    def reply(*_):
        entered.set()
        assert release.wait(8)
        return '{"action_id":"look"}'

    c.host.planner.factory = lambda: scripted_gateway(reply)[0]

    async def exercise():
        slow = asyncio.create_task(bot.handle_event(msg("换个方式观察", group="4000", mid=1200)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for i in range(3):
                await asyncio.wait_for(
                    bot.handle_event(msg("感觉卡住了", group="4000", at_bot=True, mid=1201 + i)), 2
                )
            await asyncio.wait_for(bot.handle_event(msg("查看状态", group="4000", mid=1210)), 2)
            await asyncio.wait_for(
                bot.handle_event(msg("暂停跑团", user="2000", group="4000", mid=1211)), 2
            )
            await asyncio.wait_for(bot.handle_event(msg("你好", user="2000", mid=1212)), 2)
            await asyncio.wait_for(bot.drain(), 3)
            assert len(bot.chat.requests) == 1
            assert s.campaign(pc)["status"] == "paused"
            assert sum("没有排队执行" in text for text in bot.client.texts()) == 1
            assert any("正在跑团" in text for text in bot.client.texts())
        finally:
            release.set()
            await asyncio.wait_for(slow, 3)

    asyncio.run(exercise())
    assert not bot._trpg_busy
    assert len(c.store.metrics(cid)) == 1


def test_private_planner_ignores_legacy_cached_failure_notice(hosted):
    import hashlib

    from companion.models.openai_compat import ChatMessage

    c, s, cid = hosted
    c.config.trpg.narration = True
    planner = c.host.planner
    gateway, fake = scripted_gateway(lambda *_: '{"action_id":""}')
    planner.factory = lambda: gateway
    request = "legacy-failure-retry"
    key = hashlib.sha256(("trpg_kp" + request).encode()).hexdigest()
    with c.store.connect(write=True) as conn:
        conn.execute("INSERT INTO narratives VALUES(?,?,?)", (key, cid, "（AI暂不可用）"))
    messages = [ChatMessage("user", "虚构测试行动")]
    assert planner.generate(cid, messages, request, "trpg_kp") == '{"action_id":""}'
    assert planner.generate(cid, messages, request, "trpg_kp") == '{"action_id":""}'
    assert len(fake.requests) == 1


def test_quick_pause_is_not_discarded_when_normal_reply_rate_is_exhausted(bot):  # noqa: F811
    c, s, gm, pc, cid = qq_host(bot)
    bot.cfg.max_replies_per_minute = 0
    run(bot, msg("暂停跑团", user="2000", group="4000", mid=1300))
    assert s.campaign(pc)["status"] == "paused"
    assert any("跑团已暂停" in text for text in bot.client.texts())
    assert not bot.chat.requests
