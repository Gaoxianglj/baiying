"""规则计算不调用模型；资料见 docs/trpg-system.md 的逐项来源。"""

from __future__ import annotations

from companion.skills.base import SkillError
from companion.skills.dice import Expression, percentile
from companion.skills.dice import grade as grade

RANK = {"大失败": -1, "失败": 0, "常规成功": 1, "困难成功": 2, "极难成功": 3, "大成功": 4}
BASE_SKILLS = {
    "锁匠": 1,
    "急救": 30,
    "斗殴": 25,
    "侦查": 25,
    "聆听": 20,
    "说服": 10,
    "话术": 5,
    "恐吓": 15,
    "潜行": 20,
    "攀爬": 20,
    "图书馆使用": 20,
    "手枪": 20,
    "医学": 1,
}


def integer(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SkillError(f"{name}必须是{low}～{high}的整数")
    return value


def can_act(card):
    if card["HP"] <= 0 or any(v in {"死亡", "昏迷"} for v in card["conditions"]):
        return False
    for condition in card["conditions"]:
        if "疯狂" in condition and not (
            card.get("bout_resolved") and condition.endswith("（本次发作结束）")
        ):
            return False
    return True


def character(data, *, npc=False):
    if not isinstance(data, dict):
        raise SkillError("角色卡必须是对象")
    allowed = {
        "STR",
        "CON",
        "SIZ",
        "DEX",
        "APP",
        "INT",
        "POW",
        "EDU",
        "HP",
        "HP_MAX",
        "MP",
        "MP_MAX",
        "SAN",
        "LUCK",
        "MYTHOS",
        "skills",
        "items",
        "conditions",
        "name",
    }
    if set(data) - allowed:
        raise SkillError("角色卡有不支持的字段")
    attrs = {
        key: integer(data.get(key, 50), 0 if npc and key == "POW" else 1, 999, key)
        for key in ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU")
    }
    max_hp = integer(data.get("HP_MAX", (attrs["CON"] + attrs["SIZ"]) // 10), 1, 1000, "HP上限")
    max_mp = integer(data.get("MP_MAX", attrs["POW"] // 5), 0, 1000, "MP上限")
    mythos = integer(data.get("MYTHOS", 0), 0, 99, "神话")
    skills = dict(BASE_SKILLS)
    skills["闪避"] = attrs["DEX"] // 2
    supplied = data.get("skills", {})
    if not isinstance(supplied, dict) or len(supplied) > 100:
        raise SkillError("技能列表过大或格式不对")
    for key, value in supplied.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 30:
            raise SkillError("技能名格式不对")
        skills[key] = integer(value, 0, 999, key)
    items = data.get("items", {})
    if not isinstance(items, dict) or len(items) > 100:
        raise SkillError("物品列表格式不对")
    for key, value in items.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 60:
            raise SkillError("物品名格式不对")
        integer(value, 0, 9999, key)
    conditions = data.get("conditions", [])
    if (
        not isinstance(conditions, list)
        or len(conditions) > 20
        or any(not isinstance(v, str) or len(v) > 40 for v in conditions)
    ):
        raise SkillError("角色状态格式不对")
    return {
        **attrs,
        "HP_MAX": max_hp,
        "HP": integer(data.get("HP", max_hp), 0, max_hp, "HP"),
        "MP_MAX": max_mp,
        "MP": integer(data.get("MP", max_mp), 0, max_mp, "MP"),
        "SAN": integer(data.get("SAN", min(attrs["POW"], 99 - mythos)), 0, 99 - mythos, "SAN"),
        "LUCK": integer(data.get("LUCK", 50), 0, 999, "幸运"),
        "MYTHOS": mythos,
        "skills": skills,
        "items": dict(items),
        "conditions": list(conditions),
        "growth": [],
        "version": 1,
        "san_day_start": data.get("SAN", min(attrs["POW"], 99 - mythos)),
        "san_loss_today": 0,
        "wounds": [],
        "healed_wounds": [],
    }


def skill_value(card, name):
    name = {
        "力量": "STR",
        "体质": "CON",
        "敏捷": "DEX",
        "智力": "INT",
        "意志": "POW",
        "理智": "SAN",
        "幸运": "LUCK",
        "教育": "EDU",
    }.get(name, name)
    if name in card and isinstance(card[name], int):
        return card[name]
    if name not in card["skills"]:
        raise SkillError("角色卡没有此技能，请先确认数值，不能让模型猜")
    return card["skills"][name]


def check(value, difficulty, bonus, penalty, rng):
    integer(value, 0, 999, "技能值")
    if difficulty not in ("常规", "困难", "极难"):
        raise SkillError("难度须为常规、困难或极难")
    integer(bonus, 0, 20, "奖励")
    integer(penalty, 0, 20, "惩罚")
    if abs(bonus - penalty) > 2:
        raise SkillError("奖惩抵消后最多2颗")
    result = percentile(
        {"skill": value, "difficulty": difficulty, "bonus": bonus, "penalty": penalty, "label": ""},
        rng,
    )
    if value == 0:
        result.update(level="大失败" if result["roll"] >= 96 else "失败", passed=False)
    return result


def loss(expression, rng, maximum=False):
    parsed = Expression(expression)
    low, _ = parsed._bounds(parsed.root)
    if low < 0:
        raise SkillError("损失表达式不能产生负数")
    return parsed.roll((lambda sides: sides - 1) if maximum else rng)


def sanity(card, success, failure, rng):
    # 全部先解析，失败不能消耗一半随机数后悄悄改式子。
    for expression in (success, failure):
        loss(expression, lambda _: 0)
    result = check(card["SAN"], "常规", 0, 0, rng)
    amount = loss(
        success if result["passed"] else failure, rng, maximum=result["level"] == "大失败"
    )
    actual = min(card["SAN"], amount["total"])
    card["SAN"] -= actual
    card["san_loss_today"] += actual
    notes = []
    if actual >= 5:
        intelligence = check(card["INT"], "常规", 0, 0, rng)
        result["INT_check"] = intelligence
        if intelligence["passed"]:
            notes.append("临时疯狂，发作症状由KP裁定")
    if card["san_day_start"] > 0 and card["san_loss_today"] * 5 >= card["san_day_start"]:
        notes.append("达到本叙事日SAN五分之一，不定性疯狂待KP处理")
    if card["SAN"] == 0:
        notes.append("SAN为0，永久疯狂，停止自动行动")
    for note in notes:
        if note not in card["conditions"]:
            card["conditions"].append(note)
    return {
        "check": result,
        "loss": amount,
        "actual_loss": actual,
        "SAN": card["SAN"],
        "notes": notes,
    }


def wound(card, amount, rng):
    integer(amount, 0, 10000, "伤害")
    before = card["HP"]
    card["HP"] = max(0, before - amount)
    notes, result = [], {"damage": amount, "before": before, "HP": card["HP"]}
    if amount:
        card["wounds"].append({"damage": amount, "healed": False})
    if amount >= card["HP_MAX"]:
        notes.append("死亡")
    elif amount * 2 >= card["HP_MAX"]:
        notes.append("重伤")
        con = check(card["CON"], "常规", 0, 0, rng)
        result["CON_check"] = con
        if not con["passed"] or card["HP"] == 0:
            notes.append("昏迷")
    if card["HP"] == 0 and "死亡" not in notes:
        notes.append("昏迷")
        if "重伤" in card["conditions"] or "重伤" in notes:
            notes.append("濒死，须急救及每轮CON检定")
    for note in notes:
        if note not in card["conditions"]:
            card["conditions"].append(note)
    return {**result, "conditions": notes}


def oppose(a, b, mode="普通"):
    ra, rb = RANK[a["level"]], RANK[b["level"]]
    if max(ra, rb) <= 0:
        return "无人成功"
    if ra == rb:
        if mode == "闪避":
            return "防守方"
        if mode == "反击":
            return "进攻方"
        if a["skill"] == b["skill"]:
            return "平局"
        return "进攻方" if a["skill"] > b["skill"] else "防守方"
    return "进攻方" if ra > rb else "防守方"


def damage(expression, bonus, extreme, impale, counterattack, rng):
    Expression(expression)
    Expression(bonus)
    weapon = loss(expression, rng, maximum=extreme and not counterattack)
    extra = loss(expression, rng)["total"] if extreme and impale and not counterattack else 0
    # 伤害加值可能是-1，不能用只接受非负的理智损失解析。
    db = Expression(bonus).roll((lambda sides: sides - 1) if extreme and not counterattack else rng)
    return {
        "weapon": weapon,
        "bonus": db,
        "extra": extra,
        "total": max(0, weapon["total"] + db["total"] + extra),
    }
