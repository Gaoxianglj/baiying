"""按配置装配内置技能。"""

from __future__ import annotations

from pathlib import Path

from companion.config import Config
from companion.models.codex_app_server import CodexAppServerBackend, talk_only_overrides
from companion.models.gateway import ModelGateway
from companion.skills.agent import CodexAgent, default_deny
from companion.skills.base import Skill
from companion.skills.files import SendFile
from companion.skills.image import ImageGen, ImagePainter
from companion.skills.reader import ReadFile
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
                overrides=talk_only_overrides(image=True),
                model=sub.model,
                effort="low",
                timeout=config.skills.image.timeout_seconds,
            )

    def deny() -> list[Path]:  # 读文件的黑名单和助手的一样，每次现算（配置和路径都可能变）
        extra = [Path(p) for p in config.skills.agent.extra_deny]
        extra = [p if p.is_absolute() else config.root / p for p in extra]
        return default_deny(config.root) + extra

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
        ReadFile(config.skills.files, config.root, config.skills.agent.workspace, deny),
    ]
