"""给白璎使用的新团筹备操作：复用草案、关联资料、从文件绑定角色、核对可开场条件。"""

from __future__ import annotations

import hashlib
import json
import re

from companion.skills.base import SkillError
from companion.skills.reader import extract_text
from companion.trpg import rules
from companion.trpg.store import Store, dump

ATTRIBUTES = ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU")


def parse_card(text):
    """读取明确的JSON或人物卡Markdown数值；不推测未登记装备、伤势与配点。"""
    block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if block or text.lstrip().startswith("{"):
        value = json.loads(block[1] if block else text)
        if not isinstance(value, dict):
            raise SkillError("角色卡JSON须为对象")
        card = value.get("card", value)
        if not isinstance(card, dict) or not set(ATTRIBUTES) <= set(card):
            raise SkillError("角色卡JSON须明确提供完整八属性，不能用默认值补齐")
        return value.get("name", card.get("name", "调查员")), rules.character(card)
    card, skills, header, name = {}, {}, [], "调查员"
    heading = re.search(r"(?m)^#\s+([^\n]+)", text)
    if heading:
        name = re.split(r"[｜|]", heading[1])[0].strip()
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if not cells or re.fullmatch(r"[-: ]+", cells[0]):
            continue
        attr = next((key for key in ATTRIBUTES if re.search(rf"\b{key}\b", cells[0], re.I)), None)
        if attr and len(cells) > 1 and cells[1].isdigit():
            card[attr] = int(cells[1])
        elif "幸运" in cells[0] and len(cells) > 1 and cells[1].isdigit():
            card["LUCK"] = int(cells[1])
        elif cells[0] in {"技能", "技能名", "技能名称"}:
            header = cells
        elif header:
            column = next(
                (
                    i
                    for i, h in enumerate(header)
                    if h in {"最终", "最终值", "数值", "技能值", "常规"}
                ),
                None,
            )
            if column is not None and len(cells) > column and cells[column].isdigit():
                skill = {"格斗（斗殴）": "斗殴", "格斗(斗殴)": "斗殴"}.get(cells[0], cells[0])
                skills[skill] = int(cells[column])
    # 兼容白璎生成的逐行卡和同一行的多个属性；冲突不能静默选一个。
    for key in ATTRIBUTES:
        values = {
            int(m)
            for m in re.findall(
                rf"(?:^|[\s，,；;]){key}\s*[:：]?\s*(\d+)(?=$|[\s，,；;。])", text, re.M | re.I
            )
        }
        if key in card:
            values.add(card[key])
        if len(values) > 1:
            raise SkillError(f"角色卡{key}有冲突数值，请核对明确的最终值")
        if values:
            card[key] = values.pop()
    section = ""
    items = {}
    known_skills = set(rules.BASE_SKILLS) | {"机械维修", "电气维修", "心理学", "克苏鲁神话", "闪避"}
    for line in text.splitlines():
        clean = line.strip().lstrip("-• ").replace("**", "")
        if clean.startswith("#"):
            title = clean.lstrip("# ").strip()
            section = {
                "技能": "skills",
                "物品": "items",
                "当前物品": "items",
                "携带物品": "items",
                "随身物品": "items",
                "装备": "items",
                "随身装备": "items",
            }.get(title, "")
            continue
        if re.match(r"(?:候选|可选|建议|未配发|拟购|未获得|未携带)", clean):
            section = ""
            continue
        inline = re.match(
            r"^(技能|物品|当前物品|携带物品|随身物品|装备|随身装备)\s*[:：]\s*(.*)$", clean
        )
        current = section
        if inline:
            current = "skills" if inline[1] == "技能" else "items"
            clean = inline[2]
        if clean.startswith("|"):
            continue
        for part in re.split(r"[；;、，]", clean):
            part = part.strip().rstrip("。")
            if current == "items":
                match = re.fullmatch(r"(.{1,40}?)\s*(?:[×xX*：:]\s*|\s+)(\d+)", part)
                if not match:
                    match = re.fullmatch(r"([\u4e00-\u9fffA-Za-z（）()]{1,40}?)(\d+)", part)
                if match:
                    item = match[1].strip()
                    if item in items and items[item] != int(match[2]):
                        raise SkillError("角色卡物品数量冲突：" + item)
                    items[item] = int(match[2])
            else:
                match = re.fullmatch(
                    r"([\u4e00-\u9fffA-Za-z（）() ]{1,30}?)\s*[:：]?\s*(\d+)%?", part
                )
                if match and (current == "skills" or match[1].strip() in known_skills):
                    skill, value = match[1].strip(), int(match[2])
                    if skill in skills and skills[skill] != value:
                        raise SkillError("角色卡技能数值冲突：" + skill)
                    skills[skill] = value
    card["items"] = items
    lucky = {
        int(v)
        for v in re.findall(r"(?:^|[\s，,；;])(?:幸运|LUCK)\s*[:：]?\s*(\d+)", text, re.M | re.I)
    }
    if "LUCK" in card:
        lucky.add(card["LUCK"])
    if len(lucky) > 1:
        raise SkillError("角色卡幸运数值冲突")
    if lucky:
        card["LUCK"] = lucky.pop()
    missing = set(ATTRIBUTES) - set(card)
    if missing:
        raise SkillError(
            "卡文件尚未明确列出八属性："
            + "、".join(sorted(missing))
            + "。请读取表格坐标或正文后，向绑定接口提供核对后的card对象。"
        )
    card["skills"] = skills
    for key in ("HP", "MP"):
        match = re.search(rf"\b{key}\s*[:：]?\s*(\d+)\s*/\s*(\d+)", text)
        if match:
            card[key], card[key + "_MAX"] = int(match[1]), int(match[2])
        elif match := re.search(rf"\b{key}\s*[:：]\s*(\d+)(?!\d|/)", text):
            card[key] = int(match[1])
    sanity = re.search(r"\bSAN\s*[:：]?\s*(\d+)", text)
    if sanity:
        card["SAN"] = int(sanity[1])
    card["MYTHOS"] = skills.get("克苏鲁神话", 0)
    return name, rules.character(card)


