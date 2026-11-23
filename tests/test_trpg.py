import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from companion.config import load_config
from companion.skills.base import SkillContext, SkillError
from companion.trpg import rules
from companion.trpg.controller import Controller
from companion.trpg.policy import protect, registry
from tests.fakes import scripted_gateway
from tests.test_qq import bot, msg, run  # noqa: F401

GM = SkillContext("qq", "group:100", "gm", False, True)
PC = replace(GM, person_id="player", owner=False)
OTHER = replace(PC, person_id="observer")


class Dice:
    def __init__(self, values=()):
        self.values = iter(values)
        self.calls = []

    def __call__(self, n):
        self.calls.append(n)
        value = next(self.values)
        assert 0 <= value < n
        return value


@pytest.fixture
def game(tmp_path):
    cfg = load_config(tmp_path, environ={})
    cfg.trpg.narration = False
    c = Controller(cfg)
    s = c.service
    cid = s.execute(GM, "创建", {"name": "测试团"})["campaign"]
    s.execute(GM, "成员", {"person": PC.person_id})
    s.execute(
        PC,
        "建卡",
        {
            "name": "调查员",
            "card": {
                "CON": 55,
                "HP_MAX": 10,
                "SAN": 70,
                "skills": {"侦查": 60, "斗殴": 45, "急救": 50},
            },
        },
    )
    s.execute(GM, "事实", {"id": "clue", "text": "抽屉里的公开线索", "source": "测试书第2页"})
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "hall",
                "title": "走廊",
                "public": "潮湿的走廊旁有抽屉。",
                "private": "ABSOLUTE-KP-SECRET",
                "actions": [
                    {
                        "id": "drawer",
                        "title": "查看抽屉",
                        "aliases": ["查看抽屉", "打开抽屉"],
                        "success": [{"type": "reveal", "name": "clue"}],
                        "success_text": "你在抽屉里发现线索。",
                    }
                ],
            }
        },
    )
    s.execute(GM, "开团")
    return c, s, cid


def request(s, **kwargs):
    s.execute(GM, "检定", {"actor": PC.person_id, "skill": "侦查", "reason": "搜寻", **kwargs})
    with s.store.connect() as conn:
        return conn.execute("SELECT id FROM checks ORDER BY rowid DESC LIMIT 1").fetchone()[0]


def test_role_channel_and_character_ownership_are_checked(game):
    c, s, cid = game
    for actor in (OTHER, replace(PC, owner=True)):
        with pytest.raises(SkillError):
            s.execute(actor, "成员", {"person": "intruder", "role": "kp"})
    with pytest.raises(SkillError):
        s.execute(replace(PC, channel="group:other"), "状态", {"campaign": cid})
    private = s.execute(
        replace(PC, private=True, channel="private:player"), "状态", {"campaign": cid}
    )
    assert "ABSOLUTE-KP-SECRET" not in private["text"]
    with pytest.raises(SkillError):
        s.execute(PC, "角色", {"person": "gm"})


def test_simple_action_reveals_only_approved_facts_without_random_or_model(game):
    c, s, cid = game
    s.rng = Dice()
    result = c.act(replace(PC, request_id="m1"), "我要打开抽屉")
    assert "发现线索" in result["text"]
    assert "ABSOLUTE-KP-SECRET" not in s.execute(PC, "状态")["text"]
    assert len(s.execute(PC, "状态")["data"]["known"]) == 1
    old = s.campaign(PC)["version"]
    assert c.act(replace(PC, request_id="m1"), "我要打开抽屉")["replay"]
    assert s.campaign(PC)["version"] == old
    with pytest.raises(SkillError):
        c.act(OTHER, "打开抽屉")


def test_formal_check_persists_once_and_does_not_repeat_effects(game):
    c, s, cid = game
    key = request(s, success=[{"type": "item", "name": "金属片", "delta": 1}])
    s.rng = Dice([2, 2])
    first = s.execute(PC, "投骰", {"id": key}, request_id="m1")
    before = s.pc(s.campaign(PC), PC.person_id)[1]["items"]
    assert before["金属片"] == 1
    assert s.execute(PC, "投骰", {"id": key}, request_id="m1")["replay"]
    s.execute(PC, "投骰", {"id": key}, request_id="m2")
    assert s.pc(s.campaign(PC), PC.person_id)[1]["items"] == before
    with s.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 1
    assert "22" in first["text"]


