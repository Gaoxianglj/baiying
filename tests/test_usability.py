import asyncio
import zipfile
from dataclasses import replace
from io import BytesIO

import pytest

from companion.bodies.qq.bot import Source
from companion.bodies.qq.message import FileRef, Incoming
from companion.bodies.qq.onebot import OneBotError
from companion.skills.base import SkillContext, SkillError
from companion.skills.reader import FindFile, extract_text
from companion.trpg.setup import parse_card
from tests.test_qq import bot, msg, run  # noqa: F401
from tests.test_reader import FRIEND_IN_GROUP, FRIEND_PRIVATE, setup  # noqa: F401
from tests.test_trpg import GM, PC, game  # noqa: F401


def xlsx_bytes():
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="人物卡" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>机械维修</t></si></sst>",
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>75</v></c></row>'
            '<row r="2"><c r="B2"><f>SUM(1,2)</f></c></row></sheetData></worksheet>',
        )
    return out.getvalue()


def card_text():
    return (
        "# 测试机械师｜人物卡\n|属性|常规|困难|极难|\n|---|---|---|---|\n"
        + "\n".join(
            f"|{key}|50|25|10|" for key in ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU")
        )
        + (
            "\n|幸运|60|30|12|\nHP 10/10；MP 10/10；SAN 50\n"
            "|技能|基础|职业投入|兴趣投入|最终|困难|极难|\n|---|---|---|---|---|---|---|\n"
            "|机械维修|10|65|0|75|37|15|\n候选装备：扳手，尚未配发。"
        )
    )


def test_group_filename_lookup_searches_subdirectories_without_crossing_chats(setup):  # noqa: F811
    reader, inbox, root, cfg = setup
    current = reader.access.folder(FRIEND_IN_GROUP.channel)
    (current / "人物").mkdir(parents=True)
    (current / "人物" / "卡.md").write_text("这张卡在子目录", encoding="utf-8")
    inbox.save("私聊文件.md", b"private", None, FRIEND_PRIVATE.channel)
    assert "子目录" in reader.run("读文件", "卡.md", FRIEND_IN_GROUP).text
    finder = FindFile(cfg, reader.access)
    assert "人物/卡.md" in finder.run("找文件", "卡", FRIEND_IN_GROUP).text
    assert "私聊文件" not in finder.run("找文件", "私聊", FRIEND_IN_GROUP).text


def test_duplicate_basename_returns_candidates_and_protected_files_stay_hidden(setup):  # noqa: F811
    reader, inbox, root, cfg = setup
    current = reader.access.folder(FRIEND_IN_GROUP.channel)
    for name in ("甲", "乙"):
        (current / name).mkdir(parents=True)
        (current / name / "卡.md").write_text(name, encoding="utf-8")
    with pytest.raises(SkillError, match="多个同名"):
        reader.run("读文件", "卡.md", FRIEND_IN_GROUP)
    (current / "api_secret.md").write_text("do not read", encoding="utf-8")
    assert all("secret" not in p.name for p in reader.access.inventory(FRIEND_IN_GROUP))


def test_xlsx_reading_preserves_sheet_cells_and_uncalculated_formulas():
    text = extract_text("人物卡.xlsx", xlsx_bytes(), 100000)
    assert "人物卡" in text and "A1: 机械维修" in text and "B1: 75" in text
    assert "SUM(1,2)" in text and "未计算" in text


def test_missing_group_file_is_fetched_before_asking_user_to_upload_again(setup):  # noqa: F811
    reader, inbox, root, cfg = setup
    queries = []

    def fetch(query):
        queries.append(query)
        inbox.save("规则.md", "现有群文件已取回".encode(), None, FRIEND_IN_GROUP.channel)
        return [{"name": "规则.md"}]

    ctx = replace(FRIEND_IN_GROUP, fetch_files=fetch)
    assert "已取回" in reader.run("读文件", "规则.md", ctx).text
    assert queries == ["规则.md"]


