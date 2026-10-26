"""bash 工具：在工作目录里执行 shell 命令。

骨架阶段没有沙箱，唯一的防线是权限审批；以后可以参考 Codex 接 macOS sandbox-exec / Linux bubblewrap。
"""

import asyncio
import os
import signal
from typing import Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

MAX_TIMEOUT_SECONDS = 600


class BashTool(Tool):
    name = "bash"
    description = (
        "在工作目录里用 bash 执行一条命令，返回合并后的 stdout/stderr 和退出码。"
        "不要用它来读写文件（用 read_file / edit_file），也不要启动不会自己退出的进程。"
    )
    kind = ToolKind.EXECUTE
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout_seconds": {"type": "integer", "description": f"超时秒数，最长 {MAX_TIMEOUT_SECONDS}"},
        },
        "required": ["command"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("command", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        timeout_seconds = min(int(arguments.get("timeout_seconds", context.shell_timeout_seconds)), MAX_TIMEOUT_SECONDS)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            arguments["command"],
            cwd=context.workspace,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # 单独的进程组，超时或被中断时能把子进程一起杀掉
            start_new_session=True,
        )
        try:
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await _kill_process_group(process)
            return ToolResult(f"命令超过 {timeout_seconds} 秒未结束，已终止", is_error=True)
        except asyncio.CancelledError:
            await _kill_process_group(process)
            raise
        output = output_bytes.decode("utf-8", errors="replace")
        exit_code = process.returncode
        text = f"{output}\n[exit code {exit_code}]" if output else f"[exit code {exit_code}]"
        return ToolResult(text, is_error=exit_code != 0)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()