def test_target_and_risk_are_locked_before_roll_and_stale_cards_are_rejected(game):
    c, s, cid = game
    result = s.execute(
        GM,
        "检定",
        {"actor": PC.person_id, "skill": "锁匠", "reason": "拨锁", "risk": "工具可能损坏"},
    )
    assert "完整值1" in result["text"] and "成功率很低" in result["text"]
    with s.store.connect() as conn:
        key = conn.execute("SELECT id FROM checks").fetchone()[0]
    s.execute(GM, "暂停")
    s.execute(
        GM,
        "改卡",
        {
            "person": PC.person_id,
            "name": "调查员",
            "reason": "纠错",
            "card": {"skills": {"锁匠": 99}},
        },
    )
    s.execute(GM, "续团")
    s.rng = Dice()
    with pytest.raises(SkillError):
        s.execute(PC, "投骰", {"id": key})


def test_failed_effect_rolls_back_card_and_roll_ledger_as_one_transaction(game):
    c, s, cid = game
    key = request(
        s,
        success=[
            {"type": "item", "name": "物品", "delta": 1},
            {"type": "item", "name": "不存在", "delta": -1},
        ],
    )
    s.rng = Dice([1, 1])
    before = s.pc(s.campaign(PC), PC.person_id)[1]
    with pytest.raises(SkillError):
        s.execute(PC, "投骰", {"id": key})
    assert s.pc(s.campaign(PC), PC.person_id)[1] == before
    with s.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 0


def test_sanity_loss_and_day_do_not_follow_real_time(game):
    c, s, cid = game
    key = request(s, kind="sanity", skill="SAN", success_loss="0", failure_loss="1d6")
    s.rng = Dice([0, 0])  # D100=100，大失败，损失最大6；INT后续还需要两个数。
    s.rng = Dice([0, 0, 9, 9])
    s.execute(PC, "投骰", {"id": key})
    card = s.pc(s.campaign(PC), PC.person_id)[1]
    assert card["SAN"] == 64 and card["san_loss_today"] == 6
    s.execute(GM, "暂停")
    reopened = Controller(c.config)
    assert reopened.service.pc(reopened.service.campaign(PC), PC.person_id)[1]["SAN"] == 64
    s.execute(GM, "下一日")
    card = s.pc(s.campaign(PC), PC.person_id)[1]
    assert card["san_day_start"] == 64 and card["san_loss_today"] == 0


@pytest.mark.parametrize(
    "mode,a,b,expected",
    [
        ("闪避", 40, 40, "防守方"),
        ("反击", 40, 40, "进攻方"),
        ("反击", 90, 96, "无人成功"),
        ("闪避", 10, 99, "进攻方"),
    ],
)
def test_opposed_ties_and_both_failed(mode, a, b, expected):
    aa = {"skill": 60, **rules.grade(a, 60, "常规")}
    bb = {"skill": 60, **rules.grade(b, 60, "常规")}
    assert rules.oppose(aa, bb, mode) == expected


def test_extreme_blunt_impale_and_counterattack_damage():
    assert rules.damage("1d6", "1d4", True, False, False, Dice())["total"] == 10
    assert rules.damage("1d6", "0", True, True, False, Dice([2]))["total"] == 9
    assert rules.damage("1d6", "0", True, False, True, Dice([1]))["total"] == 2
    assert rules.damage("1d3", "-1", False, False, False, Dice([0]))["total"] == 0


def test_major_wound_and_zero_hp_do_not_automatically_equal_death():
    card = rules.character({"HP_MAX": 10, "CON": 55})
    result = rules.wound(card, 6, Dice([6, 6]))
    assert result["damage"] == 6
    assert card["HP"] == 4 and "重伤" in card["conditions"] and "昏迷" in card["conditions"]
    rules.wound(card, 4, Dice())
    assert card["HP"] == 0 and "死亡" not in card["conditions"]
    assert any("濒死" in v for v in card["conditions"])


