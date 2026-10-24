"""按配置装配内置技能。"""

from __future__ import annotations

from companion.config import Config
from companion.models.gateway import ModelGateway
from companion.skills.base import Skill
from companion.skills.search import WebSearch


def build_skills(config: Config, gateway: ModelGateway) -> list[Skill]:
    return [WebSearch(config.skills.search, config.models.fallback, gateway.ledger)]
