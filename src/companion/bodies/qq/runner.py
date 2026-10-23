"""`companion qq`：连接 NapCat，让白璎在 QQ 上活动。"""

from __future__ import annotations

import asyncio
import logging

from companion.bodies.qq.bot import QQBot
from companion.config import Config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper
from companion.models.openai_compat import ModelError

log = logging.getLogger(__name__)


def run_qq(config: Config) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s：%(message)s",
        datefmt="%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config.qq
    if cfg.access_token is None:
        log.warning("没有设置 NAPCAT_TOKEN：如果 NapCat 的 WebSocket 服务设了 token，会连接失败")
    if not cfg.groups:
        log.info("没有配置 QQ_GROUPS：只回复私聊，不进任何群")
    if not cfg.owner_accounts:
        log.warning("没有配置 QQ_OWNER_ACCOUNTS：跨平台绑定无法确认，/状态 不可用")

    with Companion(config) as companion:
        sleeper = Sleeper(companion)

        def daily() -> None:
            report = sleeper.run()
            log.info("睡眠完成：%s", report)

        bot = QQBot(companion, cfg, on_daily=daily)
        log.info("连接 NapCat：%s（按 Ctrl+C 退出）", cfg.ws_url)
        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            log.info("正在退出……")
        finally:
            bot.close()
            try:
                done = companion.extract_pending(force=True)
                log.info("退出前整理了 %d 段会话", len(done))
            except ModelError as e:
                log.error("退出前整理失败（下次启动会重试）：%s", e)
    return 0
