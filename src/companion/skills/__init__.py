"""技能：联网搜索、生图、Agent 等。

统一流程：她在回复里写【标记：参数】→ 程序执行 → 结果交回给她 → 她接着回复。
"""

from companion.skills.base import (
    Skill,
    SkillCall,
    SkillContext,
    SkillError,
    SkillResult,
    Skills,
)

__all__ = ["Skill", "SkillCall", "SkillContext", "SkillError", "SkillResult", "Skills"]
