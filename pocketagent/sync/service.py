"""后台同步：web / 桌面端活着的时候，每隔几分钟把改动推上去。

没有用 launchd / cron：这些入口在受管机器上经常被策略挡住，而且桌面端本来就是常驻的，
在它自己的事件循环里跑最省事。间隔从 sync.json 的 auto_interval_minutes 读，0 表示关掉；
每次睡醒重读一遍配置，改完不用重启服务。
"""

import asyncio
from pathlib import Path
from typing import Callable

from pocketagent.sync.backend import SyncError
from pocketagent.sync.config import SyncConfig, SyncConfigError, sync_config_path
from pocketagent.sync.engine import SyncEngine

DEFAULT_AUTO_INTERVAL_MINUTES = 10


def auto_sync_interval_minutes(home_dir: Path) -> int | None:
    """没配同步、或者明确关了自动同步时返回 None。"""
    try:
        config = SyncConfig.load(sync_config_path(home_dir))
    except SyncConfigError:
        return None
    if config.auto_interval_minutes <= 0:
        return None
    return config.auto_interval_minutes


def sync_once(home_dir: Path) -> str:
    """同步一次；返回空字符串表示成功或不需要同步，否则是给日志看的原因。"""
    try:
        config = SyncConfig.load(sync_config_path(home_dir))
    except SyncConfigError:
        return ""
    if config.auto_interval_minutes <= 0:
        return ""
    try:
        report = SyncEngine(home_dir, config).run()
    except SyncError as error:
        return str(error)
    return "；".join(report.errors)


async def run_periodic_sync(home_dir: Path, log: Callable[[str], None] | None = None) -> None:
    while True:
        interval = auto_sync_interval_minutes(home_dir)
        if interval is None:
            return
        await asyncio.sleep(interval * 60)
        error = await asyncio.to_thread(sync_once, home_dir)
        if error and log is not None:
            log(f"云同步失败：{error}")