def test_file_download_uses_group_fallback_when_get_file_is_unavailable(bot, tmp_path):  # noqa: F811
    from unittest.mock import patch

    from companion import media

    bot.client.replies["get_file"] = OneBotError("not supported")
    bot.client.replies["get_group_file_url"] = {"url": "https://example.invalid/file"}
    src = Source("group", "3000", "4000", "测试者", None, Incoming(text=""))
    with patch.object(media, "fetch", return_value=b"file-data"):
        assert (
            asyncio.run(bot._download_file(FileRef("规则.md", "fid", 9, None, 102), src))
            == b"file-data"
        )
    assert (
        "get_group_file_url",
        {"group_id": 4000, "file_id": "fid", "busid": 102},
    ) in bot.client.sent


def test_passive_upload_is_saved_without_starting_a_conversation(bot, tmp_path):  # noqa: F811
    source = tmp_path / "upload.md"
    source.write_text("普通测试文件", encoding="utf-8")
    bot.client.replies["get_file"] = {"file": str(source)}
    asyncio.run(
        bot.handle_event(
            {
                "post_type": "notice",
                "notice_type": "group_upload",
                "group_id": 4000,
                "user_id": 3000,
                "file": {"name": "upload.md", "id": "f", "size": source.stat().st_size},
            }
        )
    )
    reader = next(s for s in bot.c.skills.all if s.name == "read_files")
    assert (reader.access.folder("group:4000") / "upload.md").exists()
    assert not bot.chat.requests
    assert not any(action.startswith("send_") for action, _ in bot.client.sent)


def test_active_group_does_not_answer_other_mentions_gossip_or_acknowledgments(bot):  # noqa: F811
    bot.cfg.unsolicited_probability = 0
    run(bot, msg("白璎在吗", group="4000", at_bot=True, mid=1))
    before = len(bot.chat.requests)
    other = msg("你怎么想", group="4000", mid=2)
    other["message"].insert(0, {"type": "at", "data": {"qq": "3001"}})
    run(
        bot,
        other,
        msg("gpt今天是不是调了设置", group="4000", mid=3),
        msg("嗯嗯", group="4000", mid=4),
        msg("哈哈", group="4000", mid=5),
    )
    assert len(bot.chat.requests) == before
    run(bot, msg("刚才的文件你能看吗", group="4000", mid=6))
    assert len(bot.chat.requests) == before + 1


def test_auto_followup_budget_requires_a_new_direct_invitation(bot):  # noqa: F811
    bot.cfg.unsolicited_probability = 0
    run(bot, msg("白璎在吗", group="4000", at_bot=True, mid=10))
    for i in range(3):
        run(bot, msg("你还在吗", group="4000", mid=11 + i))
    assert len(bot.chat.requests) == 3
    run(bot, msg("白璎帮我看看", group="4000", at_bot=True, mid=20))
    assert len(bot.chat.requests) == 4


def test_prepare_associates_rules_template_and_card_and_resolves_qq_identity(game):  # noqa: F811
    c, s, old = game
    s.execute(GM, "暂停")
    before = s.campaign(GM)
    c.person_resolver = lambda account: "player" if account == "3040885652" else "unknown"
    folder = c.access.folder(GM.channel)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "规则.md").write_text("这是公开规则，不是模组秘密。", encoding="utf-8")
    (folder / "人物卡.xlsx").write_bytes(xlsx_bytes())
    (folder / "新卡.md").write_text(card_text(), encoding="utf-8")
    args = {
        "action": "准备",
        "name": "新的示例团",
        "rules": "规则.md",
        "template": "人物卡.xlsx",
        "characters": [{"person": "3040885652", "path": "新卡.md"}],
        "scene": {"id": "entry", "title": "门厅", "public": "你站在门厅。", "actions": []},
        "start": True,
    }
    result = c.setup.run(GM, args)
    current = s.campaign(GM, result["campaign"])
    assert current["status"] == "active" and current["id"] != old
    assert current["state"]["ai_kp"]["mode"] == "public"
    assert current["state"]["members"]["player"]["character"]
    card = s.pc(current, "player")[1]
    assert card["skills"]["机械维修"] == 75 and card["items"] == {}
    assert {a["role"] for a in current["state"]["assets"]} == {"rules", "template"}
    assert s.campaign(GM, old) == before
    assert c.setup.run(GM, args)["campaign"] == current["id"]
    assert s.campaign(GM, current["id"])["version"] == current["version"]


