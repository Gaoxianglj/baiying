from __future__ import annotations

import json
import sqlite3

from companion.skills.access import FileAccess
from companion.skills.agent import default_deny
from companion.skills.base import SkillError
from companion.trpg.documents import Library
from companion.trpg.host import Host
from companion.trpg.narrator import Narrator
from companion.trpg.scenes import available
from companion.trpg.service import Service
from companion.trpg.setup import Setup
from companion.trpg.store import Store

HELP = """跑团模式
/团 创建 团名（主人操作）
/团 成员 QQ号 player/kp/observer（本团KP操作）
/团 建卡 角色名｜角色卡数据（也可在控制台录入）
/团 状态、/团 角色、/团 检定列表、/团 额度：程序直接查询，不调用模型
/团 开团、暂停、续团、结束：实时保存；离线和睡眠不推进剧情
/团 授权 开/关：本人的代骰授权
/团 投骰 编号：确认已经宣告的目标和风险，再结算
/团 应对 编号 闪避/反击/不应对
/团 取消检定 编号；/团 行动 行动编号
/行动 描述：玩家行动（已开团的登记玩家也可直接说话）
团外：内容 —— 作为日常聊天，不推进剧情
/团 战报、/团 导出：当前团公开记录
KP准备场景、NPC、检定、资料导入、改卡等可在控制台完成；高级指令支持“操作 JSON”。
查看别的团须显式给campaign编号并持有本团成员权限；KP全文只在私聊或本机控制台查看。
白璎内部筹备：优先用【筹备团】批量登记scenes、start_scene与角色，检查返回总数和绑定。
高级团务操作（参数JSON）：
场景 {"scene":{"id":"编号","title":"标题","public":"公开描写","actions":[]}}；
转场 {"scene":"已登记编号"}（KP按明确要求操作）；开团 {}；角色 {}；状态 {}。
场景列表只列当前/已到达场景，这是公开视图；筹备检查的scene_count/action_count才是登记总数。
公开资料可用导入资料 {"path":"文件","visibility":"public"}，同一路径不能同时作为公开规则和KP模组。
模组草案/采纳模组仅在暂停后的KP私聊或本地使用；公开场景可由筹备团或场景工具登记，不必为此申请私有授权。"""


