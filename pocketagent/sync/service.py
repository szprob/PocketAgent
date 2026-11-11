"""事件驱动的云同步：配置改完、一轮对话结束各排一次。

不做定时轮询：闲着的机器没必要每隔几分钟去问一次远端。触发点只有两个——
设置保存（含模型、工作区、同步设置）和一轮对话结束。短时间内连着触发多次时，
后一次把前一次还没开始的等待取消掉，最后只同步一次。
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Callable

from pocketagent.sync.backend import SyncError
from pocketagent.sync.config import SyncConfig, SyncConfigError
from pocketagent.sync.engine import SyncEngine

# 一轮对话结束后等这么久再同步：连着跑几轮时合并成一次
TURN_END_DEBOUNCE_SECONDS = 20
# 配置改完不用等太久
CONFIG_DEBOUNCE_SECONDS = 1


def is_sync_configured(home_dir: Path) -> bool:
    try:
        SyncConfig.load(Path(home_dir))
    except SyncConfigError:
        return False
    return True


def sync_once(home_dir: Path) -> str:
    """同步一次；返回空字符串表示成功或没配置，否则是给日志看的原因。"""
    try:
        config = SyncConfig.load(Path(home_dir))
    except SyncConfigError:
        return ""
    try:
        report = SyncEngine(home_dir, config).run()
    except SyncError as error:
        return str(error)
    return "；".join(report.errors)


class SyncScheduler:
    """把多次触发合并成一次同步。没有运行中的事件循环时（命令行、测试）直接跳过。"""

    def __init__(self, home_dir: Path, log: Callable[[str], None] | None = None) -> None:
        self.home_dir = Path(home_dir)
        self.log = log
        self._task: asyncio.Task | None = None
        self._started = False

    def schedule(self, reason: str = "", delay: float = TURN_END_DEBOUNCE_SECONDS) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if not is_sync_configured(self.home_dir):
            return
        if self._task is not None and not self._task.done():
            if self._started:
                # 已经在同步了，这次不排队：下一次触发会再排
                return
            self._task.cancel()
        self._started = False
        self._task = loop.create_task(self._run(reason, delay))

    async def _run(self, reason: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self._started = True
        error = await asyncio.to_thread(sync_once, self.home_dir)
        if error and self.log is not None:
            self.log(f"云同步失败（{reason}）：{error}")

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