def test_card_parser_reads_final_values_without_granting_candidate_equipment():
    name, card = parse_card(card_text())
    assert (
        name == "测试机械师"
        and sum(card[k] for k in ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU")) == 400
    )
    assert card["skills"]["机械维修"] == 75 and card["LUCK"] == 60 and not card["items"]


@pytest.mark.parametrize("layout", ["compact", "lines"])
def test_generated_markdown_card_preserves_attributes_skills_luck_and_actual_inventory(layout):
    values = {
        "STR": 50,
        "CON": 60,
        "SIZ": 55,
        "DEX": 65,
        "APP": 50,
        "INT": 70,
        "POW": 60,
        "EDU": 65,
    }
    attrs = (
        "，".join(f"{k} {v}" for k, v in values.items())
        if layout == "compact"
        else "\n".join(f"{k}: {v}" for k, v in values.items())
    )
    text = "# 林澈｜人物卡\n## 属性\n" + attrs + "\n幸运55；HP: 9；MP: 10；SAN: 58\n"
    if layout == "compact":
        text += "## 技能\n侦查60；机械维修75；聆听55。\n## 物品\n工具包×1；黄铜钥匙×1；手电筒×1。\n"
    else:
        text += "侦查: 60\n机械维修: 75\n聆听: 55\n物品：工具包1、黄铜钥匙1、手电筒1。\n"
    text += "## 候选装备\n喷火器×1\n炸药×1\n"
    name, card = parse_card(text)
    assert name == "林澈" and all(card[k] == v for k, v in values.items())
    assert card["LUCK"] == 55 and card["HP"] == 9 and card["MP"] == 10 and card["SAN"] == 58
    assert card["skills"]["机械维修"] == 75 and card["skills"]["侦查"] == 60
    assert card["items"] == {"工具包": 1, "黄铜钥匙": 1, "手电筒": 1}


def test_card_parser_rejects_missing_json_attributes_and_conflicting_values():
    with pytest.raises(SkillError, match="完整八属性"):
        parse_card('{"name":"不完整卡","card":{"STR":50}}')
    with pytest.raises(SkillError, match="冲突"):
        parse_card(card_text() + "\nSTR: 70\n")


def test_background_worker_really_has_trpg_tools(bot):  # noqa: F811
    from tests.fakes import scripted_gateway

    answers = iter(
        ['【筹备团：{"action":"准备","name":"后台筹备示例"}】', "【任务完成：新团草案已经建立】"]
    )
    bot.c.background_gateway_factory = lambda: scripted_gateway(lambda *_: next(answers))[0]
    ctx = SkillContext("qq", "group:4000", bot.c.people.ensure("qq", "2000"), False, True)
    ctx.task_id = bot.c.tasks.create("background_tasks", "任务", "建立新团草案", ctx)
    result = bot.c._background_work("建立新团草案", ctx)
    assert result.ok
    assert any(row["name"] == "后台筹备示例" for row in bot.c.trpg.store.listing())


def test_observer_cannot_use_setup_tools_to_grant_roles_or_modify_existing_campaign(game):  # noqa: F811
    c, s, cid = game
    with pytest.raises(SkillError):
        c.setup.run(PC, {"action": "关联资料", "campaign": cid, "path": "anything.md"})
    with pytest.raises(SkillError):
        s.execute(PC, "公开主持", {})


def test_file_refusal_is_repaired_before_it_is_sent_to_user(bot):  # noqa: F811
    from tests.fakes import scripted_gateway

    reader = next(s for s in bot.c.skills.all if s.name == "read_files")
    folder = reader.access.folder("group:4000")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "规则.md").write_text("示例规则：按已登记角色数值检定。", encoding="utf-8")
    answers = iter(
        [
            "我现在看不到规则正文，请重新上传。",
            "【找文件：规则】",
            "【读文件：规则.md】",
            "规则已经找到，可以按角色数值继续。",
        ]
    )
    gateway, fake = scripted_gateway(lambda *_: next(answers))
    bot.c.gateway = gateway
    run(bot, msg("白璎，读取规则文件", group="4000", at_bot=True, mid=90))
    texts = "\n".join(bot.client.texts())
    assert "重新上传" not in texts and "规则已经找到" in texts
    assert len(fake.requests) == 4