class Controller:
    def __init__(self, config, *, gateway_factory=None, rng=None):
        self.config = config
        self.store = Store(config.data_dir / "trpg/state.sqlite")
        self.service = Service(self.store, **({"rng": rng} if rng else {}))
        self.access = FileAccess(
            config.skills.files,
            config.root,
            config.skills.agent.workspace,
            lambda: default_deny(config.root) + [config.data_dir / "trpg"],
            config.data_dir / "run/trpg-protected.json",
        )
        self.library = Library(self.service, self.access, config.data_dir)
        self.narrator = Narrator(self.service, config, gateway_factory)
        self.host = Host(self, gateway_factory)
        self.person_resolver = None
        self.setup = Setup(self)

    def close(self):
        self.host.close()
        self.narrator.close()

    def active(self, ctx):
        return self.service.active(ctx) if self.config.trpg.enabled else None

    def parse(self, line):
        if line.strip().startswith("{"):
            try:
                args = json.loads(line)
            except ValueError as error:
                raise SkillError("高级参数不是合法对象") from error
            if not isinstance(args, dict):
                raise SkillError("参数须为对象")
            fields = [key for key in ("operation", "操作") if key in args]
            if not fields and "action" in args:
                fields = ["action"]
            if len(fields) != 1 or not isinstance(args[fields[0]], str):
                raise SkillError("JSON须含唯一的操作名称：operation、操作或action")
            operation = args.pop(fields[0])
            operation = {
                "status": "状态",
                "help": "帮助",
                "pause": "暂停",
                "resume": "续团",
                "inventory": "角色",
            }.get(operation, operation)
            return operation, args
        parts = line.strip().split(maxsplit=1)
        op, tail = (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("帮助", "")
        if tail.startswith("{"):
            try:
                args = json.loads(tail)
            except ValueError as error:
                raise SkillError("高级参数不是合法对象") from error
            if not isinstance(args, dict):
                raise SkillError("参数须为对象")
        elif op == "创建":
            args = {"name": tail}
        elif op == "成员":
            who, _, role = tail.partition(" ")
            args = {"person": who, "role": role.strip() or "player"}
        elif op == "建卡":
            name, _, card = tail.replace("|", "｜", 1).partition("｜")
            try:
                args = {"name": name, "card": json.loads(card) if card else {}}
            except ValueError as error:
                raise SkillError("角色卡数据格式不对，请用控制台录入或导入角色卡") from error
        elif op in {"投骰", "取消检定", "孤注", "花幸运", "应对", "接受结果"}:
            key, _, value = tail.partition(" ")
            args = {"id": key}
            if op == "应对":
                args["mode"] = value
            elif op == "花幸运":
                try:
                    args["amount"] = int(value)
                except ValueError as error:
                    raise SkillError("格式：花幸运 检定编号 花费数值") from error
            elif op == "孤注":
                args["risk"] = value
        elif op == "行动":
            args = {"action": tail}
        elif op == "授权":
            if tail not in {"开", "关"}:
                raise SkillError("格式：/团 授权 开 或 关")
            args = {"auto_roll": tail == "开"}
        elif op == "检索":
            args = {"query": tail}
        elif tail:
            raise SkillError("此高级操作请在控制台完成，或用 操作 JSON；帮助：/团 帮助")
        else:
            args = {}
        return op, args

    def command(self, ctx, line, *, resolve_person=None):
        if not self.config.trpg.enabled or not self.config.skills.enabled:
            raise SkillError("跑团功能或技能总开关已关闭")
        if len(line) > 150_000:
            raise SkillError("指令过长")
        operation, args = self.parse(line)
        resolve_person = resolve_person or self.person_resolver
        setup_ops = {
            "准备新团": "准备",
            "绑定角色": "绑定角色",
            "关联资料": "关联资料",
            "筹备检查": "检查",
        }
        if operation in setup_ops:
            return self.setup.run(ctx, {**args, "action": setup_ops[operation]})
        if operation == "帮助":
            return {"text": HELP, "visibility": "public", "changed": False}
        if (
            operation in {"续团", "暂停", "确认恢复"}
            and not args.get("campaign")
            and self.store.path.exists()
        ):
            campaign = self.service.campaign(ctx)
            if self.host.enabled(campaign) and self.service.role(campaign, ctx) == "player":
                return self.host.handle(
                    ctx, {"续团": "继续跑团", "暂停": "暂停跑团", "确认恢复": "确认继续"}[operation]
                )
        if operation == "成员" and resolve_person:
            # 只接受数字账号或现有固定人物ID，不把昵称当授权依据。
            who = args.get("person", "")
            if isinstance(who, str) and who.isascii() and who.isdigit():
                args["person"] = resolve_person(who)
        if operation in {"导入资料", "检索"}:
            campaign = self.service.campaign(ctx, args.get("campaign"))
            if operation == "导入资料":
                return self.library.import_file(
                    ctx, campaign["id"], args.get("path", ""), args.get("visibility", "kp")
                )
            return self.library.search(
                ctx, campaign["id"], args.get("query", ""), private=bool(args.get("private"))
            )
        if operation == "导出":
            response = self.service.execute(ctx, "战报", args)
            campaign = self.service.campaign(ctx, args.get("campaign"))
            if args.get("private"):
                raise SkillError("KP全文请在本机控制台导出，避免通过普通附件路径公开")
            lines = [f"# {campaign['name']} 公开战报", ""]
            lines.extend(f"- {row['created']} {row['text']}" for row in response["data"])
            path = self.access.save_new(
                f"跑团战报-{campaign['id']}-{campaign['version']}.md",
                "\n".join(lines).encode(),
                ctx,
            )
            return {
                "text": "公开战报已导出。",
                "files": [path],
                "visibility": "public",
                "changed": False,
            }
        try:
            result = self.service.execute(ctx, operation, args)
        except sqlite3.IntegrityError as error:
            raise SkillError("团状态冲突，已有记录保留，请重新查询") from error
        # 风险宣告不多调用模型；数值结算后才按需要描写。
        if (
            result.get("changed")
            and not result.get("replay")
            and operation in {"投骰", "行动", "接受结果", "花幸运"}
        ):
            campaign = self.service.campaign(ctx, result.get("campaign"))
            if self.host.enabled(campaign):
                result = self.host.after_result(ctx, result, line[:700])
                self.host._outbox(result)
                return result
            if "尚未掷骰" not in result["text"] and "失败后果尚未提交" not in result["text"]:
                result = self.decorate(ctx, result, line[:700])
        return result

    def act(self, ctx, description):
        campaign = self.service.campaign(ctx)
        if self.host.enabled(campaign):
            return self.host.handle(ctx, description)
        if campaign["status"] != "active":
            return {
                "text": "团已暂停，进度保持；请KP续团后再行动。",
                "changed": False,
                "visibility": "public",
            }
        if self.service.role(campaign, ctx) not in {"player", "kp"}:
            raise SkillError("旁观者不能替玩家行动")
        self.service.pc(campaign, ctx.person_id)
        options = available(campaign["state"])
        normalized = "".join(description.split())
        matches = [
            a
            for a in options
            if a["id"] == normalized or any(alias in normalized for alias in a.get("aliases", []))
        ]
        if len(matches) == 1:
            return self.command(ctx, "行动 " + matches[0]["id"])
        # 未覆盖的行动仅提出澄清，不让自由文本模型编造新的世界状态。
        message = (
            "有多个可能的行动，请明确选哪一个。"
            if matches
            else "此行动尚无已确认裁定；请KP补充目标、风险或场景行动。没有掷骰，也没有改变状态。"
        )
        narration = self.narrator.choose(ctx, campaign, description, options) if options else ""
        return {
            "text": "\n\n".join(p for p in (narration, message) if p),
            "campaign": campaign["id"],
            "changed": False,
            "visibility": "public",
        }

    def decorate(self, ctx, result, action):
        narration = self.narrator.narrate(ctx, result["campaign"], action, result["text"])
        if narration:
            result = {**result, "text": narration + "\n\n" + result["text"]}
        if result.get("outbox"):
            with self.store.connect(write=True) as conn:
                conn.execute(
                    "UPDATE outbox SET text=? WHERE id=? AND state='pending'",
                    (result["text"], result["outbox"]),
                )
        return result

    def prepare_delivery(self, key):
        if not key:
            return True, None
        with self.store.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM outbox WHERE id=?", (key,)).fetchone()
            if row is None or row["state"] != "pending":
                return False, None
            conn.execute("UPDATE outbox SET state='sending' WHERE id=?", (key,))
            return True, row["text"]

    def delivery(self, key, success):
        if key:
            with self.store.connect(write=True) as conn:
                conn.execute(
                    "UPDATE outbox SET state=? WHERE id=?", ("sent" if success else "unknown", key)
                )

    def recover_delivery(self):
        if self.store.path.exists():
            with self.store.connect(write=True) as conn:
                conn.execute("UPDATE outbox SET state='unknown' WHERE state='sending'")