def test_private_source_is_blocked_as_file_copy_zip_history_and_cached_text(game, tmp_path):
    c, s, cid = game
    folder = c.access.folder(GM.channel)
    folder.mkdir(parents=True)
    source = folder / "模组.md"
    source.write_text("ABSOLUTE-KP-SECRET", "utf-8")
    result = c.library.import_file(GM, cid, str(source))
    assert result["visibility"] == "kp"
    with pytest.raises(SkillError):
        c.access.check_read(source, GM)
    copy = folder / "改名.md"
    copy.write_bytes(source.read_bytes())
    with pytest.raises(SkillError):
        c.access.check_send(copy, GM)
    archive = folder / "复制.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("无关名字.md", source.read_bytes())
    with pytest.raises(SkillError):
        c.access.check_read(archive, GM)
    assert "未找到" in c.library.search(PC, cid, "ABSOLUTE")["text"]
    private = c.library.search(
        replace(GM, private=True, channel="private:gm"), cid, "ABSOLUTE", private=True
    )
    assert "ABSOLUTE" in private["text"]
    with pytest.raises(SkillError):
        c.library.search(GM, cid, "ABSOLUTE", private=True)


def test_delivery_unknown_survives_restart_without_blind_resend(game):
    c, s, cid = game
    result = c.command(GM, "暂停")
    assert c.prepare_delivery(result["outbox"])[0]
    c.recover_delivery()
    assert not c.prepare_delivery(result["outbox"])[0]


def test_rollback_preserves_rolls_and_known_information(game):
    c, s, cid = game
    old = s.campaign(PC)["version"]
    c.act(PC, "打开抽屉")
    key = request(s)
    s.rng = Dice([1, 1])
    s.execute(PC, "投骰", {"id": key})
    s.execute(GM, "暂停")
    s.execute(GM, "回滚", {"version": old, "reason": "测试恢复"})
    assert s.campaign(PC)["state"]["known"] == ["clue"]
    with s.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 1


def test_narrative_budget_cache_and_visibility_keep_one_bounded_call(game):
    c, s, cid = game
    c.config.trpg.narration = True
    gateway, fake = scripted_gateway(lambda *_: "潮湿的气息扑面而来，你看到抽屉里的线索。")
    c.narrator.factory = lambda: gateway
    c.config.trpg.daily_model_calls = 1
    a = c.narrator.narrate(PC, cid, "看抽屉", "完成")
    assert c.narrator.narrate(PC, cid, "看抽屉", "完成") == a
    assert len(fake.requests) == 1
    content = "".join(m.content for m in fake.requests[0])
    assert "ABSOLUTE-KP-SECRET" not in content and len(content) <= c.config.trpg.max_input_chars
    assert "预算" in c.narrator.narrate(PC, cid, "不同的问题", "完成")
    for _ in range(20):
        s.execute(PC, "状态")
    assert len(fake.requests) == 1
    assert s.store.metrics(cid)[0]["calls"] == 1


def test_concurrent_duplicate_command_is_applied_once(game):
    c, s, cid = game

    def work(_):
        return s.execute(GM, "推进时间", {"minutes": 3}, request_id="same-message")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, range(8)))
    assert s.campaign(GM)["state"]["minutes"] == 3
    assert sum(not r.get("replay") for r in results) == 1


def test_regular_qq_still_works_and_trpg_status_needs_no_model(bot):  # noqa: F811
    run(bot, msg("/团 创建 团名", user="2000", group="4000", mid=900))
    run(bot, msg("/团 状态", user="2000", group="4000", mid=901))
    assert not bot.chat.requests
    assert any("团名" in t for t in bot.client.texts())


def test_old_sensitive_events_and_derived_memory_are_masked_without_deletion(bot):  # noqa: F811
    event = bot.c.observe(body="qq", channel="group:4000", account="3000", text="KP秘密")
    protect(registry(bot.c.config.data_dir), events=[event.id])
    assert all(e.id != event.id for e in bot.c.log.iter_events())
    assert any(e.id == event.id for e in bot.c.log.iter_events(include_private=True))


