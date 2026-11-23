from __future__ import annotations

import json

from companion.skills.base import Skill, SkillError, SkillResult


class Trpg(Skill):
    name, label = "trpg", "跑团系统"
    markers = {
        "团务": "【团务：操作 JSON】查询/管理当前团、角色、正式检定。状态/角色/检定列表/额度无需模型代算。"
        "不能直接改骰点；旁观者不能推进剧情；KP秘密只在私聊。",
        "查团规": "【查团规：关键词】只从本团当前权限可见的已索引资料取相关片段，返回源页与版本，不整本读取。",
    }

    def __init__(self, controller):
        self.controller = controller
        self.markers = {
            **self.markers,
            "筹备团": (
                "【筹备团：JSON】白璎直接筹备团。action=准备时可传name、rules规则文件、template表格、"
                'module模组文件、characters=[{"person":"QQ号","path":"卡文件"}]、scene=已确认开场场景对象、start=true。'
                "person省略或填self即当前发言人，其他玩家用QQ号。"
                "角色卡可用带八属性的Markdown表格、逐行数值或JSON；也可直接传card对象，"
                "其中须有STR/CON/SIZ/DEX/APP/INT/POW/EDU及已核对的skills、items，不能猜数值。"
                'scene示例={"id":"opening","title":"开场","public":"资料确认的公开描写","actions":[]}。'
                "多个场景可传scenes=[场景对象,...]及start_scene=开场编号；草案会选定开场，已有进度不重置。"
                "检查返回绑定、总场景数、总行动数与当前位置，场景列表只显示已到达场景，不代表其他场景未保存。"
                "可传campaign编号或本聊天唯一团名复用草案。action=绑定角色时传campaign、person、path；"
                "action=关联资料时传campaign、path、role=rules/template/module；"
                "action=检查时传campaign。操作由本团KP执行，身份按真实QQ解析，旧团原档保留。"
            ),
        }

    def enabled(self):
        return self.controller.config.trpg.enabled

    def run(self, marker, arg, ctx):
        if not self.enabled():
            raise SkillError("跑团功能已关闭")
        if marker == "筹备团":
            try:
                args = json.loads(arg)
            except ValueError as error:
                raise SkillError("筹备参数需要合法JSON对象") from error
            result = self.controller.setup.run(ctx, args)
            return SkillResult(result["text"], max_chars=12000)
        line = ("检索 " + arg) if marker == "查团规" else arg
        _, args = self.controller.parse(line)
        if args.get("private"):
            raise SkillError("KP全文只走专用私聊指令或控制台，不放进普通聊天模型上下文")
        result = self.controller.command(ctx, line)
        return SkillResult(result["text"], files=result.get("files", []), max_chars=16000)

    def context(self, ctx):
        if self.controller.store.path.exists():
            try:
                current = self.controller.service.campaign(ctx)
            except SkillError:
                current = None
            if current and self.controller.host.enabled(current):
                return (
                    f"白璎是本团{current['id']}的AI主持，当前{current['status']}。"
                    "玩家说继续跑团、确认继续、暂停、查看状态由程序专用主持通道处理。"
                    "普通聊天工具仍以当前用户身份执行，不冒充KP；KP原文不进入普通聊天。"
                )
        campaign = self.controller.active(ctx)
        if not campaign:
            joined = []
            if ctx.private and self.controller.store.path.exists():
                for row in self.controller.store.listing():
                    try:
                        own = self.controller.service.campaign(ctx, row["id"])
                        joined.append(
                            {"id": own["id"], "name": own["name"], "status": own["status"]}
                        )
                    except SkillError:
                        pass
            return "正式团状态以程序记录为准。你可查询此人参与的团：" + str(joined[:5])
        return (
            f"本聊天有活跃团{campaign['id']}，当前状态版本{campaign['version']}。"
            "只有登记玩家能行动；先说目标/成功率/失败风险，禁止用猜测改卡、重投或泄露KP资料。"
        )

    def status(self):
        return "独立存档、程序裁定、角色权限、按页资料及模型预算"
