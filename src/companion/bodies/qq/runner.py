"""`companion qq`：连接 NapCat，让白璎在 QQ 上活动。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from companion.bodies.qq.bot import QQBot
from companion.config import Config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper
from companion.models.openai_compat import ModelError
from companion.runtime import AlreadyRunning, InstanceLock, run_dir, setup_logging, stop_flag

log = logging.getLogger(__name__)

EXIT_ALREADY_RUNNING = 3


def run_qq(config: Config, log_file: Path | None = None) -> int:
    setup_logging(log_file)
    try:
        lock = InstanceLock(run_dir(config.data_dir) / "qq.lock").acquire()
    except AlreadyRunning as e:
        log.error("%s，不再重复启动", e)
        return EXIT_ALREADY_RUNNING
    # 停止标记由启停脚本创建和清理；这里只负责看见它就退出
    flag = stop_flag(config.data_dir, "qq")

    cfg = config.qq
    if cfg.access_token is None:
        log.warning("没有设置 NAPCAT_TOKEN：如果 NapCat 的 WebSocket 服务设了 token，会连接失败")
    if not cfg.groups:
        log.info("没有配置 QQ_GROUPS：只回复私聊，不进任何群")
    if not cfg.owner_accounts:
        log.warning("没有配置 QQ_OWNER_ACCOUNTS：跨平台绑定无法确认，/状态 不可用")

    try:
        with Companion(config) as companion:
            sleeper = Sleeper(companion)

            def daily() -> bool:
                if not sleeper.due():  # 今天已经睡过（比如重启过服务）
                    return True
                report = sleeper.run()
                log.info("睡眠完成：%s", report)
                return not sleeper.due()

            companion.tasks.interrupt_active()
            companion.trpg.recover_delivery()
            bot = QQBot(companion, cfg, on_daily=daily)
            log.info("连接 NapCat：%s（按 Ctrl+C 或运行停止脚本退出）", cfg.ws_url)
            planned_stop = False
            try:
                asyncio.run(_run_until_stopped(bot, flag))
                planned_stop = flag.exists()
            except KeyboardInterrupt:
                planned_stop = True
                log.info("正在退出……")
            finally:
                bot.close()
                try:
                    done = companion.extract_pending(force=True)
                    log.info("退出前整理了 %d 段会话", len(done))
                    if planned_stop:
                        companion.rotate_chat_sessions("stop")
                except ModelError as e:
                    log.error("退出前整理失败（下次启动会重试）：%s", e)
    finally:
        lock.release()
    return 0


async def _run_until_stopped(bot: QQBot, flag: Path) -> None:
    """运行机器人，直到出现停止标记文件。"""
    task = asyncio.create_task(bot.run())
    while not task.done():
        if flag.exists():
            log.info("收到停止信号，正在退出……")
            bot.c.tasks.interrupt_active()
            task.cancel()
            break
        await asyncio.sleep(1)
    with contextlib.suppress(asyncio.CancelledError):
        await task
