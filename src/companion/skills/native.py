"""现有技能的原生动态工具适配：仍从当前 SkillContext 检查可用性和参数。"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from collections.abc import Callable
from dataclasses import replace

from companion.models.openai_compat import ChatMessage
from companion.models.runtime import ToolSet
from companion.skills.base import SkillCall, SkillContext, SkillError, SkillResult, Skills


class NativeSkills:
    def __init__(
        self, skills: Skills, ctx: SkillContext,
        on_result: Callable[[SkillCall, SkillResult], None],
    ) -> None:
        self.skills, self.ctx, self.on_result = skills, ctx, on_result
        self._markers: dict[str, str] = {}
        self.history: list[ChatMessage] = []
        self._done: dict[str, tuple[str, dict]] = {}
        specs = []
        # 原生 rollout 会保留工具结果；临时桌面资料继续沿用原来的临时会话标记路径。
        for skill in skills.available(ctx):
            if skill.name in {"screen", "clipboard"}:
                continue
            for marker, usage in skill.markers_for(ctx).items():
                name = "baiying_" + hashlib.sha256(marker.encode()).hexdigest()[:12]
                self._markers[name] = marker  # 与文本技能一样，后注册的场合专属技能优先。
                spec = {"type": "function", "name": name,
                        "description": f"{skill.label} / {marker}。{usage}",
                        "inputSchema": {"type": "object", "properties": {
                            "arg": {"type": "string", "description": "参数或路径；无参数时填空字符串"},
                            "content": {"type": "string", "description": "写文件的正文；其他工具填空字符串"},
                        }, "required": ["arg", "content"], "additionalProperties": False}}
                specs = [s for s in specs if s["name"] != name]
                specs.append(spec)
        self.tools = ToolSet(specs, self.call, self.history)

    def call(self, name: str, args: dict, call_id: str) -> dict:
        def failed(message: str) -> dict:
            return {"success": False, "contentItems": [{"type": "inputText", "text": message}]}

        try:
            if not isinstance(args, dict) or set(args) - {"arg", "content"}:
                raise SkillError("工具参数必须是 arg、content 两个文本字段")
            if not isinstance(args.get("arg"), str) or not isinstance(args.get("content", ""), str):
                raise SkillError("工具参数类型不对")
            marker = self._markers.get(name)
            choices = {m: s for s in self.skills.available(self.ctx)
                       if s.name not in {"screen", "clipboard"}
                       for m in s.markers_for(self.ctx)}
            skill = choices.get(marker)
            if skill is None:
                raise SkillError("当前聊天不能使用这个工具")
            if self.ctx.stop_event and self.ctx.stop_event.is_set():
                raise SkillError("任务已停止，不能继续执行工具")
            arg, content = args["arg"], args.get("content", "")
            if len(arg) + len(content) > 2_000_000:
                raise SkillError("工具参数过长")
            if content:
                if not skill.takes_block:
                    raise SkillError("这个工具不接受正文 content")
                arg += "｜" + content
            fingerprint = hashlib.sha256((name + "\0" + arg).encode()).hexdigest()
            if call_id in self._done:
                previous, result = self._done[call_id]
                if previous != fingerprint:
                    raise SkillError("重复工具编号的参数发生变化，拒绝执行")
                return result
            run_ctx = (replace(self.ctx, request_id="native:" + call_id)
                       if skill.name in {"dice", "trpg"} else self.ctx)
            call, outcome = self.skills.run(skill, marker, arg, run_ctx)
            self.on_result(call, outcome)
            feedback = self.skills.feedback(skill, marker, outcome)
            self.history += [ChatMessage("assistant", f"【{marker}：{arg}】"),
                             ChatMessage("skill", feedback, images=tuple(outcome.view))]
            items = [{"type": "inputText", "text": feedback}]
            for path in outcome.view:
                mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
                items.append({"type": "inputImage", "imageUrl":
                              f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()})
            result = {"success": outcome.ok, "contentItems": items}
            self._done[call_id] = fingerprint, result
            return result
        except SkillError as e:
            return failed(str(e))
