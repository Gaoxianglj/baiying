from __future__ import annotations

import copy
import json
import secrets
from dataclasses import replace

from companion.skills.base import SkillContext, SkillError
from companion.trpg import rules
from companion.trpg.store import Store, dump, now, uid

READ_ONLY = {"状态", "角色", "战报", "场景列表", "检定列表", "额度", "回执"}
HOST_OPS = {
    "主持检定",
    "主持事件",
    "主持行动",
    "主持遭遇",
    "主持发作",
    "主持续团",
    "暂停",
    "检定",
    "投骰",
    "下一位",
    "结束战斗",
}


def text(value, name="文字", maximum=800):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SkillError(f"{name}须为1～{maximum}字的文字")
    return value.strip()


class Service:
    def __init__(self, store: Store, rng=secrets.randbelow):
        self.store, self.rng = store, rng

    def campaign(self, ctx, cid=None, conn=None):
        if conn is None:
            with self.store.connect() as connection:
                return self.campaign(ctx, cid, connection)
        row = (
            conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
            if cid
            else conn.execute(
                "SELECT * FROM campaigns WHERE body=? AND channel=? "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,rowid DESC LIMIT 1",
                (ctx.body, ctx.channel),
            ).fetchone()
        )
        if row is None:
            raise SkillError("当前聊天没有团，请由主人用 /团 创建 团名 建立")
        campaign = Store.load(row)
        origin = campaign["body"] == ctx.body and campaign["channel"] == ctx.channel
        if not origin and not (
            ctx.private
            and ctx.body == campaign["body"]
            and (ctx.person_id in campaign["state"]["members"] or self.is_host(campaign, ctx))
        ):
            raise SkillError("不能跨群访问这个团")
        return campaign

    def active(self, ctx):
        if not self.store.path.exists():
            return None
        try:
            campaign = self.campaign(ctx)
            return campaign if campaign["status"] == "active" else None
        except SkillError:
            return None

    @staticmethod
    def is_host(campaign, ctx):
        grant = campaign["state"].get("ai_kp", {})
        return (
            grant.get("enabled") is True
            and ctx.trpg_principal == campaign["id"]
            and ctx.person_id == grant.get("principal")
        )

    def host_context(self, campaign, requester):
        if campaign["body"] != requester.body or campaign["channel"] != requester.channel:
            raise SkillError("主持行动请在本团原聊天进行；私聊仍可查阅公开存档")
        if self.role(campaign, requester) not in {"kp", "player"}:
            raise SkillError("旁观者不能指挥本团主持")
        grant = campaign["state"].get("ai_kp", {})
        if not grant.get("enabled"):
            raise SkillError("本团尚未授权白璎主持")
        return replace(
            requester, person_id=grant["principal"], owner=False, trpg_principal=campaign["id"]
        )

    @staticmethod
    def role(campaign, ctx):
        if Service.is_host(campaign, ctx):
            return "kp"
        return campaign["state"]["members"].get(ctx.person_id, {}).get("role", "observer")

    def kp(self, campaign, ctx):
        if self.role(campaign, ctx) != "kp":
            raise SkillError("只有本团KP能执行此操作；运维主号不自动等于其他团KP")

    @staticmethod
    def pc(campaign, pid):
        member = campaign["state"]["members"].get(pid, {})
        card_id = member.get("character")
        if not card_id or card_id not in campaign["state"]["characters"]:
            raise SkillError("玩家还没有绑定角色卡")
        return card_id, campaign["state"]["characters"][card_id]

    def view(self, campaign, ctx, *, private=False):
        state = campaign["state"]
        if private:
            self.kp(campaign, ctx)
            if not ctx.private:
                raise SkillError("KP完整状态只在私聊或本地控制台查看")
            return copy.deepcopy(campaign)
        characters = {}
        for key, card in state["characters"].items():
            if card.get("npc"):
                continue
            own = card["owner"] == ctx.person_id or (
                ctx.private and self.role(campaign, ctx) == "kp"
            )
            characters[key] = (
                {k: v for k, v in card.items() if k not in {"private"}}
                if own
                else {"name": card["name"], "owner": card["owner"]}
            )
        return {
            "id": campaign["id"],
            "name": campaign["name"],
            "status": campaign["status"],
            "version": campaign["version"],
            "scene": state["scene"],
            "description": self.scene_description(state),
            "day": state["day"],
            "minutes": state["minutes"],
            "characters": characters,
            "known": [state["facts"][k] for k in state["known"] if k in state["facts"]],
            "rulings": [r for r in state.get("rulings", []) if r.get("public")][-6:],
            "combat": {k: state.get("combat", {}).get(k) for k in ("round", "turn")},
            "ai_host": bool(state.get("ai_kp", {}).get("enabled")),
            "environment": {"darkness": bool(state["flags"].get("lights_off"))},
        }

    def execute(self, ctx: SkillContext, operation: str, args=None, request_id=None):
        args = args or {}
        if not isinstance(args, dict) or len(dump(args)) > 150_000:
            raise SkillError("团务参数过大或格式不对")
        args = copy.deepcopy(args)
        cid = args.pop("campaign", None)
        request = dump({"op": operation, "args": args, "campaign": cid})
        scope = f"{ctx.body}/{ctx.channel}/{ctx.person_id}"
        key = request_id or ctx.request_id
        with self.store.connect(write=True) as conn:
            if key:
                old = conn.execute(
                    "SELECT * FROM receipts WHERE scope=? AND key=?", (scope, key)
                ).fetchone()
                if old:
                    if old["request"] != request:
                        raise SkillError("重复消息/工具编号参数发生变化，拒绝重复结算")
                    return {**json.loads(old["response"]), "replay": True}
            if operation == "创建":
                if not ctx.owner:
                    raise SkillError("请由运维主人创建团，再登记本团KP和玩家")
                name = text(args.get("name"), "团名", 60)
                campaign = {
                    "id": uid(),
                    "body": ctx.body,
                    "channel": ctx.channel,
                    "name": name,
                    "status": "draft",
                    "kp": ctx.person_id,
                    "version": 0,
                    "state": {
                        "members": {ctx.person_id: {"role": "kp"}},
                        "characters": {},
                        "scenes": {},
                        "scene": "",
                        "flags": {},
                        "facts": {},
                        "known": [],
                        "day": 1,
                        "minutes": 0,
                        "combat": {},
                        "house_rules": {},
                        "ruleset": "coc7-20261111",
                    },
                }
                conn.execute(
                    "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?)",
                    (
                        *[
                            campaign[k]
                            for k in ("id", "body", "channel", "name", "status", "kp", "version")
                        ],
                        dump(campaign["state"]),
                    ),
                )
                message = (
                    f"团 {name} 已建立（{campaign['id']}），状态：准备。请登记玩家、角色和场景。"
                )
            else:
                campaign = self.campaign(ctx, cid, conn)
                if ctx.trpg_principal and (
                    not self.is_host(campaign, ctx) or operation not in HOST_OPS
                ):
                    raise SkillError("本团主持权限不能用于成员、改卡、授权或其它管理操作")
                if "expected_version" in args:
                    expected = args.pop("expected_version")
                    if expected != campaign["version"]:
                        raise SkillError("团状态已经变化，请重新查看后操作，未覆盖其他人的行动")
                if operation in READ_ONLY:
                    return self._read(conn, campaign, ctx, operation, args)
                message = self._mutate(conn, campaign, ctx, operation, args)
                if campaign.pop("_unchanged", False):
                    return {
                        "text": message,
                        "campaign": campaign["id"],
                        "version": campaign["version"],
                        "changed": False,
                        "visibility": "actor",
                    }
            Store.save(conn, campaign)
            # 仅可见的程序摘要进入公共事件；输入的KP材料不复制到普通日志。
            visibility = (
                "kp"
                if operation
                in {
                    "场景",
                    "事实",
                    "改卡",
                    "房规",
                    "回滚",
                    "恢复草案",
                    "模组草案",
                    "采纳模组",
                    "状态裁定",
                    "裁定",
                    "主持授权",
                }
                else "public"
            )
            Store.event(
                conn,
                campaign["id"],
                ctx.person_id,
                operation,
                message,
                data={"request": args, "request_id": key},
                visibility=visibility,
            )
            response = {
                "text": message,
                "campaign": campaign["id"],
                "version": campaign["version"],
                "visibility": visibility,
                "changed": True,
                "outbox": uid(),
            }
            conn.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,?,?)",
                (
                    response["outbox"],
                    campaign["id"],
                    ctx.body,
                    ctx.channel,
                    message,
                    "pending",
                    now(),
                ),
            )
            if key:
                conn.execute(
                    "INSERT INTO receipts VALUES(?,?,?,?)", (scope, key, request, dump(response))
                )
            return response

    def _read(self, conn, campaign, ctx, operation, args):
        cid = campaign["id"]
        if operation == "状态":
            value = self.view(campaign, ctx, private=bool(args.get("private")))
        elif operation == "角色":
            who = args.get("person", ctx.person_id)
            if who != ctx.person_id:
                self.kp(campaign, ctx)
                if not ctx.private:
                    raise SkillError("其他玩家的完整卡请在KP私聊或控制台查看")
            _, value = self.pc(campaign, who)
        elif operation == "战报":
            private = bool(args.get("private"))
            if private:
                self.kp(campaign, ctx)
                if not ctx.private:
                    raise SkillError("KP战报仅在私聊查看")
            rows = conn.execute(
                "SELECT seq,kind,text,created,visibility FROM events WHERE campaign=? "
                "ORDER BY seq DESC LIMIT 100",
                (cid,),
            ).fetchall()
            value = [dict(r) for r in reversed(rows) if private or r["visibility"] == "public"]
        elif operation == "场景列表":
            value = [
                {"id": key, "title": s["title"]}
                for key, s in campaign["state"]["scenes"].items()
                if key == campaign["state"]["scene"]
                or key in campaign["state"]["flags"].get("visited", [])
            ]
        elif operation == "检定列表":
            rows = conn.execute(
                "SELECT id,actor,status,spec,result FROM checks WHERE campaign=? "
                "ORDER BY rowid DESC LIMIT 20",
                (cid,),
            ).fetchall()
            value = [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "reason": json.loads(r["spec"])["reason"],
                    "result": json.loads(r["result"]) if r["result"] else None,
                }
                for r in rows
                if r["actor"] == ctx.person_id or self.role(campaign, ctx) == "kp"
            ]
            # 完整对手数值只保存在受保护账本，不走群内读接口。
            for row in value:
                if row["result"]:
                    row["result"] = self._public_result(row["result"])
        elif operation == "回执":
            self.kp(campaign, ctx)
            value = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,state,created FROM outbox WHERE campaign=? "
                    "ORDER BY rowid DESC LIMIT 30",
                    (cid,),
                )
            ]
        else:
            value = [
                dict(r)
                for r in conn.execute(
                    "SELECT day,SUM(calls) calls,SUM(input_chars) input_chars,"
                    "SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,"
                    "SUM(cached_tokens) cached_tokens FROM metrics WHERE campaign=? GROUP BY day",
                    (cid,),
                )
            ]
        return {
            "text": dump(value),
            "campaign": cid,
            "version": campaign["version"],
            "visibility": "kp" if args.get("private") else "actor",
            "changed": False,
            "data": value,
        }

    def _mutate(self, conn, campaign, ctx, operation, args):
        state, cid = campaign["state"], campaign["id"]
        if operation in {"场景", "事实", "NPC", "采纳模组"}:
            self.kp(campaign, ctx)
            state["knowledge_version"] = state.get("knowledge_version", 0) + 1
            if state.get("ai_kp"):
                state["ai_kp"]["allow_private_model"] = False
        if operation == "公开主持":
            self.kp(campaign, ctx)
            if not ctx.owner or ctx.trpg_principal:
                raise SkillError("只有本团运维主人能启用公开资料主持")
            state["ai_kp"] = {
                "id": uid(),
                "enabled": True,
                "principal": "ai-kp:" + cid,
                "allow_private_model": False,
                "mode": "public",
                "sources": [],
                "knowledge_version": state.get("knowledge_version", 0),
                "by": ctx.person_id,
                "created": now(),
            }
            return "白璎已接手公开资料主持，KP私有资料仍不进入此通道。"
        if operation == "主持授权":
            self.kp(campaign, ctx)
            if not ctx.owner or ctx.trpg_principal:
                raise SkillError("只有本团运维主人能授权或撤销模型读取KP资料")
            enabled = args.get("enabled")
            if not isinstance(enabled, bool):
                raise SkillError("enabled须为布尔值")
            if enabled:
                if args.get("allow_private_model") is not True:
                    raise SkillError("须明确允许专用主持模型读取本团指定KP资料")
                consent = text(args.get("consent"), "授权依据", 500)
                hashes = args.get("sources", [])
                known = {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT sha FROM documents WHERE campaign=?", (cid,)
                    )
                }
                if (
                    not isinstance(hashes, list)
                    or not hashes
                    or any(
                        not isinstance(h, str)
                        or len(h) != 64
                        or not all(x in "0123456789abcdef" for x in h)
                        for h in hashes
                    )
                    or not set(hashes) <= known
                ):
                    raise SkillError("授权来源须为本团已入库的确切版本")
                state["ai_kp"] = {
                    "id": uid(),
                    "enabled": True,
                    "principal": "ai-kp:" + cid,
                    "allow_private_model": True,
                    "provider": "subscription",
                    "sources": hashes,
                    "consent": consent,
                    "module_sha": state.get("module", {}).get("manifest_sha"),
                    "knowledge_version": state.get("knowledge_version", 0),
                    "by": ctx.person_id,
                    "created": now(),
                }
            else:
                state.setdefault("ai_kp", {})["enabled"] = False
            return "本团白璎主持授权已更新；玩家身份及其它团权限不变。"
        if operation in {"主持行动", "主持遭遇", "主持发作", "主持事件", "主持续团", "主持检定"}:
            if not self.is_host(campaign, ctx):
                raise SkillError("只能由服务器专用主持通道执行")
            actor = text(args.get("actor"), "请求玩家", 80)
            if state["members"].get(actor, {}).get("role") not in {"kp", "player"}:
                raise SkillError("主持请求人不是本团成员")
            player = replace(ctx, person_id=actor, trpg_principal=None)
            if operation == "主持检定":
                from companion.trpg.dialogue import check_plan

                if any(
                    row["actor"] == actor
                    or state["characters"]
                    .get(json.loads(row["spec"]).get("target"), {})
                    .get("owner")
                    == actor
                    for row in conn.execute(
                        "SELECT actor,spec FROM checks WHERE campaign=? "
                        "AND status IN ('ready','rolled')",
                        (cid,),
                    )
                ):
                    raise SkillError("先完成当前检定，不能同时建立另一项检定")
                return self._request(conn, campaign, check_plan(state, actor, args.get("plan")))
            if operation == "主持事件":
                if (
                    not state.get("module", {}).get("darkness_clock")
                    or state["flags"].get("power_on")
                    or state.get("combat")
                    or state["scene"] == "escaped"
                    or state["minutes"] // 15 <= state["flags"].get("darkness_cycle", 0)
                ):
                    campaign["_unchanged"] = True
                    return ""
                state["flags"]["darkness_cycle"] = state["minutes"] // 15
                state["flags"]["lights_off"] = True
                return (
                    "按已确认的剧情耗时，灯光忽然熄灭。此刻无法借光观察；"
                    "你可以聆听或说“等灯光恢复”。没有因离线追加伤害。"
                )
            if operation == "主持发作":
                card_id, card = self.pc(campaign, actor)
                affected = [
                    v
                    for v in card["conditions"]
                    if v
                    in {"临时疯狂，发作症状由KP裁定", "达到本叙事日SAN五分之一，不定性疯狂待KP处理"}
                ]
                if not affected or card["SAN"] == 0:
                    raise SkillError("没有可按此流程处理的短期发作；不自动治愈永久状态")
                if state.get("combat", {}).get("order"):
                    raise SkillError(
                        "仍处战斗，不能把发作快进成无伤；先处理已宣告的危险，再由主持确认场面"
                    )
                rounds = self.rng(10) + 1
                hours = self.rng(10) + 1 if any(v.startswith("临时") for v in affected) else None
                card["conditions"] = [v for v in card["conditions"] if v not in affected]
                card["conditions"].append(
                    ("不定性疯狂" if any("不定性" in v for v in affected) else "临时疯狂")
                    + "（本次发作结束）"
                )
                card["bout_resolved"] = True
                card["insanity_hours"] = hours
                card["version"] += 1
                state["elapsed_rounds"] = state.get("elapsed_rounds", 0) + rounds
                check_id, record_id = uid(), uid()
                result = {
                    "bout_rounds": rounds,
                    "temporary_hours": hours,
                    "symptom": "短暂的情绪失控",
                    "source": "Chaosium官方理智规则；主持选择温和的情绪发作表现",
                    "record_id": record_id,
                }
                conn.execute(
                    "INSERT INTO checks VALUES(?,?,?,?,?,?,?)",
                    (
                        check_id,
                        cid,
                        actor,
                        "applied",
                        dump({"kind": "bout", "card": card_id}),
                        dump(result),
                        now(),
                    ),
                )
                conn.execute(
                    "INSERT INTO rolls VALUES(?,?,?,?,?)",
                    (record_id, cid, check_id, dump(result), now()),
                )
                return (
                    f"白璎将这次发作按短暂情绪失控处理，持续{rounds}轮；这段发作已结束。"
                    + (
                        f"临时疯狂总时长记录为{hours}小时，尚未因处理发作而消除。"
                        if hours
                        else "不定性疯狂仍需后续剧情与治疗处理。"
                    )
                    + "HP和SAN没有恢复，物品与位置未改变。\n骰记录："
                    + record_id
                )
            if operation == "主持遭遇":
                if state.get("combat", {}).get("order"):
                    campaign["_unchanged"] = True
                    return "当前战斗仍在进行。"
                from companion.trpg.host_rules import actions_for

                visible = {a.get("target") for a in actions_for(state, actor)}
                profiles = state.get("module", {}).get("npcs", {})
                enemies = [
                    key
                    for key in state["scenes"][state["scene"]].get("npcs", [])
                    if key in visible
                    and state["flags"].get(profiles.get(key, {}).get("hostile_flag"))
                ]
                if not enemies:
                    campaign["_unchanged"] = True
                    return "没有已触发的敌对遭遇。"
                pc_id, _ = self.pc(campaign, actor)
                self._mutate(conn, campaign, ctx, "战斗", {"characters": [pc_id, *enemies]})
                if profiles[enemies[0]].get("ambush"):
                    state["combat"]["turn"] = enemies[0]
                    state["combat"]["position"] = state["combat"]["order"].index(enemies[0])
                return "对方已注意到你的行动，按已登记的DEX和遭遇规则进入战斗。"
            if operation == "主持续团":
                if state.get("needs_confirmation") and args.get("confirmed") is not True:
                    raise SkillError("先由请求人确认公开恢复摘要")
                state["needs_confirmation"] = False
                return self._mutate(conn, campaign, ctx, "续团", {})
            from companion.trpg.host_rules import choose_action

            action = choose_action(state, actor, args.get("plan"))
            if campaign["status"] != "active":
                raise SkillError("团已暂停，尚未执行行动")
            if action.get("kind") == "combat":
                card_id, _ = self.pc(campaign, actor)
                if not state.get("combat", {}).get("order"):
                    self._mutate(
                        conn, campaign, ctx, "战斗", {"characters": [card_id, action["target"]]}
                    )
            return self._action(conn, campaign, player, action)
        if operation not in {
            "投骰",
            "接受结果",
            "花幸运",
            "应对",
            "暂停",
            "续团",
            "确认恢复",
            "主持续团",
        }:
            if conn.execute(
                "SELECT id FROM checks WHERE campaign=? AND status='rolled'", (cid,)
            ).fetchone():
                raise SkillError("先接受已保存的失败结果或决定花幸运，再改变团状态")
        gm_ops = {
            "成员",
            "暂停",
            "开团",
            "续团",
            "结束",
            "场景",
            "事实",
            "转场",
            "检定",
            "改卡",
            "物品",
            "下一日",
            "推进时间",
            "战斗",
            "下一位",
            "房规",
            "回滚",
            "恢复草案",
            "模组草案",
            "采纳模组",
            "状态裁定",
            "结束战斗",
            "裁定",
        }
        if operation in gm_ops:
            self.kp(campaign, ctx)
        if operation == "成员":
            person = text(args.get("person"), "固定人物ID", 80)
            role = args.get("role", "player")
            if role not in {"kp", "player", "observer"}:
                raise SkillError("角色身份须为kp/player/observer")
            member = state["members"].setdefault(person, {})
            if (
                member.get("role") == "kp"
                and role != "kp"
                and sum(m.get("role") == "kp" for m in state["members"].values()) == 1
            ):
                raise SkillError("不能移除最后一名KP；请先登记接任者")
            member["role"] = role
            return f"已登记成员 {person}，身份 {role}。"
        if operation in {"暂停", "开团", "续团", "结束"}:
            for row in conn.execute(
                "SELECT id,spec FROM checks WHERE campaign=? AND status='ready'", (cid,)
            ):
                spec = json.loads(row["spec"])
                if spec["campaign_version"] == campaign["version"]:
                    spec["campaign_version"] += 1
                    conn.execute("UPDATE checks SET spec=? WHERE id=?", (dump(spec), row["id"]))
            if operation in {"开团", "续团"}:
                if state.get("needs_confirmation"):
                    raise SkillError("这是恢复草案，请先核对并执行 /团 确认恢复")
                if not state["scene"] or not any(
                    m.get("character") for m in state["members"].values()
                ):
                    raise SkillError("先配置起始场景和玩家角色卡")
                conflict = conn.execute(
                    "SELECT id FROM campaigns WHERE body=? AND channel=? "
                    "AND status='active' AND id!=?",
                    (campaign["body"], campaign["channel"], cid),
                ).fetchone()
                if conflict:
                    raise SkillError("同一聊天已有活跃团，请先暂停它")
                campaign["status"] = "active"
            else:
                campaign["status"] = "closed" if operation == "结束" else "paused"
            return f"{campaign['name']}：{campaign['status']}；进度已实时保存。"
        if operation == "确认恢复":
            self.kp(campaign, ctx)
            state["needs_confirmation"] = False
            return "恢复草案已确认，仍保持暂停；需要时执行续团。"
        if operation in {"建卡", "改卡"}:
            who = args.get("person", ctx.person_id)
            if who != ctx.person_id:
                self.kp(campaign, ctx)
            if who not in state["members"] or state["members"][who]["role"] == "observer":
                raise SkillError("先登记为本团玩家")
            if operation == "建卡" and state["members"][who].get("character"):
                raise SkillError("已有角色卡，不能用建卡覆盖")
            if campaign["status"] == "active":
                raise SkillError("改卡前先暂停，不能在看到骰点后临时改数值")
            if operation == "改卡":
                text(args.get("reason"), "改卡理由")
            data = rules.character(args.get("card", {}))
            name = text(args.get("name"), "角色名", 60)
            card_id = state["members"][who].get("character", uid())
            previous = state["characters"].get(card_id, {})
            data.update(name=name, owner=who, version=previous.get("version", 0) + 1, npc=False)
            state["characters"][card_id] = data
            state["members"][who]["character"] = card_id
            conn.execute(
                "UPDATE checks SET status='invalid' WHERE campaign=? AND actor=? "
                "AND status='ready'",
                (cid, who),
            )
            return f"{name}的独立团角色卡已保存，版本{data['version']}。"
        if operation == "授权":
            if self.role(campaign, ctx) not in {"player", "kp"}:
                raise SkillError("只有参团成员能授权自己的角色代骰")
            if not isinstance(args.get("auto_roll"), bool):
                raise SkillError("auto_roll须为true/false")
            state["members"][ctx.person_id]["auto_roll"] = args["auto_roll"]
            return "本人的代骰授权已更新。"
        if operation == "场景":
            from companion.trpg.scenes import validate_scene

            scene = validate_scene(args.get("scene", {}), state)
            state["scenes"][scene["id"]] = scene
            if not state["scene"]:
                state["scene"] = scene["id"]
            return f"场景已保存，编号{scene['id']}。秘密描述未公开。"
        if operation in {"模组草案", "采纳模组"}:
            from companion.trpg.scenes import validate_module

            if not ctx.private or campaign["status"] == "active":
                raise SkillError("请暂停后在KP私聊或本地控制台审查模组")
            if operation == "模组草案":
                state["module_draft"] = validate_module(args.get("draft"), state)
                return "本地模组草案已保存，尚未生效；请检查公开描述、行动风险和资料出处后采纳。"
            draft = validate_module(state.get("module_draft"), state)
            for scene in draft["scenes"]:
                state["scenes"][scene["id"]] = scene
            for fact in draft["facts"]:
                state["facts"][fact["id"]] = fact
            if not state["scene"]:
                state["scene"] = draft["scenes"][0]["id"]
            del state["module_draft"]
            return "已采纳审查后的模组场景；事实仍按触发条件公开，团保持暂停。"
        if operation == "状态裁定":
            text(args.get("reason"), "状态裁定理由")
            key = args.get("character")
            if key not in state["characters"]:
                raise SkillError("没有此角色")
            card = state["characters"][key]
            for field in ("add", "remove"):
                values = args.get(field, [])
                if not isinstance(values, list) or len(values) > 8:
                    raise SkillError("状态列表不合法")
                for value in values:
                    text(value, "状态", 80)
            card["conditions"] = list(
                dict.fromkeys(
                    [v for v in card["conditions"] if v not in args.get("remove", [])]
                    + args.get("add", [])
                )
            )
            card["version"] += 1
            return "KP已记录状态裁定及理由；不会改写原骰点。"
        if operation == "事实":
            fid = text(args.get("id"), "事实编号", 60)
            state["facts"][fid] = {
                "id": fid,
                "text": text(args.get("text"), "事实", 1600),
                "source": str(args.get("source", "KP裁定"))[:200],
            }
            if args.get("public") and fid not in state["known"]:
                state["known"].append(fid)
            return "事实已登记；只有显式公开或满足场景触发条件才进入玩家视图。"
        if operation == "转场":
            sid = args.get("scene")
            if sid not in state["scenes"]:
                raise SkillError("未配置此场景")
            state["scene"] = sid
            visited = state["flags"].setdefault("visited", [])
            if sid not in visited:
                visited.append(sid)
            return "已转场：" + state["scenes"][sid]["public"]
        if operation == "检定":
            return self._request(conn, campaign, args)
        if operation == "行动":
            if state.get("ai_kp", {}).get("enabled"):
                return self._mutate(
                    conn,
                    campaign,
                    self.host_context(campaign, ctx),
                    "主持行动",
                    {"actor": ctx.person_id, "plan": {"action_id": args.get("action")}},
                )
            from companion.trpg.scenes import available

            if campaign["status"] != "active":
                raise SkillError("当前团未开团或已暂停，剧情没有自动推进")
            if self.role(campaign, ctx) not in {"player", "kp"}:
                raise SkillError("旁观者的消息不会变成玩家行动")
            _, card = self.pc(campaign, ctx.person_id)
            if any(v in card["conditions"] for v in ("死亡", "昏迷")):
                raise SkillError("角色当前不能自行行动，需要KP先处理状态")
            if conn.execute(
                "SELECT id FROM checks WHERE campaign=? AND actor=? "
                "AND status IN ('ready','rolled')",
                (cid, ctx.person_id),
            ).fetchone():
                raise SkillError("还有待确认的检定，请先投骰或取消，不自动重投")
            action = next((a for a in available(state) if a["id"] == args.get("action")), None)
            if action is None:
                raise SkillError("没有当前条件下可用的此行动；需要KP补充裁定")
            return self._action(conn, campaign, ctx, action)
        if operation == "取消检定":
            row = conn.execute(
                "SELECT * FROM checks WHERE id=? AND campaign=?", (args.get("id"), cid)
            ).fetchone()
            if row is None or (row["actor"] != ctx.person_id and self.role(campaign, ctx) != "kp"):
                raise SkillError("没有可操作的此检定")
            if row["status"] != "ready":
                raise SkillError("已经出目或失效，不能取消后重抽")
            if json.loads(row["spec"]).get("mandatory"):
                raise SkillError("已目击事件的理智检查不能取消逃避；可先暂停")
            conn.execute("UPDATE checks SET status='cancelled' WHERE id=?", (row["id"],))
            return "检定已取消，未掷骰。"
        if operation in {"投骰", "应对", "孤注", "花幸运", "接受结果"}:
            return self._resolve(conn, campaign, ctx, operation, args)
        if operation == "物品":
            _, card = self.pc(campaign, args.get("person", ctx.person_id))
            name = text(args.get("name"), "物品", 60)
            delta = rules.integer(args.get("delta"), -9999, 9999, "数量变化")
            text(args.get("reason"), "物品变化理由")
            total = card["items"].get(name, 0) + delta
            rules.integer(total, 0, 9999, "剩余数量")
            card["items"][name] = total
            card["version"] += 1
            return f"物品已更新：{name} × {total}。"
        if operation in {"下一日", "推进时间"}:
            if operation == "下一日":
                state["day"] += 1
                state["minutes"] = 0
                for card in state["characters"].values():
                    card["san_day_start"], card["san_loss_today"] = card["SAN"], 0
            else:
                state["minutes"] += rules.integer(args.get("minutes"), 0, 1440, "剧情分钟")
            return (
                f"叙事日{state['day']}，当日已确认耗时{state['minutes']}分钟；"
                "不按现实离线时间推进。"
            )
        if operation == "战斗":
            ids = args.get("characters", [])
            if not isinstance(ids, list) or not 2 <= len(ids) <= 20 or len(ids) != len(set(ids)):
                raise SkillError("战斗需2～20个不重复角色编号")
            if any(i not in state["characters"] for i in ids):
                raise SkillError("有角色尚未登记")
            order = sorted(ids, key=lambda key: (-state["characters"][key]["DEX"], key))
            state["combat"] = {
                "order": order,
                "turn": order[0],
                "round": 1,
                "position": 0,
                "defended": {},
            }
            return "战斗已按DEX排序，等待当前角色行动与目标选择闪避/反击。"
        if operation == "NPC":
            self.kp(campaign, ctx)
            card = rules.character(args.get("card", {}), npc=True)
            key = uid()
            state["characters"][key] = {
                **card,
                "name": text(args.get("name"), "NPC名", 60),
                "owner": "",
                "npc": True,
            }
            return f"NPC已登记，编号{key}；数值不会通过公开状态接口暴露。"
        if operation == "下一位":
            self._advance_turn(state)
            return "已推进战斗回合。"
        if operation == "结束战斗":
            state["combat"] = {}
            return "战斗已结束，原有伤势、弹药和战斗记录保留。"
        if operation == "房规":
            name = args.get("name")
            if name != "luck_spending" or not isinstance(args.get("enabled"), bool):
                raise SkillError("本版仅支持显式启停luck_spending；其余房规请记为KP裁定，不改骰点")
            state["house_rules"][name] = args["enabled"]
            return "房规快照已更新；已有请求保持原规则，不追溯修改。"
        if operation == "裁定":
            state.setdefault("rulings", []).append(
                {
                    "name": text(args.get("name"), "裁定名", 80),
                    "text": text(args.get("text"), "已采用裁定", 1600),
                    "source": text(args.get("source"), "出处或KP解释", 200),
                    "public": args.get("public") is True,
                }
            )
            return "已记录本团采用的裁定及出处；不追溯改点或自动改写规则算法。"
        if operation == "回滚":
            if campaign["status"] == "active":
                raise SkillError("先暂停再回滚")
            text(args.get("reason"), "回滚原因")
            row = conn.execute(
                "SELECT state FROM snapshots WHERE campaign=? AND version=?",
                (cid, args.get("version")),
            ).fetchone()
            if row is None:
                raise SkillError("没有这个快照")
            restored = json.loads(row["state"])
            # 已公开信息无法让玩家忘记，保留其可见性；骰点账本不回滚。
            restored["facts"].update(state["facts"])
            restored["known"] = list(dict.fromkeys(restored["known"] + state["known"]))
            campaign["state"] = restored
            conn.execute(
                "UPDATE checks SET status='invalid' WHERE campaign=? AND status='ready'", (cid,)
            )
            return "已追加回滚事件；原骰点、历史和已公开线索仍保留。"
        raise SkillError("未知团务操作，请用 /团 帮助")

    @staticmethod
    def scene_description(state):
        scene = state["scenes"].get(state["scene"], {})
        for variant in scene.get("variants", []):
            if all(state["flags"].get(k) == v for k, v in variant.get("requires", {}).items()):
                return variant["public"]
        return scene.get("public", "尚未配置场景")

    def _action(self, conn, campaign, ctx, action):
        from companion.trpg.scenes import apply

        state = campaign["state"]
        from companion.trpg.host_rules import trial_fingerprint

        _, card = self.pc(campaign, ctx.person_id)
        for pending in conn.execute(
            "SELECT actor,spec FROM checks WHERE campaign=? AND status IN ('ready','rolled')",
            (campaign["id"],),
        ):
            target = state["characters"].get(json.loads(pending["spec"]).get("target"), {})
            if pending["actor"] == ctx.person_id or target.get("owner") == ctx.person_id:
                raise SkillError("先确认当前检定或应对，不能用另一个行动跳过已宣告的危险")
        if (
            state.get("combat", {}).get("order")
            and action.get("skill")
            and action.get("kind", "skill") not in {"combat", "opposed"}
        ):
            raise SkillError("当前仍在战斗，先处理对手或撤离，再进行搜索和治疗")
        if not rules.can_act(card):
            raise SkillError("角色当前不能自行行动，白璎须先处理状态")
        if conn.execute(
            "SELECT id FROM checks WHERE campaign=? AND actor=? AND status IN ('ready','rolled')",
            (campaign["id"], ctx.person_id),
        ).fetchone():
            raise SkillError("先确认当前检定或决定幸运，原请求不会重投")
        if any(card["items"].get(k, 0) < v for k, v in action.get("items", {}).items()):
            raise SkillError("所需物品不在角色当前物品栏")
        if action.get("any_item") and not any(card["items"].get(k, 0) for k in action["any_item"]):
            raise SkillError("缺少此行动所需工具")
        if action.get("skill"):
            return self._request(
                conn,
                campaign,
                {
                    **action,
                    "actor": ctx.person_id,
                    "reason": action["title"],
                    "template": action["id"],
                    "try_fingerprint": trial_fingerprint(state),
                },
            )
        old_scene = state["scene"]
        revealed = apply(state, card, action.get("success", []))
        if state["scene"] != old_scene:
            state["combat"] = {}
        card["version"] += 1
        message = action.get("success_text", action["title"] + "已完成。")
        message += "\n" + "\n".join(f["text"] for f in revealed) if revealed else ""
        if state["scene"] != old_scene:
            message += "\n" + self.scene_description(state)
        followup = self._scene_sanity(
            conn, campaign, ctx.person_id, action.get("sanity"), changed=state["scene"] != old_scene
        )
        return message + ("\n" + followup if followup else "")

    def _scene_sanity(self, conn, campaign, actor, event=None, *, changed=False):
        state = campaign["state"]
        if event is None and changed:
            event = state["scenes"][state["scene"]].get("sanity")
        if not event or state["flags"].get("san:" + event["id"]):
            return ""
        marker = {"type": "flag", "name": "san:" + event["id"], "value": True}
        return self._request(
            conn,
            campaign,
            {
                "actor": actor,
                "kind": "sanity",
                "reason": event.get("reason", "首次目击当前景象"),
                "risk": "理智损失 " + event["success_loss"] + "/" + event["failure_loss"],
                "success_loss": event["success_loss"],
                "failure_loss": event["failure_loss"],
                "success": [marker],
                "failure": [marker],
                "mandatory": True,
            },
        )

    def _request(self, conn, campaign, args):
        state = campaign["state"]
        actor = text(args.get("actor"), "检定角色所属人物ID", 80)
        if actor in state["characters"] and state["characters"][actor].get("npc"):
            card_id, card = actor, state["characters"][actor]
        else:
            card_id, card = self.pc(campaign, actor)
        if campaign["status"] != "active":
            raise SkillError("先开团或续团，再发起正式检定")
        kind = args.get("kind", "skill")
        if kind not in {"skill", "sanity", "opposed", "combat", "heal", "growth", "dying"}:
            raise SkillError("不支持此检定类型")
        if kind in {"skill", "combat", "opposed", "heal"} and not rules.can_act(card):
            raise SkillError("角色不能自行行动或有疯狂待裁定，请KP先处理状态")
        from companion.trpg.scenes import validate_effects

        for branch in ("success", "failure"):
            validate_effects(args.get(branch, []))
        skill = args.get(
            "skill",
            "SAN"
            if kind == "sanity"
            else "急救"
            if kind == "heal"
            else "CON"
            if kind == "dying"
            else "",
        )
        value = rules.skill_value(card, skill) if kind != "growth" else 0
        reason = text(args.get("reason"), "检定目标", 300)
        difficulty = args.get("difficulty", "常规")
        bonus, penalty = args.get("bonus", 0), args.get("penalty", 0)
        if (
            kind == "combat"
            and not args.get("firearm")
            and state.get("combat", {}).get("defended", {}).get(args.get("target"), 0)
        ):
            net = max(-2, min(2, bonus - penalty + 1))
            bonus, penalty = max(0, net), max(0, -net)
        # 验证参数但不抽样。
        rules.check(value, difficulty, bonus, penalty, lambda _: 0)
        spec = {
            "actor": actor,
            "card": card_id,
            "card_version": card["version"],
            "kind": kind,
            "skill_name": skill,
            "value": value,
            "difficulty": difficulty,
            "bonus": bonus,
            "penalty": penalty,
            "reason": reason,
            "risk": str(args.get("risk", "失败后按已宣告的规则处理，不自行增加后果"))[:800],
            "success": args.get("success", []),
            "failure": args.get("failure", []),
            "success_text": str(args.get("success_text", "目标达成"))[:800],
            "failure_text": str(args.get("failure_text", "目标未达成"))[:800],
            "campaign_version": campaign["version"] + 1,
            "pushed": bool(args.get("pushed", False)),
            "rules": copy.deepcopy(state["house_rules"]),
            "mandatory": args.get("mandatory") is True,
            "requires_critical": args.get("requires_critical") is True,
            "escape": args.get("escape") is True,
            "legacy_origin": str(args.get("legacy_origin", ""))[:80],
            "template": str(args.get("template", ""))[:160],
            "attempt_base": str(args.get("attempt_base", args.get("template", "")))[:160],
            "try_fingerprint": str(args.get("try_fingerprint", ""))[:64],
        }
        if args.get("sanity"):
            event = args["sanity"]
            if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                raise SkillError("后续理智事件格式不对")
            for field in ("success_loss", "failure_loss"):
                rules.loss(event[field], lambda _: 0)
            spec["sanity_event"] = copy.deepcopy(event)
        if args.get("failure_damage"):
            expression = text(args["failure_damage"], "已宣告失败伤害", 160)
            rules.loss(expression, lambda _: 0)
            spec["failure_damage"] = expression
        if kind == "sanity":
            for field in ("success_loss", "failure_loss"):
                spec[field] = text(
                    args.get(field, "0" if field == "success_loss" else "1d3"), field, 160
                )
                rules.loss(spec[field], lambda _: 0)
        if kind in {"combat", "opposed"}:
            target = args.get("target")
            if target == card_id or target not in state["characters"]:
                raise SkillError("对手须为已登记的另一个角色编号")
            defender = state["characters"][target]
            spec.update(
                target=target,
                target_version=defender["version"],
                defense=args.get("defense"),
                target_skill=args.get(
                    "target_skill", "闪避" if args.get("defense") == "闪避" else "斗殴"
                ),
                counter_skill=args.get("counter_skill", args.get("target_skill", "斗殴")),
            )
            if spec["defense"] not in {None, "闪避", "反击", "普通", "不应对"}:
                raise SkillError("应对须为闪避、反击或不应对")
            spec["defense_confirmed"] = bool(defender.get("npc")) or kind == "opposed"
            if kind == "combat":
                combat = state.get("combat", {})
                if combat.get("turn") != card_id:
                    raise SkillError("请先建立战斗并等到此角色的DEX轮次")
                for field in ("damage", "counter_damage", "db", "target_db"):
                    spec[field] = str(args.get(field, "1d3" if "damage" in field else "0"))
                    rules.Expression(spec[field])
                spec["impale"] = bool(args.get("impale", False))
                spec["weapon"] = str(args.get("weapon", ""))[:60]
                if args.get("firearm"):
                    if spec["defense"] in {"闪避", "反击"}:
                        raise SkillError(
                            "枪械不能按近战闪避/反击；扑倒掩蔽、连射等先由KP定难度与惩罚"
                        )
                    ammo = text(args.get("ammo"), "弹药物品名", 60)
                    if card["items"].get(ammo, 0) < 1:
                        raise SkillError("没有可用弹药")
                    spec["ammo"] = ammo
        if kind == "heal":
            target = args.get("target", card_id)
            if target not in state["characters"]:
                raise SkillError("没有此治疗目标")
            spec.update(
                target=target,
                target_version=state["characters"][target]["version"],
                heal_type=args.get("heal_type", "first_aid"),
                wound=args.get("wound", 0),
            )
            if spec["heal_type"] not in {"first_aid", "medicine"}:
                raise SkillError("治疗类型须为first_aid/medicine")
            target_card = state["characters"][target]
            if "死亡" in target_card["conditions"]:
                raise SkillError("死亡不能用普通治疗复活")
            if spec["heal_type"] == "medicine" and args.get("equipment") is not True:
                raise SkillError("医学治疗须由KP确认有合适装备（equipment=true），且耗时一小时")
            index = rules.integer(
                spec["wound"], 0, max(0, len(target_card["wounds"]) - 1), "伤口编号"
            )
            if not target_card["wounds"] or target_card["wounds"][index].get(spec["heal_type"]):
                raise SkillError("伤口不存在或已尝试过此类治疗")
        key = uid()
        conn.execute(
            "INSERT INTO checks VALUES(?,?,?,?,?,?,?)",
            (key, campaign["id"], actor, "ready", dump(spec), None, now()),
        )
        target_value = value // {"常规": 1, "困难": 2, "极难": 5}[difficulty]
        if kind == "growth":
            return (
                f"检定 {key}：{reason}；按已勾选的技能逐项进行成长检查，"
                f"不是普通成功率检定。\n确认后用 /团 投骰 {key}；尚未掷骰。"
            )
        if card.get("npc"):
            return (
                f"检定 {key}：{reason}；对手数值已封存。"
                f"\n风险：{spec['risk']}。目标玩家先选应对，KP再结算；尚未掷骰。"
            )
        warning = "\n注意：成功率很低，请明确选择是否尝试。" if value < 20 else ""
        if spec["requires_critical"]:
            warning += "\n按本场景来源，只有大成功才算成功。"
        return (
            f"检定 {key}：{reason}；{skill}完整值{value}，"
            f"{difficulty}目标{target_value}，奖{bonus}/罚{penalty}。"
            f"\n失败风险：{spec['risk']}{warning}\n玩家确认后用 /团 投骰 {key}；尚未掷骰。"
        )

    def _resolve(self, conn, campaign, ctx, operation, args):
        cid, state = campaign["id"], campaign["state"]
        row = conn.execute(
            "SELECT * FROM checks WHERE id=? AND campaign=?", (args.get("id"), cid)
        ).fetchone()
        if row is None:
            raise SkillError("当前团没有这个检定")
        spec = json.loads(row["spec"])
        card = state["characters"].get(spec["card"])
        if card is None:
            raise SkillError("原检定角色已不在当前状态中，请KP核对记录")
        if operation == "应对":
            target = state["characters"].get(spec.get("target"), {})
            if target.get("owner") != ctx.person_id or row["status"] != "ready":
                raise SkillError("只有尚未结算的被攻击角色本人能选择应对")
            mode = args.get("mode")
            if mode not in {"闪避", "反击", "不应对"}:
                raise SkillError("请选择闪避、反击或不应对")
            spec.update(
                defense=mode,
                defense_confirmed=True,
                target_skill="闪避" if mode == "闪避" else spec.get("counter_skill", "斗殴"),
                campaign_version=campaign["version"] + 1,
            )
            if mode == "反击" and args.get("weapon"):
                weapon_name = args["weapon"]
                weapon = state.get("module", {}).get("weapons", {}).get(weapon_name)
                if not weapon or weapon.get("firearm") or target["items"].get(weapon_name, 0) < 1:
                    raise SkillError("反击武器须为本人持有的已登记近战武器")
                spec.update(
                    counter_damage=weapon["damage"],
                    target_db=weapon.get("db", "0"),
                    target_skill=weapon["skill"],
                )
            conn.execute("UPDATE checks SET spec=? WHERE id=?", (dump(spec), row["id"]))
            return "防守选择已锁定，等待进攻方投骰结算。"
        if row["actor"] != ctx.person_id:
            member = state["members"].get(row["actor"], {})
            if self.role(campaign, ctx) != "kp" or not (member.get("auto_roll") or card.get("npc")):
                raise SkillError("只能操作自己的检定；KP代骰需要该玩家已明确授权")
        if operation == "孤注":
            self.kp(campaign, ctx)
            if row["status"] != "applied" or spec["kind"] != "skill" or spec["pushed"]:
                raise SkillError("只有一次普通失败技能可孤注；战斗/理智/幸运/已孤注不能这样重投")
            result = json.loads(row["result"])
            if result.get("pushed_to"):
                raise SkillError("这个失败已经发起过孤注，不能重复重投")
            if card["version"] != spec["card_version"] + 1:
                raise SkillError("原检定后角色卡已变，不能修改数值后再孤注")
            if (
                result.get("passed")
                or result.get("level") == "大失败"
                or spec["skill_name"] in {"幸运", "LUCK"}
            ):
                raise SkillError("此结果不支持孤注，请由KP处理")
            message = self._request(
                conn,
                campaign,
                {
                    **spec,
                    "skill": spec["skill_name"],
                    "pushed": True,
                    "risk": text(args.get("risk"), "必须先宣告更严重的失败后果"),
                    "reason": "孤注一掷：" + spec["reason"],
                    "failure": args.get("failure", spec["failure"]),
                },
            )
            result["pushed_to"] = message.split("：", 1)[0].removeprefix("检定 ")
            conn.execute("UPDATE checks SET result=? WHERE id=?", (dump(result), row["id"]))
            return message
        if operation in {"花幸运", "接受结果"} and row["status"] == "rolled":
            return self._settle_luck(conn, campaign, ctx, row, spec, card, operation, args)
        if operation == "花幸运":
            raise SkillError("只有等待幸运决定的检定能花点；已经接受的后果不能追溯改写")
        if row["status"] == "applied":
            campaign["_unchanged"] = True
            return "此检定已结算，不重投、不重复扣点：\n" + dump(
                self._public_result(json.loads(row["result"]))
            )
        if row["status"] == "rolled":
            campaign["_unchanged"] = True
            return "原骰点已保存，等待接受失败或花幸运，不重投：\n" + dump(
                self._public_result(json.loads(row["result"]))
            )
        if row["status"] != "ready" or campaign["status"] != "active":
            raise SkillError("检定已取消/失效或团已暂停")
        if (
            not card
            or card["version"] != spec["card_version"]
            or campaign["version"] != spec["campaign_version"]
        ):
            raise SkillError("卡或团状态已变，旧请求不能套用新数值；请KP取消后重新宣告")
        if (
            spec.get("target")
            and state["characters"][spec["target"]]["version"] != spec["target_version"]
        ):
            raise SkillError("目标角色状态已变，请重新宣告")
        if spec["kind"] == "sanity":
            result = rules.sanity(card, spec["success_loss"], spec["failure_loss"], self.rng)
            passed = result["check"]["passed"]
        elif spec["kind"] == "growth":
            result = {"growth": []}
            for name in list(dict.fromkeys(card["growth"])):
                if name not in card["skills"] or name in {"克苏鲁神话", "MYTHOS"}:
                    continue
                roll = self.rng(100) + 1
                gain = self.rng(10) + 1 if roll > card["skills"][name] or roll >= 96 else 0
                card["skills"][name] += gain
                result["growth"].append({"skill": name, "roll": roll, "gain": gain})
            card["growth"] = []
            passed = True
        else:
            if spec["kind"] == "combat" and not spec.get("defense_confirmed"):
                raise SkillError("等待目标玩家选择应对，尚未掷骰")
            result = rules.check(
                spec["value"], spec["difficulty"], spec["bonus"], spec["penalty"], self.rng
            )
            passed = result["passed"]
            if spec.get("requires_critical"):
                passed = result["passed"] = result["level"] == "大成功"
            if spec["kind"] in {"opposed", "combat"}:
                result, passed = self._combat(state, card, spec, result)
            elif spec["kind"] == "heal":
                target = state["characters"][spec["target"]]
                index = rules.integer(
                    spec["wound"], 0, max(0, len(target["wounds"]) - 1), "伤口编号"
                )
                if not target["wounds"]:
                    raise SkillError("须先登记明确伤口，不能无限重复治疗旧HP缺口")
                wound = target["wounds"][index]
                field = spec["heal_type"]
                if wound.get(field):
                    raise SkillError("同一伤口已经尝试此类治疗，不能刷急救")
                wound[field] = True
                if field == "medicine":
                    state["minutes"] += 60
                if passed:
                    amount = 1 if field == "first_aid" else self.rng(3) + 1
                    target["HP"] = min(target["HP_MAX"], target["HP"] + amount)
                    target["conditions"] = [
                        v
                        for v in target["conditions"]
                        if v not in {"昏迷", "濒死，须急救及每轮CON检定"}
                    ]
                    if target["HP"] * 2 >= target["HP_MAX"] or rules.RANK[result["level"]] >= 3:
                        target["conditions"] = [v for v in target["conditions"] if v != "重伤"]
                    result.update(healed=amount, target_HP=target["HP"])
                target["version"] += 1
            elif spec["kind"] == "dying":
                if not passed and "死亡" not in card["conditions"]:
                    card["conditions"].append("死亡")
            elif (
                passed
                and spec["skill_name"] in card["skills"]
                and spec["bonus"] <= spec["penalty"]
                and not spec["pushed"]
            ):
                if (
                    spec["skill_name"] not in {"克苏鲁神话"}
                    and spec["skill_name"] not in card["growth"]
                ):
                    card["growth"].append(spec["skill_name"])
        from companion.trpg.scenes import apply

        if (
            spec["kind"] == "skill"
            and not passed
            and result["level"] != "大失败"
            and not spec["pushed"]
            and not spec.get("requires_critical")
            and spec["rules"].get("luck_spending")
            and spec["skill_name"] not in {"LUCK", "幸运", "SAN", "理智"}
        ):
            result.update(
                check_id=row["id"],
                record_id=uid(),
                pending_luck=True,
                applied_version=campaign["version"] + 1,
            )
            conn.execute(
                "INSERT INTO rolls VALUES(?,?,?,?,?)",
                (result["record_id"], cid, row["id"], dump(result), now()),
            )
            conn.execute(
                "UPDATE checks SET status='rolled',result=? WHERE id=?", (dump(result), row["id"])
            )
            return (
                "原骰已锁定，失败后果尚未提交：\n"
                + dump(result)
                + f"\n请用 /团 接受结果 {row['id']} 或 /团 花幸运 {row['id']} 花费数值。"
            )
        old_scene = state["scene"]
        if spec.get("template"):
            state.setdefault("attempts", {}).setdefault(row["actor"], {})[
                spec.get("attempt_base") or spec["template"]
            ] = {
                "passed": passed,
                "pushed": spec["pushed"],
                "fingerprint": spec.get("try_fingerprint", ""),
            }
        revealed = apply(state, card, spec["success"] if passed else spec["failure"])
        if not passed and spec.get("failure_damage"):
            injury_roll = rules.loss(spec["failure_damage"], self.rng)
            result["failure_injury"] = {
                "roll": injury_roll,
                "injury": rules.wound(card, injury_roll["total"], self.rng),
            }
        if state["scene"] != old_scene:
            state["combat"] = {}
        elif spec.get("escape") and state.get("combat"):
            self._advance_turn(state)
        card["version"] += 1
        result.update(
            check_id=row["id"],
            applied_version=campaign["version"] + 1,
            revealed=revealed,
            consequence=spec["success_text"] if passed else spec["failure_text"],
            actor_npc=bool(card.get("npc")),
        )
        rid = uid()
        result["record_id"] = rid
        conn.execute(
            "INSERT INTO rolls VALUES(?,?,?,?,?)", (rid, cid, row["id"], dump(result), now())
        )
        conn.execute(
            "UPDATE checks SET status='applied',result=? WHERE id=?", (dump(result), row["id"])
        )
        message = "程序原始结算（不可改点）：\n" + dump(self._public_result(result))
        if state["scene"] != old_scene:
            message += "\n" + self.scene_description(state)
        if not card.get("npc"):
            followup = self._scene_sanity(
                conn,
                campaign,
                row["actor"],
                spec.get("sanity_event"),
                changed=state["scene"] != old_scene,
            )
            message += "\n" + followup if followup else ""
        return message

    def _settle_luck(self, conn, campaign, ctx, row, spec, card, operation, args):
        if campaign["status"] != "active" or card["version"] != spec["card_version"]:
            raise SkillError("角色已变或团已暂停，请由KP处理，不套用新卡或重投")
        result = json.loads(row["result"])
        if operation == "花幸运":
            cost = rules.integer(args.get("amount"), 1, card["LUCK"], "花费幸运")
            adjusted = result["roll"] - cost
            if adjusted < 2:
                raise SkillError("花幸运不能制造大成功或降到1以下")
            grade = rules.grade(adjusted, spec["value"], spec["difficulty"])
            if not grade["passed"]:
                raise SkillError("不足以达到所需难度，未扣点")
            card["LUCK"] -= cost
            result.update(luck_spent=cost, adjusted_roll=adjusted, adjusted_grade=grade)
            result.update(original_passed=result["passed"], passed=grade["passed"])
            branch = "success"
        else:
            branch = "failure"
        from companion.trpg.scenes import apply

        state = campaign["state"]
        old_scene = state["scene"]
        revealed = apply(state, card, spec[branch])
        if branch == "failure" and spec.get("failure_damage"):
            damage = rules.loss(spec["failure_damage"], self.rng)
            result["failure_injury"] = {
                "roll": damage,
                "injury": rules.wound(card, damage["total"], self.rng),
            }
        if state["scene"] != old_scene:
            state["combat"] = {}
        if spec.get("template"):
            campaign["state"].setdefault("attempts", {}).setdefault(row["actor"], {})[
                spec.get("attempt_base") or spec["template"]
            ] = {
                "passed": branch == "success",
                "pushed": spec["pushed"],
                "fingerprint": spec.get("try_fingerprint", ""),
            }
        card["version"] += 1
        result.update(
            pending_luck=False,
            consequence=spec[branch + "_text"],
            revealed=revealed,
            applied_version=campaign["version"] + 1,
        )
        conn.execute(
            "UPDATE checks SET status='applied',result=? WHERE id=?", (dump(result), row["id"])
        )
        message = "原骰点保留，结果已提交：\n" + dump(self._public_result(result))
        followup = self._scene_sanity(
            conn,
            campaign,
            row["actor"],
            spec.get("sanity_event"),
            changed=state["scene"] != old_scene,
        )
        return message + ("\n" + followup if followup else "")

    def _combat(self, state, card, spec, attack):
        target = state["characters"][spec["target"]]
        if not spec.get("defense_confirmed"):
            raise SkillError("等待目标玩家明确选择闪避/反击，不替玩家决定")
        mode = spec.get("defense") or "不应对"
        defense = (
            rules.check(rules.skill_value(target, spec["target_skill"]), "常规", 0, 0, self.rng)
            if mode != "不应对"
            else {"level": "失败", "skill": 0, "roll": None}
        )
        winner = rules.oppose(attack, defense, mode)
        result = {
            **attack,
            "defender": defense,
            "defender_level": defense["level"],
            "winner": winner,
        }
        if spec["kind"] == "combat":
            if not card.get("npc"):
                state["combat"]["last_firearm"] = bool(spec.get("ammo"))
                state["combat"]["last_weapon"] = spec.get("weapon", "空手")
            if spec.get("ammo"):
                if card["items"].get(spec["ammo"], 0) < 1:
                    raise SkillError("弹药不足，未发射")
                card["items"][spec["ammo"]] -= 1
            if winner == "进攻方" or (winner == "防守方" and mode == "反击"):
                counter = winner == "防守方"
                damage = rules.damage(
                    spec["counter_damage"] if counter else spec["damage"],
                    spec["target_db"] if counter else spec["db"],
                    rules.RANK[(defense if counter else attack)["level"]] >= 3,
                    spec["impale"],
                    counter,
                    self.rng,
                )
                wounded = card if counter else target
                wounded_key = spec["card"] if counter else spec["target"]
                profile = state.get("module", {}).get("npcs", {}).get(wounded_key, {})
                original = damage["total"]
                if profile.get("firearm_half") and spec.get("ammo") and not counter:
                    damage["total"] //= 2
                damage["total"] = max(0, damage["total"] - profile.get("armor", 0))
                if damage["total"] != original:
                    damage["before_protection"] = original
                result["damage"] = damage
                injury = rules.wound(wounded, damage["total"], self.rng)
                result["injury"] = (
                    injury if counter or not target.get("npc") else {"damage": damage["total"]}
                )
            target["version"] += 1
            combat = state["combat"]
            combat["defended"][spec["target"]] = combat["defended"].get(spec["target"], 0) + 1
            attacks = (
                state.get("module", {}).get("npcs", {}).get(spec["card"], {}).get("attacks", 1)
            )
            left = combat.get("attacks_left", attacks)
            if card.get("npc") and card["HP"] > 0 and left > 1:
                combat["attacks_left"] = left - 1
            else:
                self._advance_turn(state)
            alive_npcs = [
                k
                for k in combat["order"]
                if state["characters"][k].get("npc")
                and state["characters"][k]["HP"] > 0
                and not set(state["characters"][k]["conditions"]) & {"死亡", "昏迷"}
            ]
            if any(state["characters"][k].get("npc") for k in combat["order"]) and not alive_npcs:
                state["combat"] = {}
                result["combat_ended"] = True
        return result, winner == "进攻方"

    @staticmethod
    def _advance_turn(state):
        combat = state.get("combat", {})
        if not combat.get("order"):
            raise SkillError("当前没有战斗")
        for _ in combat["order"]:
            combat["position"] = (combat["position"] + 1) % len(combat["order"])
            if combat["position"] == 0:
                combat["round"] += 1
                combat["defended"] = {}
            key = combat["order"][combat["position"]]
            card = state["characters"][key]
            if card["HP"] > 0 and not set(card["conditions"]) & {"死亡", "昏迷"}:
                combat["turn"] = key
                combat["attacks_left"] = (
                    state.get("module", {}).get("npcs", {}).get(key, {}).get("attacks", 1)
                )
                return
        combat["turn"] = ""

    @staticmethod
    def _public_result(result):
        value = {k: v for k, v in result.items() if k not in {"defender", "target_card", "private"}}
        if result.get("actor_npc"):
            hidden = {
                "skill",
                "target",
                "roll",
                "units",
                "tens",
                "candidates",
                "bonus",
                "penalty",
                "net_bonus",
                "net_penalty",
            }
            value = {k: v for k, v in value.items() if k not in hidden}
        return value
