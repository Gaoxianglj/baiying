"""按配置装配内置技能。"""

from __future__ import annotations

from pathlib import Path

from companion.config import Config
from companion.media import MediaStore
from companion.memory.events import EventLog
from companion.models.codex_app_server import CodexAppServerBackend, talk_only_overrides
from companion.models.gateway import ModelGateway
from companion.skills.access import FileAccess
from companion.skills.agent import CodexAgent, default_deny
from companion.skills.base import Skill
from companion.skills.files import SendFile, WriteFile
from companion.skills.group_agent import GroupAgent
from companion.skills.history import ChatHistory, ChatImages, ChatScope
from companion.skills.image import ImageGen, ImagePainter
from companion.skills.pictures import ImageTools
from companion.skills.reader import FindFile, ReadFile
from companion.skills.reminder import ReminderBook, Reminders
from companion.skills.search import WebSearch


def build_skills(
    config: Config,
    gateway: ModelGateway,
    reminders: ReminderBook,
    log: EventLog,
    store: MediaStore,
) -> list[Skill]:
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

    files = config.skills.files
    access = FileAccess(files, config.root, config.skills.agent.workspace, deny)
    scope = ChatScope(config.skills.history)
    views = MediaStore(  # 主人让她看的本机图片转成小图放这里，不进记忆文件夹
        config.data_dir / "run" / "view",
        max_side=config.vision.max_side,
        max_bytes=int(files.read_max_mb * 1024 * 1024),
        keep_originals=False,
    )
    images = ChatImages(log, store, views, access, scope)

    return [
        WebSearch(config.skills.search, config.models.fallback, gateway.ledger),
        ImageGen(
            config.skills.image,
            painter,
            config.data_dir / "run" / "image_quota.json",
            quota_ok=gateway.subscription_available,
            images=images,
            access=access,
        ),
        CodexAgent(
            config.skills.agent,
            config.root,
            sub,
            config.models.fallback,
            quota_ok=lambda: gateway.subscription is not None and gateway.subscription_available(),
            ledger=gateway.ledger,
        ),
        GroupAgent(
            config.skills.group_agent,
            config.root,
            sub,
            access,
            deny,
            config.data_dir / "run",
            quota_ok=lambda: gateway.subscription is not None and gateway.subscription_available(),
        ),
        SendFile(files, access),
        Reminders(config.skills.reminders, reminders),
        ReadFile(files, access),
        WriteFile(files, access),
        FindFile(files, access),
        ChatHistory(config.skills.history, log, scope),
        ImageTools(config.skills.history, files, images, access),
    ]
