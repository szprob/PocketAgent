"""跨平台的子进程辅助：找到 bash、结束整棵进程树、管后台命令。

bash 工具和 ripgrep 都要起外部进程，收尾时都得把子进程一起带走：

- bash：macOS / Linux 上一般就在 PATH 里；Windows 靠 Git for Windows 自带的 bash，
  用户没把 Git 加进 PATH 时按默认安装位置找
- 结束：POSIX 杀掉进程组（调用方的 start_new_session=True 建组），Windows 用 taskkill /T
- 后台：dev server 这类不会自己退出的命令交给 BackgroundShells 管，输出攒在内存里等模型来读
"""

import asyncio
import codecs
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 后台命令的输出总量上限：dev server 跑久了会把日志刷满内存，早期没读过的部分丢掉并提示模型
MAX_BACKGROUND_OUTPUT_CHARS = 200_000
# 已经结束的后台命令保留几条，方便模型回头取最后的输出
MAX_KEPT_FINISHED_SHELLS = 10
BACKGROUND_READ_CHUNK_BYTES = 4096
BACKGROUND_WAIT_POLL_SECONDS = 0.1


def bash_executable() -> str | None:
    """返回 bash 的路径；这台机器上找不到就返回 None。

    Windows 上先找 Git for Windows：PATH 里可能还有 WSL 的 bash，它拿到 Windows 路径的工作目录跑不了。
    """
    if sys.platform == "win32":
        for candidate in _windows_bash_candidates():
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


def _windows_bash_candidates() -> list[Path]:
    """Git for Windows 常见的安装位置（系统级、64 位、32 位、用户级）。

    scoop、MSYS2 这类装法没法穷举，把 bash 放进 PATH 就行。
    """
    roots: list[Path] = []
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    local_app_data = os.environ.get("LocalAppData")
    if local_app_data:
        roots.append(Path(local_app_data) / "Programs")
    # 每个根目录下都可能是 bin\bash.exe 或 usr\bin\bash.exe
    return [
        root / "Git" / relative
        for root in roots
        for relative in (Path("bin") / "bash.exe", Path("usr") / "bin" / "bash.exe")
    ]


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """结束进程和它派生出来的子进程，然后等它真正退出。"""
    if process.returncode is None:
        if sys.platform == "win32":
            await _taskkill_tree(process.pid)
        else:
            _kill_process_group(process)
    await process.wait()


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # 进程已经没了，或者不是组长：退回只杀自己
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _taskkill_tree(pid: int) -> None:
    """Windows 没有进程组信号，taskkill /T 才会连子进程一起结束。"""
    killer = shutil.which("taskkill") or "taskkill"
    try:
        taskkill = await asyncio.create_subprocess_exec(
            killer,
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        # 连 taskkill 都起不来（极少数精简系统），让下面的 wait() 自己收尾
        return
    await taskkill.wait()


@dataclass
class BackgroundShell:
    """一个后台命令：进程、攒下来的输出、退出码。

    输出读取是增量的：read_new 只给上次之后的新内容，避免模型每次轮询都把整份日志重新读一遍。
    """

    id: str
    command: str
    process: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.monotonic)
    output_parts: list[str] = field(default_factory=list)
    read_index: int = 0
    total_chars: int = 0
    exit_code: int | None = None
    trimmed: bool = False
    # 输出读干净、退出码也拿到了才算结束；只跑 returncode 判断会让最后一段输出还没读到就报「已退出」
    finished: bool = False
    pump_task: "asyncio.Task[None] | None" = None

    @property
    def running(self) -> bool:
        return not self.finished

    @property
    def seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.output_parts.append(text)
        self.total_chars += len(text)
        self._trim()

    def _trim(self) -> None:
        """超过上限时从最早的开始丢；已经读过的先丢，读索引跟着往前移。"""
        while self.total_chars > MAX_BACKGROUND_OUTPUT_CHARS and len(self.output_parts) > self.read_index + 1:
            removed = self.output_parts.pop(0)
            self.total_chars -= len(removed)
            if self.read_index > 0:
                self.read_index -= 1
            self.trimmed = True

    def has_new_output(self) -> bool:
        return self.read_index < len(self.output_parts)

    def read_new(self) -> str:
        text = "".join(self.output_parts[self.read_index :])
        self.read_index = len(self.output_parts)
        if self.trimmed:
            self.trimmed = False
            return f"（更早的输出已经丢弃）\n{text}"
        return text

    def status_text(self) -> str:
        if self.running:
            return f"[{self.id} 还在运行，已运行 {self.seconds} 秒；要结束用 kill_shell]"
        return f"[{self.id} 已退出，exit code {self.exit_code}，运行 {self.seconds} 秒]"

    async def wait_for_update(self, timeout_seconds: float) -> None:
        """最多等这么多秒，等到有新输出或者进程退出；没有就按现状返回。"""
        deadline = time.monotonic() + timeout_seconds
        while not self.has_new_output() and self.running and time.monotonic() < deadline:
            await asyncio.sleep(BACKGROUND_WAIT_POLL_SECONDS)

    async def pump_output(self) -> None:
        """持续读合并后的 stdout/stderr；不读的话管道写满会把命令卡住。"""
        if self.process.stdout is None:
            self.finished = True
            self.exit_code = self.process.returncode
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = await self.process.stdout.read(BACKGROUND_READ_CHUNK_BYTES)
                if not chunk:
                    break
                self.append_output(decoder.decode(chunk))
            self.append_output(decoder.decode(b"", final=True))
            await self.process.wait()
        finally:
            self.exit_code = self.process.returncode
            self.finished = True

    async def kill(self) -> None:
        if self.running:
            await terminate_process_tree(self.process)
        if self.pump_task is not None:
            try:
                await self.pump_task
            except asyncio.CancelledError:
                pass
        self.exit_code = self.process.returncode
        self.finished = True


class BackgroundShells:
    """一个会话里的后台命令。id 按会话内顺序编号，模型只认这个表里的 id。"""

    def __init__(self) -> None:
        self.shells: dict[str, BackgroundShell] = {}
        self.counter = 0

    def get(self, shell_id: str) -> BackgroundShell | None:
        return self.shells.get(shell_id)

    def list_all(self) -> list[BackgroundShell]:
        return list(self.shells.values())

    async def launch(self, command_line: list[str], cwd: Path, command: str) -> BackgroundShell:
        process = await asyncio.create_subprocess_exec(
            *command_line,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.counter += 1
        shell = BackgroundShell(id=f"bash_{self.counter}", command=command, process=process)
        self.shells[shell.id] = shell
        shell.pump_task = asyncio.create_task(shell.pump_output())
        self._prune_finished()
        return shell

    def _prune_finished(self) -> None:
        finished = [shell for shell in self.shells.values() if not shell.running]
        for shell in finished[: max(len(finished) - MAX_KEPT_FINISHED_SHELLS, 0)]:
            self.shells.pop(shell.id, None)

    async def kill_all(self) -> None:
        """会话结束用：每个都尽量结束，一个失败不影响后面的；结束完清空表。"""
        try:
            for shell in list(self.shells.values()):
                try:
                    await shell.kill()
                except Exception:
                    # 收尾阶段以清理为准，单个进程结束失败不往外抛
                    continue
        finally:
            self.shells.clear()
