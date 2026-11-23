from __future__ import annotations

import copy
import json

from companion.skills.base import SkillError
from companion.trpg.rules import integer


def validate_scene(scene, state):
    if not isinstance(scene, dict) or len(json.dumps(scene)) > 60_000:
        raise SkillError("场景格式不对或过大")
    for key in ("id", "title", "public"):
        if not isinstance(scene.get(key), str) or not 1 <= len(scene[key]) <= (
            2400 if key == "public" else 80
        ):
            raise SkillError(f"场景{key}缺失或过长")
    actions = scene.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 40:
        raise SkillError("每场景最多40个行动")
    seen = set()
    for action in actions:
        if (
            not isinstance(action, dict)
            or not isinstance(action.get("id"), str)
            or len(action["id"]) > 60
        ):
            raise SkillError("行动缺少编号")
        if action["id"] in seen:
            raise SkillError("行动编号重复")
        seen.add(action["id"])
        if not isinstance(action.get("title"), str) or len(action["title"]) > 120:
            raise SkillError("行动须有简短标题")
        aliases = action.get("aliases", [])
        if (
            not isinstance(aliases, list)
            or len(aliases) > 12
            or any(not isinstance(a, str) or not 2 <= len(a) <= 60 for a in aliases)
        ):
            raise SkillError("行动别名须为2～60字，最多12个")
        conditions = action.get("requires", {})
        if not isinstance(conditions, dict) or len(conditions) > 20:
            raise SkillError("触发条件格式不对")
        for branch in ("success", "failure"):
            validate_effects(action.get(branch, []))
        for key in ("risk", "success_text", "failure_text"):
            if not isinstance(action.get(key, ""), str) or len(action.get(key, "")) > 800:
                raise SkillError("行动描述过长或格式不对")
    return copy.deepcopy({**scene, "actions": actions})


def validate_effects(effects):
    if not isinstance(effects, list) or len(effects) > 12:
        raise SkillError("每次最多12项状态变更")
    for effect in effects:
        if not isinstance(effect, dict) or effect.get("type") not in {
            "item",
            "flag",
            "scene",
            "reveal",
            "minutes",
            "resource",
        }:
            raise SkillError("不支持此状态变更类型")
        if effect["type"] in {"item", "flag", "scene", "reveal", "resource"}:
            if not isinstance(effect.get("name"), str) or not 1 <= len(effect["name"]) <= 80:
                raise SkillError("状态变更缺少名称")
        if effect["type"] in {"item", "minutes", "resource"}:
            integer(effect.get("delta"), 0 if effect["type"] == "minutes" else -999, 999, "变化值")
        if effect["type"] == "flag" and not isinstance(effect.get("value"), (str, int, bool)):
            raise SkillError("场景标志只能是标量")
        if effect["type"] == "resource" and effect["name"] not in {"MP", "LUCK"}:
            raise SkillError("HP/SAN必须走伤害或理智规则，不能用通用资源增减跳过检定")


def validate_module(draft, state):
    """只在本机校验资料草案；采纳是KP显式操作，不向模型发送原文。"""
    if not isinstance(draft, dict) or len(json.dumps(draft, ensure_ascii=False)) > 160_000:
        raise SkillError("模组草案须为不超过16万字符的JSON对象")
    scenes, facts = draft.get("scenes", []), draft.get("facts", [])
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 64:
        raise SkillError("草案须有1～64个场景")
    if not isinstance(facts, list) or len(facts) > 100:
        raise SkillError("草案最多100条事实")
    scenes = [validate_scene(s, state) for s in scenes]
    scene_ids = [s["id"] for s in scenes]
    fact_ids = []
    for fact in facts:
        if not isinstance(fact, dict):
            raise SkillError("事实格式错误")
        for key, cap in (("id", 60), ("text", 1600), ("source", 200)):
            if not isinstance(fact.get(key), str) or not 1 <= len(fact[key]) <= cap:
                raise SkillError("每条事实须含id、text和可核查source")
        fact_ids.append(fact["id"])
    if len(set(scene_ids)) != len(scene_ids) or len(set(fact_ids)) != len(fact_ids):
        raise SkillError("草案有重复编号")
    for scene in scenes:
        for action in scene["actions"]:
            for effect in action.get("success", []) + action.get("failure", []):
                if effect["type"] == "scene" and effect["name"] not in set(scene_ids) | set(
                    state["scenes"]
                ):
                    raise SkillError("草案引用了不存在的场景")
                if effect["type"] == "reveal" and effect["name"] not in set(fact_ids) | set(
                    state["facts"]
                ):
                    raise SkillError("草案引用了不存在的事实")
    return {
        "scenes": scenes,
        "facts": copy.deepcopy(facts),
        "notes": str(draft.get("notes", ""))[:2400],
    }


def available(state):
    scene = state["scenes"].get(state["scene"], {})
    return [
        a
        for a in scene.get("actions", [])
        if all(state["flags"].get(key) == value for key, value in a.get("requires", {}).items())
    ]


def apply(state, card, effects):
    validate_effects(effects)
    revealed = []
    for effect in effects:
        kind, name = effect["type"], effect.get("name")
        if kind == "item":
            total = card["items"].get(name, 0) + effect["delta"]
            integer(total, 0, 9999, "剩余物品")
            card["items"][name] = total
        elif kind == "resource":
            limit = card["MP_MAX"] if name == "MP" else 999
            card[name] = integer(card[name] + effect["delta"], 0, limit, "剩余资源")
        elif kind == "flag":
            state["flags"][name] = effect["value"]
        elif kind == "minutes":
            state["minutes"] += effect["delta"]
        elif kind == "scene":
            if name not in state["scenes"]:
                raise SkillError("行动引用了尚未配置的场景")
            state["scene"] = name
            visited = state["flags"].setdefault("visited", [])
            if name not in visited:
                visited.append(name)
        elif kind == "reveal":
            if name not in state["facts"]:
                raise SkillError("行动引用了尚未登记的事实")
            if name not in state["known"]:
                state["known"].append(name)
            revealed.append(state["facts"][name])
    return revealed