def test_group_cloud_search_is_confined_to_current_group_and_downloads_existing_file(bot, tmp_path):  # noqa: F811
    source = tmp_path / "rules.md"
    source.write_text("云端已有规则", encoding="utf-8")
    bot.client.replies["get_group_root_files"] = {
        "folders": [{"folder_id": "rules-folder"}],
        "files": [],
    }
    bot.client.replies["get_group_files_by_folder"] = {
        "folders": [],
        "files": [
            {
                "file_name": "规则.md",
                "file_id": "rule-file",
                "file_size": source.stat().st_size,
                "busid": 102,
            }
        ],
    }
    bot.client.replies["get_file"] = {"file": str(source)}
    src = Source("group", "3000", "4000", "测试者", None, Incoming(text=""))
    result = asyncio.run(bot._group_files(src, "规则"))
    assert result[0]["name"] == "规则.md" and "存在工作文件夹" in result[0]["result"]
    params = [params for action, params in bot.client.sent if action.startswith("get_group_")]
    assert all(p["group_id"] == 4000 for p in params)
    with pytest.raises(SkillError):
        asyncio.run(bot._group_files(replace(src, group_id="9999"), "规则"))


def test_setup_normalizes_self_scene_aliases_and_unique_campaign_name(game):  # noqa: F811
    c, s, old = game
    s.execute(GM, "暂停")
    folder = c.access.folder(GM.channel)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "卡.md").write_text(card_text(), encoding="utf-8")
    result = c.setup.run(
        GM,
        {
            "action": "准备",
            "name": "别名新团",
            "characters": [{"person": "self", "path": "卡.md"}],
            "scene": {"description": "你站在一个空工坊。"},
            "start": True,
        },
    )
    campaign = s.campaign(GM, result["campaign"])
    assert s.pc(campaign, GM.person_id)[1]["name"] == "测试机械师"
    assert campaign["state"]["scenes"][campaign["state"]["scene"]]["public"] == "你站在一个空工坊。"
    assert c.setup.run(GM, {"action": "检查", "campaign": "别名新团"})["campaign"] == campaign["id"]


def test_setup_batches_scenes_selects_opening_and_verifies_registration_counts(game):  # noqa: F811
    c, s, old = game
    s.execute(GM, "暂停")
    folder = c.access.folder(GM.channel)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "卡.md").write_text(card_text(), encoding="utf-8")
    report = c.setup.run(
        GM,
        {
            "action": "准备",
            "name": "批量场景团",
            "characters": [{"path": "卡.md"}],
            "scenes": [
                {"id": "stairs", "title": "楼梯间", "public": "楼梯间", "actions": []},
                {
                    "id": "duty",
                    "title": "值班室",
                    "public": "值班室",
                    "actions": [{"id": "look", "title": "观察桌面"}],
                },
            ],
            "start_scene": "duty",
        },
    )
    data = report["data"]
    assert data["scene_count"] == 2 and data["action_count"] == 1
    assert data["current_scene"] == "duty" and data["bindings"][0]["name"] == "测试机械师"
    s.execute(GM, "开团", {"campaign": report["campaign"]})
    s.execute(GM, "暂停", {"campaign": report["campaign"]})
    with pytest.raises(SkillError, match="重置位置"):
        c.setup.run(GM, {"action": "准备", "campaign": report["campaign"], "start_scene": "stairs"})
    assert s.campaign(GM, report["campaign"])["state"]["scene"] == "duty"


