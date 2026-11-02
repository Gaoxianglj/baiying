"""按配置装配内置技能。"""

from __future__ import annotations

from companion.config import Config
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.gateway import ModelGateway
from companion.skills.agent import CodexAgent
from companion.skills.base import Skill
from companion.skills.files import SendFile
from companion.skills.image import ImageGen, ImagePainter
from companion.skills.reminder import ReminderBook, Reminders
from companion.skills.search import WebSearch


def build_skills(config: Config, gateway: ModelGateway, reminders: ReminderBook) -> list[Skill]:
    sub = config.models.subscription
    painter = None
    if gateway.subscription is not None:

        def painter() -> ImagePainter:  # 第一次画图时才启动单独的 Codex 进程
            return CodexAppServerBackend.from_codex(
                sub.codex_command,
                config.data_dir / "codex-image",
                model=sub.model,
                effort="low",
                timeout=config.skills.image.timeout_seconds,
            )

    return [
        WebSearch(config.skills.search, config.models.fallback, gateway.ledger),
        ImageGen(
            config.skills.image,
            painter,
            config.data_dir / "run" / "image_quota.json",
            quota_ok=gateway.subscription_available,
        ),
        CodexAgent(
            config.skills.agent,
            config.root,
            sub,
            config.models.fallback,
            quota_ok=lambda: gateway.subscription is not None and gateway.subscription_available(),
            ledger=gateway.ledger,
        ),
        SendFile(config.skills.files, config.root, config.skills.agent.workspace),
        Reminders(config.skills.reminders, reminders),
    ]
