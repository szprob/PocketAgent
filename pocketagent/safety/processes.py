"""跨平台的子进程辅助：找到 bash、结束整棵进程树。

bash 工具和 ripgrep 都要起外部进程，收尾时都得把子进程一起带走：

- bash：macOS / Linux 上一般就在 PATH 里；Windows 靠 Git for Windows 自带的 bash，
  用户没把 Git 加进 PATH 时按默认安装位置找
- 结束：POSIX 杀掉进程组（调用方的 start_new_session=True 建组），Windows 用 taskkill /T
"""

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path


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


def process_exists(pid: int) -> bool:
    """只用于展示后台任务状态；停止前仍由监督进程写入最终状态。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# OpenProcess / GetExitCodeProcess 用到的常量（winnt.h / winerror.h）
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _windows_process_exists(pid: int) -> bool:
    """Windows 上不能用 os.kill(pid, 0) 探活：除 CTRL_C / CTRL_BREAK 以外的信号都会走 TerminateProcess，
    一查就把进程杀了。改成打开进程句柄看退出码。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 拒绝访问说明进程在、只是不归我们管；其余情况（参数无效）就是没有这个进程
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        # 进程退出了但还有人拿着句柄时也能打开，要看退出码才知道还在不在跑
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


async def signal_process_tree(pid: int, force: bool) -> None:
    """给不由当前进程托管的后台命令发信号；调用方负责等监督进程写最终状态。"""
    if sys.platform == "win32":
        await _taskkill_tree(pid, force=force)
        return
    signal_number = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, signal_number)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal_number)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # 进程已经没了，或者不是组长：退回只杀自己
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _taskkill_tree(pid: int, force: bool = True) -> None:
    """Windows 没有进程组信号，taskkill /T 才会连子进程一起结束。"""
    killer = shutil.which("taskkill") or "taskkill"
    arguments = [killer]
    if force:
        arguments.append("/F")
    arguments += ["/T", "/PID", str(pid)]
    try:
        taskkill = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        # 连 taskkill 都起不来（极少数精简系统），让下面的 wait() 自己收尾
        return
    await taskkill.wait()
