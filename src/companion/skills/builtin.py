"""按配置装配内置技能。"""

from __future__ import annotations

from companion.config import Config
from companion.models.gateway import ModelGateway
from companion.skills.base import Skill


def build_skills(config: Config, gateway: ModelGateway) -> list[Skill]:
    return []
