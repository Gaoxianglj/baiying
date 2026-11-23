"""主持可选择的行动来自已采用模组与角色卡，不接受模型指定数值或任意写库。"""

from __future__ import annotations

import copy
import hashlib
import json

from companion.trpg.scenes import available


def trial_fingerprint(state):
    conditions = {
        k: state["flags"].get(k) for k in state.get("module", {}).get("retry_flags", ["power_on"])
    }
    return hashlib.sha256(json.dumps(conditions, sort_keys=True).encode()).hexdigest()


def actions_for(state, actor):
    member = state["members"].get(actor, {})
    key = member.get("character")
    if key not in state["characters"]:
        return []
    card = state["characters"][key]
    if state.get("module", {}).get("darkness_clock") and state["flags"].get("lights_off"):
        return [
            {
                "id": "env:wait",
                "title": "等待短暂黑暗过去",
                "aliases": ["等灯光恢复", "等待灯光", "等一会"],
                "success": [{"type": "flag", "name": "lights_off", "value": False}],
                "success_text": "短暂的黑暗结束，灯光重新亮起；位置和物品没有改变。",
            },
            {
                "id": "env:listen",
                "title": "在黑暗中留意声音",
                "aliases": ["黑暗中聆听", "仔细听声音"],
                "skill": "聆听",
                "risk": "失败没有额外辨明信息，不自动得到隐藏线索。",
                "success": [{"type": "flag", "name": "lights_off", "value": False}],
                "failure": [{"type": "flag", "name": "lights_off", "value": False}],
                "success_text": "你留意周围的声音，随后短暂的黑暗过去；未凭空获得未发现线索。",
            },
        ]
    choices = []
    for action in available(state):
        required = action.get("items", {})
        if any(card["items"].get(name, 0) < count for name, count in required.items()):
            continue
        if action.get("any_item") and not any(
            card["items"].get(name, 0) for name in action["any_item"]
        ):
            continue
        previous = state.get("attempts", {}).get(actor, {}).get(action["id"], {})
        if (
            action.get("skill")
            and not action.get("repeatable")
            and previous.get("passed") is False
            and previous.get("fingerprint") == trial_fingerprint(state)
        ):
            if action.get("push") and not previous.get("pushed"):
                pushed = copy.deepcopy(action)
                pushed.update(
                    id="push:" + action["id"],
                    attempt_base=action["id"],
                    pushed=True,
                    title="孤注一掷：" + action["title"],
                    aliases=["孤注一掷", "孤注"],
                    risk=action["push"]["risk"],
                    failure=action["push"]["failure"],
                )
                choices.append(pushed)
            continue
        choices.append(copy.deepcopy(action))
    module = state.get("module", {})
    scene = state["scenes"].get(state["scene"], {})
    combat = state.get("combat", {})
    enemies = [
        i
        for i in combat.get("order", [])
        if state["characters"][i].get("npc") and state["characters"][i]["HP"] > 0
    ]
    unbarred = [i for i in enemies if not is_barred(state, module.get("npcs", {}).get(i, {}))]
    if unbarred:
        for action in choices:
            if any(e["type"] == "scene" for e in action.get("success", [])):
                action.update(
                    kind="opposed",
                    skill="DEX",
                    escape=True,
                    target=unbarred[0],
                    defense="普通",
                    target_skill="DEX",
                    failure=[],
                    risk="本团撤离裁定：DEX对抗，失败未能脱离，轮到对方行动。",
                )
    for npc in scene.get("npcs", []):
        target = state["characters"].get(npc, {})
        profile = module.get("npcs", {}).get(npc, {})
        if not target or target["HP"] <= 0 or "死亡" in target["conditions"]:
            continue
        if any(state["flags"].get(k) != v for k, v in profile.get("requires", {}).items()):
            continue
        for name, weapon in module.get("weapons", {}).items():
            if name != "空手" and card["items"].get(name, 0) < 1:
                continue
            if "武器卡住：" + name in card["conditions"]:
                continue
            if weapon.get("ammo") and card["items"].get(weapon["ammo"], 0) < 1:
                continue
            choices.append(
                {
                    "id": "attack:" + npc + ":" + name,
                    "title": "用" + name + "攻击" + target["name"],
                    "kind": "combat",
                    "skill": weapon["skill"],
                    "target": npc,
                    "damage": weapon["damage"],
                    "db": weapon.get("db", "0"),
                    "counter_damage": profile.get("damage", "1d4"),
                    "target_db": profile.get("db", "0"),
                    "weapon": name,
                    "defense": "不应对"
                    if weapon.get("firearm")
                    else profile.get("defense", "闪避"),
                    "firearm": bool(weapon.get("firearm")),
                    "ammo": weapon.get("ammo"),
                    "impale": bool(weapon.get("impale")),
                    "penalty": 1 if is_barred(state, profile) and not weapon.get("firearm") else 0,
                    "risk": "攻击失败可能暴露空隙；近距离停留可能遭到对方反击。",
                    "source": profile.get("source", "已采用NPC资料"),
                    "aliases": [],
                    "success": [],
                    "failure": [],
                }
            )
    # 治疗仍由已有伤口规则检查，不能反复刷同一个伤口。
    for index, wound in enumerate(card["wounds"]):
        if not wound.get("first_aid"):
            choices.append(
                {
                    "id": f"heal:{index}",
                    "title": "处理自己的伤口",
                    "skill": "急救",
                    "kind": "heal",
                    "target": key,
                    "wound": index,
                    "heal_type": "first_aid",
                    "risk": "同一伤口只能尝试一次急救。",
                    "success": [],
                    "failure": [],
                    "source": "CoC7急救规则",
                }
            )
    return choices


def is_barred(state, profile):
    return bool(profile.get("barred") and not state["flags"].get(profile.get("barrier_flag", "")))


def choose_action(state, actor, plan):
    from companion.skills.base import SkillError
    from companion.trpg import rules

    if not isinstance(plan, dict) or set(plan) - {
        "action_id",
        "skill",
        "bonus",
        "penalty",
        "difficulty",
    }:
        raise SkillError("主持提案包含不允许的字段，未执行")
    action = next((a for a in actions_for(state, actor) if a["id"] == plan.get("action_id")), None)
    if action is None:
        raise SkillError("主持选择不在当前已采用行动范围内，未执行")
    skill = plan.get("skill", action.get("skill"))
    if skill and skill not in [action.get("skill"), *action.get("allowed_skills", [])]:
        raise SkillError("不能替换为模组未允许的技能")
    if skill:
        action["skill"] = skill
        action["bonus"] = rules.integer(plan.get("bonus", action.get("bonus", 0)), 0, 2, "奖励")
        # 隔着栏杆等固定惩罚不能被模型取消。
        action["penalty"] = max(
            action.get("penalty", 0), rules.integer(plan.get("penalty", 0), 0, 2, "惩罚")
        )
        difficulty = plan.get("difficulty", action.get("difficulty", "常规"))
        if difficulty not in {"常规", "困难", "极难"}:
            raise SkillError("主持难度格式不对")
        ranks = {"常规": 1, "困难": 2, "极难": 3}
        if ranks[difficulty] < ranks[action.get("difficulty", "常规")]:
            raise SkillError("不能低于来源明确要求的难度")
        action["difficulty"] = difficulty
    return action