def test_luck_defers_failure_and_resumes_after_pause_without_reroll(game):
    c, s, cid = game
    s.execute(GM, "房规", {"name": "luck_spending", "enabled": True})
    key = request(
        s,
        failure=[{"type": "minutes", "delta": 5}],
        success=[{"type": "item", "name": "钥匙", "delta": 1}],
    )
    s.rng = Dice([6, 6])  # 66/60
    s.execute(PC, "投骰", {"id": key})
    assert s.campaign(PC)["state"]["minutes"] == 0
    with pytest.raises(SkillError):
        c.act(PC, "打开抽屉")
    s.execute(GM, "暂停")
    s.execute(GM, "续团")
    s.execute(PC, "花幸运", {"id": key, "amount": 6})
    card = s.pc(s.campaign(PC), PC.person_id)[1]
    assert card["LUCK"] == 44 and card["items"]["钥匙"] == 1
    result = s.execute(PC, "投骰", {"id": key})
    assert result["changed"] is False
    assert '"passed": true' in result["text"] and '"original_passed": false' in result["text"]
    assert len(s.rng.calls) == 2
    with s.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 1


def test_push_may_only_be_requested_once_from_one_failed_check(game):
    c, s, cid = game
    s.execute(PC, "授权", {"auto_roll": True})
    key = request(s)
    s.rng = Dice([8, 8])
    s.execute(PC, "投骰", {"id": key})
    s.execute(GM, "孤注", {"id": key, "risk": "工具可能折断"})
    with pytest.raises(SkillError, match="已经发起过"):
        s.execute(GM, "孤注", {"id": key, "risk": "工具可能折断"})


def test_local_module_requires_private_kp_review_and_resolves_references(game):
    c, s, cid = game
    draft = {
        "scenes": [
            {
                "id": "new",
                "title": "门厅",
                "public": "窗外下着雨。",
                "actions": [
                    {
                        "id": "look",
                        "title": "查看桌面",
                        "aliases": ["查看桌面"],
                        "success": [{"type": "reveal", "name": "note"}],
                    }
                ],
            }
        ],
        "facts": [{"id": "note", "text": "桌上的纸条", "source": "书SHA@页3"}],
    }
    s.execute(GM, "暂停")
    private = replace(GM, channel="private:gm", private=True)
    with pytest.raises(SkillError):
        s.execute(GM, "模组草案", {"draft": draft})
    s.execute(private, "模组草案", {"campaign": cid, "draft": draft})
    assert "new" not in s.campaign(GM)["state"]["scenes"]
    assert "module_draft" not in s.execute(PC, "状态")["text"]
    s.execute(private, "采纳模组", {"campaign": cid})
    assert "new" in s.campaign(GM)["state"]["scenes"]
    assert "note" not in s.execute(PC, "状态")["text"]


def test_failed_narration_and_identical_intent_are_not_retried_for_quota(game):
    from companion.models.openai_compat import ModelError

    c, s, cid = game
    c.config.trpg.narration = True

    def unavailable(*_):
        raise ModelError("offline")

    gateway, fake = scripted_gateway(unavailable)
    c.narrator.factory = lambda: gateway
    assert "暂不可用" in c.narrator.narrate(PC, cid, "观察")
    c.narrator.narrate(PC, cid, "观察")
    assert len(fake.requests) == 1
    assert c.store.metrics(cid)[0]["calls"] == 1


def test_document_page_number_and_empty_text_do_not_create_false_sources(game, tmp_path):
    c, s, cid = game
    source = c.access.folder(PC.channel) / "规则.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("## PDF 第 12 页（文字层）\n侦查规则\n## PDF 第 30 页\n战斗规则", "utf-8")
    c.library.import_file(GM, cid, source, "public")
    found = c.library.search(PC, cid, "战斗")["fragments"]
    assert found[0]["pdf_page_or_chunk"] == 30 and found[0]["printed_page"] is None