def test_binding_uses_verified_card_file_instead_of_incomplete_parallel_object(game):  # noqa: F811
    c, s, cid = game
    s.execute(GM, "暂停")
    folder = c.access.folder(GM.channel)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "卡.md").write_text(card_text(), encoding="utf-8")
    result = c.setup.run(
        GM,
        {
            "action": "准备",
            "name": "文件卡团",
            "characters": [
                {
                    "path": "卡.md",
                    "card": {
                        key: 50 for key in ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU")
                    },
                }
            ],
        },
    )
    card = s.pc(s.campaign(GM, result["campaign"]), GM.person_id)[1]
    assert card["name"] == "测试机械师" and card["LUCK"] == 60 and card["skills"]["机械维修"] == 75


def test_active_campaign_keeps_ordinary_lunch_chatter_out_of_host_but_accepts_actions(bot):  # noqa: F811
    from tests.test_trpg_host import qq_host

    c, s, gm, pc, cid = qq_host(bot)
    bot.cfg.unsolicited_probability = 0
    run(bot, msg("团外：白璎早上好", group="4000", mid=201))
    before = len(bot.chat.requests)
    run(bot, msg("今天的午饭还不错", group="4000", mid=202))
    assert len(bot.chat.requests) == before and not c.store.metrics(cid)
    run(bot, msg("看看桌子", group="4000", mid=203))
    assert any("桌子上没有新东西" in text for text in bot.client.texts())


def test_named_call_can_mention_another_player_as_the_target(bot):  # noqa: F811
    source = Source(
        "group",
        "3000",
        "4000",
        "测试者",
        None,
        Incoming(text="白璎，帮我绑定给这位玩家", at=["5000"]),
    )
    assert bot._addressed(source) and not bot._not_for_me(source)
    source = replace(source, incoming=Incoming(text="你怎么看", at=["5000"]))
    assert bot._not_for_me(source) and not bot._addressed(source)
    source = replace(source, incoming=Incoming(text="【拟真测试】白璎，帮我制卡"))
    assert bot._addressed(source)


@pytest.mark.parametrize("answer", ["桌子", "好"])
def test_host_question_routes_its_players_short_answer_without_opening_general_chatter(bot, answer):  # noqa: F811
    from datetime import timedelta

    from tests.fakes import scripted_gateway
    from tests.test_trpg_host import qq_host

    c, s, gm, pc, cid = qq_host(bot)
    c.config.trpg.narration = True
    s.execute(gm, "公开主持")
    replies = iter(['{"kind":"action","plan":{"action_id":"look"}}', "桌子上没有新东西。"])
    gateway, fake = scripted_gateway(lambda *_: next(replies))
    c.narrator.factory = lambda: gateway
    source = Source("group", "3000", "4000", "玩家", None, Incoming(text="看看桌子"))
    asyncio.run(
        bot._trpg_deliver(source, pc, {**c.host.notice(cid, "你想查看桌子吗？"), "spoken": True})
    )
    campaign = s.campaign(pc)
    assert bot._trpg_followup(answer, campaign, pc)
    assert not bot._trpg_followup(answer, campaign, gm)
    assert not bot._trpg_followup("今天晚饭还不错", campaign, pc)
    assert not bot._trpg_followup("哈哈", campaign, pc)
    run(bot, msg(answer, user="3000", group="4000", mid=301))
    assert len(fake.requests) == 2 and "桌子上没有新东西" in bot.client.texts()[-1]
    before = len(bot.client.texts())
    run(
        bot,
        msg("哈哈", user="3000", group="4000", mid=302),
        msg("好", user="2000", group="4000", mid=303),
    )
    assert len(bot.client.texts()) == before
    asyncio.run(
        bot._trpg_deliver(
            source, pc, {**c.host.notice(cid, "你想找桌子上的什么？"), "spoken": True}
        )
    )
    assert not bot._trpg_followup("好", s.campaign(pc), pc)
    assert bot._trpg_followup("桌子", s.campaign(pc), pc)
    asyncio.run(
        bot._trpg_deliver(source, pc, {**c.host.notice(cid, "你想查看桌子吗？"), "spoken": True})
    )
    stamp = bot._trpg_questions[(pc.channel, pc.person_id)][0]
    bot.now = lambda: stamp + timedelta(seconds=181)
    assert not bot._trpg_followup(answer, s.campaign(pc), pc)