class Setup:
    def __init__(self, controller):
        self.c, self.s = controller, controller.service

    def campaign(self, ctx, raw=None):
        if raw:
            with self.c.store.connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM campaigns WHERE body=? AND channel=? AND name=?",
                    (ctx.body, ctx.channel, str(raw)),
                ).fetchall()
            if len(rows) == 1:
                raw = rows[0]["id"]
            elif len(rows) > 1:
                raise SkillError("当前聊天有多个同名团，请从状态查询中选择确切编号")
        return self.s.campaign(ctx, raw)

    def run(self, ctx, args):
        if not isinstance(args, dict):
            raise SkillError("筹备参数须为对象")
        action = args.get("action", "检查")
        if action == "准备":
            return self.prepare(ctx, args)
        campaign = self.campaign(ctx, args.get("campaign"))
        self.s.kp(campaign, ctx)
        if action == "检查":
            return self.check(ctx, campaign["id"])
        if campaign["status"] == "active":
            raise SkillError("筹备前先暂停当前团，已有进度会保存")
        if action == "绑定角色":
            return self.bind(ctx, campaign, args)
        if action == "关联资料":
            return self.attach(ctx, campaign, args)
        raise SkillError("可用筹备操作：准备、绑定角色、关联资料、检查")

    def key(self, ctx, suffix):
        return "setup:" + ctx.request_id + ":" + suffix if ctx.request_id else None

    def file(self, ctx, raw):
        if not isinstance(raw, str) or not raw.strip():
            raise SkillError("请提供实际来源文件路径，先用找文件定位")
        path, member = self.c.access.locate(raw, ctx)
        if member:
            raise SkillError("压缩包内资料先用转存文件保存为MD，再关联或绑定")
        if not path.is_file():
            raise SkillError("来源路径未找到，先用找文件定位：" + raw)
        if path.stat().st_size > 25 * 1024 * 1024:
            raise SkillError("来源文件超过25MB，请先分段转存")
        return path

    def attach(self, ctx, campaign, args):
        role = args.get("role", "rules")
        if role not in {"rules", "template", "module"}:
            raise SkillError("role须为rules、template或module")
        path = self.file(ctx, args.get("path"))
        visibility = "kp" if role == "module" else "public"
        indexed = self.c.library.import_file(ctx, campaign["id"], str(path), visibility)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.c.store.connect(write=True) as conn:
            fresh = self.s.campaign(ctx, campaign["id"], conn=conn)
            assets = fresh["state"].setdefault("assets", [])
            if not any(a["sha"] == sha and a["role"] == role for a in assets):
                assets.append(
                    {
                        "role": role,
                        "name": path.name,
                        "path": str(path),
                        "sha": sha,
                        "visibility": visibility,
                    }
                )
                Store.save(conn, fresh)
        return {**indexed, "text": f"{role}资料已关联：{path.name}；已有版本会复用。"}

    def bind(self, ctx, campaign, args):
        who = str(args.get("person", ctx.person_id)).strip().lstrip("@")
        if who in {
            "self",
            "me",
            "requester",
            "current_user",
            "我",
            "本人",
            "当前用户",
            "当前请求者",
            "当前发言人",
        }:
            who = ctx.person_id
        if who.isdigit() and ctx.body == "qq":
            if self.c.person_resolver is None:
                raise SkillError("当前入口未连接QQ身份解析器")
            who = self.c.person_resolver(who)
        elif who not in campaign["state"]["members"]:
            raise SkillError("请使用QQ号或已经登记的固定人物ID，昵称不作为绑定身份")
        path = self.file(ctx, args.get("path")) if args.get("path") else None
        if path:
            self.c.access.check_read(path, ctx)
        if path and path.suffix.lower() != ".xlsx":
            name, card = parse_card(extract_text(path.name, path.read_bytes(), 25 * 1024 * 1024))
        elif isinstance(args.get("card"), dict):
            if not set(ATTRIBUTES) <= set(args["card"]):
                raise SkillError("绑定前需要核对完整八属性，不能用默认值补未读到的数值")
            name = args.get("name") or args["card"].get("name") or "调查员"
            card = rules.character(args["card"])
        elif path:
            name, card = parse_card(extract_text(path.name, path.read_bytes(), 25 * 1024 * 1024))
        else:
            raise SkillError("需要卡文件path或核对后的card对象")
        # rules.character返回运行期字段；提交时仅传输入角色卡允许的字段。
        fields = {
            *ATTRIBUTES,
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
        }
        supplied = {k: v for k, v in card.items() if k in fields}
        member = campaign["state"]["members"].get(who, {})
        old = campaign["state"]["characters"].get(member.get("character"))
        if old:
            if old["name"] == name and all(old.get(k) == v for k, v in supplied.items()):
                return {
                    "text": f"{name}已绑定该玩家，复用当前角色卡。",
                    "campaign": campaign["id"],
                    "changed": False,
                    "visibility": "public",
                }
            raise SkillError("该玩家已有另一张卡；需要明确替换理由后使用改卡，不覆盖现有角色")
        if who not in campaign["state"]["members"]:
            self.s.execute(
                ctx,
                "成员",
                {"campaign": campaign["id"], "person": who, "role": "player"},
                request_id=self.key(ctx, "member:" + who),
            )
        return self.s.execute(
            ctx,
            "建卡",
            {"campaign": campaign["id"], "person": who, "name": name, "card": supplied},
            request_id=self.key(ctx, "card:" + who),
        )

    def prepare(self, ctx, args):
        if args.get("campaign"):
            campaign = self.campaign(ctx, args["campaign"])
        else:
            name = str(args.get("name", "")).strip()
            if not name or len(name) > 80:
                raise SkillError("请给新团一个简短名字")
            with self.c.store.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM campaigns WHERE body=? AND channel=? AND name=? "
                    "AND status IN ('draft','active') ORDER BY rowid DESC LIMIT 1",
                    (ctx.body, ctx.channel, name),
                ).fetchone()
            if row:
                campaign = Store.load(row)
            else:
                result = self.s.execute(
                    ctx, "创建", {"name": name}, request_id=self.key(ctx, "create:" + name)
                )
                campaign = self.s.campaign(ctx, result["campaign"])
        self.s.kp(campaign, ctx)
        if campaign["status"] == "active":
            return self.check(ctx, campaign["id"])
        for role in ("rules", "template", "module"):
            if args.get(role):
                self.attach(ctx, campaign, {"role": role, "path": args[role]})
        for character in args.get("characters", []):
            if not isinstance(character, dict):
                raise SkillError("characters须为角色绑定对象列表")
            self.bind(ctx, self.s.campaign(ctx, campaign["id"]), character)
        scenes = args.get("scenes", [])
        if (
            not isinstance(scenes, list)
            or len(scenes) > 64
            or any(not isinstance(s, dict) for s in scenes)
        ):
            raise SkillError("scenes须为最多64个已核对场景对象的列表")
        if args.get("scene") is not None and not isinstance(args["scene"], dict):
            raise SkillError("scene须为已核对的开场场景对象")
        if isinstance(args.get("scene"), dict):
            scenes = [*scenes, args["scene"]]
        prepared = []
        for source in scenes:
            scene = dict(source)
            scene.setdefault("public", scene.get("description", ""))
            if not isinstance(scene["public"], str) or not scene["public"].strip():
                raise SkillError("还缺已确认的公开开场描述，请先读取开场资料再准备场景")
            scene.setdefault("title", scene.get("name") or "开场")
            scene.setdefault(
                "id", "opening-" + hashlib.sha256(scene["public"].encode()).hexdigest()[:12]
            )
            self.s.execute(
                ctx,
                "场景",
                {"campaign": campaign["id"], "scene": scene},
                request_id=self.key(ctx, "scene:" + campaign["id"] + ":" + scene["id"]),
            )
            prepared.append(scene["id"])
        opening = args.get("start_scene") or (prepared[-1] if args.get("scene") else None)
        if opening is not None and not isinstance(opening, str):
            raise SkillError("start_scene须为已登记的开场编号")
        if opening:
            current = self.s.campaign(ctx, campaign["id"])
            if current["status"] != "draft":
                if current["state"]["scene"] != opening:
                    raise SkillError(
                        "已有进度的团不通过筹备工具重置位置，请按实际行动或明确转场处理"
                    )
            elif current["state"]["scene"] != opening:
                self.s.execute(
                    ctx,
                    "转场",
                    {"campaign": campaign["id"], "scene": opening},
                    request_id=self.key(ctx, "opening:" + campaign["id"]),
                )
        current = self.s.campaign(ctx, campaign["id"])
        if ctx.owner and args.get("host", True) and not self.c.host.enabled(current):
            self.s.execute(
                ctx,
                "公开主持",
                {"campaign": campaign["id"]},
                request_id=self.key(ctx, "host:" + campaign["id"]),
            )
        report = self.check(ctx, campaign["id"])
        if args.get("start") is True:
            data = report["data"]
            if data["can_start"]:
                return self.s.execute(
                    ctx,
                    "开团",
                    {"campaign": campaign["id"]},
                    request_id=self.key(ctx, "start:" + campaign["id"]),
                )
            report["text"] += "\n尚未开场；请先由白璎完成报告中的缺项，不要求玩家操作系统。"
        return report

    def check(self, ctx, cid):
        campaign = self.s.campaign(ctx, cid)
        self.s.kp(campaign, ctx)
        state = campaign["state"]
        missing = []
        if not any(m.get("character") for m in state["members"].values()):
            missing.append("角色未绑定：使用绑定角色操作，核对QQ号和卡文件")
        if not state.get("scene"):
            missing.append("开场场景未准备：根据已确认资料使用场景或采纳模组操作")
        with self.c.store.connect() as conn:
            active = conn.execute(
                "SELECT id FROM campaigns WHERE body=? AND channel=? AND status='active'",
                (ctx.body, ctx.channel),
            ).fetchone()
        if active and active[0] != cid:
            missing.append("当前聊天另一个团仍在进行，先由有权限的请求人决定是否暂停切换")
        data = {
            "campaign": cid,
            "name": campaign["name"],
            "status": campaign["status"],
            "assets": [
                {k: a[k] for k in ("name", "role", "visibility")} for a in state.get("assets", [])
            ],
            "missing": missing,
            "scene_count": len(state["scenes"]),
            "action_count": sum(len(s.get("actions", [])) for s in state["scenes"].values()),
            "current_scene": state["scene"],
            "bindings": [
                {
                    "person": person,
                    "character": m.get("character"),
                    "name": state["characters"].get(m.get("character"), {}).get("name"),
                }
                for person, m in state["members"].items()
            ],
            "ai_host": self.c.host.enabled(campaign),
            "can_start": not missing,
        }
        return {
            "text": dump(data),
            "data": data,
            "campaign": cid,
            "changed": False,
            "visibility": "kp",
        }