def test_npc_attack_awaits_player_defense_and_hides_enemy_values(game):
    c, s, cid = game
    player_id, _ = s.pc(s.campaign(PC), PC.person_id)
    s.execute(GM, "NPC", {"name": "对手", "card": {"DEX": 70, "skills": {"斗殴": 65}}})
    npc = next(k for k, v in s.campaign(GM)["state"]["characters"].items() if v["npc"])
    s.execute(GM, "战斗", {"characters": [player_id, npc]})
    key = request(s, actor=npc, kind="combat", skill="斗殴", target=player_id, damage="1d3")
    s.rng = Dice([1, 1, 8, 8, 0])
    with pytest.raises(SkillError, match="选择应对"):
        s.execute(GM, "投骰", {"id": key})
    assert not s.rng.calls
    s.execute(PC, "应对", {"id": key, "mode": "闪避"})
    result = s.execute(GM, "投骰", {"id": key})
    assert '"skill"' not in result["text"]
    assert s.pc(s.campaign(PC), PC.person_id)[1]["HP"] == 7  # extreme blunt: max3


def test_two_players_keep_separate_cards_and_share_only_revealed_clues(game):
    c, s, cid = game
    second = replace(PC, person_id="second")
    s.execute(GM, "暂停")
    s.execute(GM, "成员", {"person": "second"})
    s.execute(second, "建卡", {"name": "另一人", "card": {"SAN": 33}})
    s.execute(GM, "续团")
    key = request(s, success=[{"type": "item", "name": "个人物品", "delta": 1}])
    with pytest.raises(SkillError, match="只能操作自己的"):
        s.execute(second, "投骰", {"id": key})
    s.rng = Dice([1, 1])
    s.execute(PC, "投骰", {"id": key})
    c.act(PC, "打开抽屉")
    own = s.execute(second, "角色")["data"]
    assert own["SAN"] == 33 and "个人物品" not in own["items"]
    view = s.execute(second, "状态")["data"]
    assert view["known"][0]["id"] == "clue"
    first_id, _ = s.pc(s.campaign(PC), PC.person_id)
    assert set(view["characters"][first_id]) == {"name", "owner"}


def test_recovery_migration_is_local_paused_and_repeatable_without_overwrite(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts/ops/migrate-trpg-20261111.py"
    spec = importlib.util.spec_from_file_location("trpg_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_config", lambda root: load_config(root, environ={}))
    folder = tmp_path / "data/workspace/chats/group-826445390"
    files = {
        "跑团存档/埃莉诺_1111_1554.md": "暂停存档 HP5/10 SAN64",
        "KP准备/08_玩家无剧透简报.md": "公开简报",
        "KP准备/09_核验更正与现行入口.md": "PRIVATE SECRET",
        "跑团规则/COC7_完整正文.md": "## PDF 第 12 页\n规则摘要",
        "调查员/喵了个咪_埃莉诺里德_人物卡.md": "|侦查|60|30|12|",
    }
    for name, content in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf-8")
    (folder / "疗养院的噩梦.pdf").write_bytes(b"fake-pdf-only-hashed-not-read")
    run_dir = tmp_path / "data/run"
    run_dir.mkdir(parents=True)
    (run_dir / "tasks.json").write_text('{"tasks":{}}', "utf-8")
    assert module.migrate(tmp_path)["apply"] is False
    assert not (tmp_path / "data/trpg/state.sqlite").exists()
    result = module.migrate(tmp_path, True)
    assert result["status"] == "paused"
    assert module.migrate(tmp_path, True)["already_migrated"]
    c = Controller(load_config(tmp_path, environ={}))
    gm = replace(GM, channel=module.CHANNEL, person_id=module.GM)
    state = c.service.campaign(gm)["state"]
    assert state["needs_confirmation"]
    assert c.service.pc(c.service.campaign(gm), module.PLAYER)[1]["SAN"] == 64
    with pytest.raises(SkillError, match="恢复草案"):
        c.service.execute(gm, "续团")
    for name, content in files.items():
        assert (folder / name).read_text("utf-8") == content
    assert c.store.metrics() == []