def test_roleplay_entry_accepts_modifiers_and_registered_aliases_but_not_gossip(bot):  # noqa: F811
    from tests.test_trpg_host import qq_host

    c, s, gm, pc, cid = qq_host(bot)
    campaign = s.campaign(pc)
    for phrase in ("我重新搜索抽屉", "我再仔细看看桌子", "我仔细聆听钟摆", "看看桌子"):
        assert bot._roleplay_input(phrase, campaign, pc)
    for phrase in ("今天晚饭还不错", "哈哈", "gpt今天是不是又改设置了"):
        assert not bot._roleplay_input(phrase, campaign, pc)


def test_pending_switch_and_paused_roll_have_unambiguous_feedback_without_extra_model_calls(bot):  # noqa: F811
    from tests.fakes import scripted_gateway
    from tests.test_trpg_host import qq_host

    c, s, gm, pc, cid = qq_host(bot)
    c.config.trpg.narration = True
    gateway, fake = scripted_gateway(lambda *_: "已暂停，进度和待确认检定都保留。")
    c.narrator.factory = lambda: gateway
    s.execute(
        gm,
        "检定",
        {"actor": pc.person_id, "skill": "侦查", "reason": "搜索抽屉", "risk": "失败耗时1分钟"},
    )
    switched = c.host.handle(replace(pc, request_id="switch"), "我用钥匙开门")
    assert "尚未执行" in switched["text"] and "取消行动" in switched["text"]
    assert "吗" not in switched["text"] and not fake.requests
    paused = c.host.handle(replace(pc, request_id="pause"), "暂停跑团")
    assert "愿意尝试" not in paused["text"] and len(fake.requests) == 1
    attempt = c.host.handle(replace(pc, request_id="paused-roll"), "投吧")
    assert "暂停" in attempt["text"] and "继续跑团" in attempt["text"]
    assert "愿意尝试" not in attempt["text"] and len(fake.requests) == 1
    with c.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 0
    assert s.campaign(pc)["status"] == "paused"


def test_registered_failure_cannot_be_repackaged_as_a_free_check_at_server_commit(game):  # noqa: F811
    from companion.trpg.dialogue import check_plan
    from tests.test_trpg import Dice

    c, s, cid = game
    s.execute(
        GM,
        "场景",
        {
            "scene": {
                "id": "hall",
                "title": "走廊",
                "public": "抽屉旁有雨声",
                "actions": [
                    {"id": "search", "title": "搜索抽屉", "skill": "侦查", "risk": "失败没找到线索"}
                ],
            }
        },
    )
    s.execute(PC, "行动", {"action": "search"})
    with c.store.connect() as conn:
        check = conn.execute("SELECT id FROM checks WHERE status='ready'").fetchone()[0]
    s.rng = Dice([8, 8])
    s.execute(PC, "投骰", {"id": check})
    s.execute(PC, "接受结果", {"id": check})
    s.execute(GM, "公开主持")
    campaign = s.campaign(PC)
    plan = {"kind": "check", "skill": "侦查", "reason": "换个说法再次寻找抽屉线索", "minutes": 1}
    with pytest.raises(SkillError, match="不能改成普通检定"):
        check_plan(campaign["state"], PC.person_id, plan)
    internal = s.host_context(campaign, PC)
    with pytest.raises(SkillError, match="不能改成普通检定"):
        s.execute(
            internal,
            "主持检定",
            {"actor": PC.person_id, "plan": plan, "expected_version": campaign["version"]},
        )
    assert (
        check_plan(
            campaign["state"], PC.person_id, {**plan, "skill": "聆听", "reason": "留意雨声"}
        )["skill"]
        == "聆听"
    )
    with c.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rolls").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM checks WHERE status='ready'").fetchone()[0] == 0
