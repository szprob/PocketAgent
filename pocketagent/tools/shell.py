"""bash 工具：在工作目录里执行 shell 命令。

默认关在沙箱里（见 sandbox.py）：只能写工作目录、临时目录和缓存目录，读不到密钥，不能联网。
模型确实需要跳出沙箱时带上 outside_sandbox=true，这种调用一定会请用户确认（「完全访问」模式除外）。

命令输出边跑边读：通过 current_tool_output 回调实时发给界面，模型最后拿到的仍然是完整结果。
"""

import asyncio
import codecs
from typing import Any

from pocketagent.processes import terminate_process_tree
from pocketagent.safety.sandbox import denial_hint, wrap_command
from pocketagent.tools.base import (
    Tool,
    ToolAccess,
    ToolContext,
    ToolKind,
    ToolOutputListener,
    ToolResult,
    current_tool_output,
)

MAX_TIMEOUT_SECONDS = 600
READ_CHUNK_BYTES = 4096


BASE_DESCRIPTION = (
    "在工作目录里用 bash 执行一条命令，返回合并后的 stdout/stderr 和退出码。"
    "不要用它来读写文件（用 read_file / edit_file），也不要启动不会自己退出的进程。"
)
SANDBOX_DESCRIPTION = (
    "命令默认在沙箱里跑：只能写工作目录和临时目录，读不到密钥目录，不能联网。"
    "确实需要联网（装依赖、git push）或写工作目录以外的地方时，设 outside_sandbox=true，会请用户确认。"
)


class BashTool(Tool):
    name = "bash"
    kind = ToolKind.EXECUTE

    def __init__(self, sandboxed: bool = False) -> None:
        """sandboxed 只决定告诉模型什么（说明文字、有没有 outside_sandbox 参数）；真正隔离看 ToolContext.sandbox。"""
        self.description = BASE_DESCRIPTION + (SANDBOX_DESCRIPTION if sandboxed else "")
        properties: dict[str, Any] = {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout_seconds": {"type": "integer", "description": f"超时秒数，最长 {MAX_TIMEOUT_SECONDS}"},
        }
        if sandboxed:
            properties["outside_sandbox"] = {
                "type": "boolean",
                "description": "在沙箱外执行（能联网、能写任何地方），需要用户确认；默认 false",
            }
        self.parameters = {"type": "object", "properties": properties, "required": ["command"]}

    def describe(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", ""))
        return f"{command}  ［沙箱外］" if arguments.get("outside_sandbox") else command

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        # 沙箱整体关掉时本来就不隔离，跳不跳出去没区别
        wants_out = bool(arguments.get("outside_sandbox")) and context.sandbox is not None
        return ToolAccess(outside_sandbox=wants_out, command=str(arguments.get("command", "")))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        timeout_seconds = min(int(arguments.get("timeout_seconds", context.shell_timeout_seconds)), MAX_TIMEOUT_SECONDS)
        policy = None if arguments.get("outside_sandbox") else context.sandbox
        command_line, sandbox_note = wrap_command(arguments["command"], policy)
        process = await asyncio.create_subprocess_exec(
            *command_line,
            cwd=context.workspace,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # 单独的进程组，超时或被中断时能把子进程一起杀掉
            start_new_session=True,
        )
        output_parts: list[str] = []
        output_sink = current_tool_output.get()
        pump_task = asyncio.create_task(_pump_output(process, output_parts, output_sink))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            await _finish_pump(pump_task)
            return ToolResult(f"命令超过 {timeout_seconds} 秒未结束，已终止", is_error=True)
        except asyncio.CancelledError:
            await terminate_process_tree(process)
            pump_task.cancel()
            await _finish_pump(pump_task)
            raise
        await _finish_pump(pump_task)
        output = "".join(output_parts)
        exit_code = process.returncode
        text = f"{output}\n[exit code {exit_code}]" if output else f"[exit code {exit_code}]"
        if exit_code != 0:
            text += denial_hint(output, policy)
        if sandbox_note:
            text += f"\n{sandbox_note}"
        return ToolResult(text, is_error=exit_code != 0)


async def _pump_output(
    process: asyncio.subprocess.Process,
    output_parts: list[str],
    output_sink: ToolOutputListener | None,
) -> None:
    """逐块读合并后的输出；边读边交给界面，同时攒一份完整的给模型。"""
    if process.stdout is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = await process.stdout.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        text = decoder.decode(chunk)
        if text:
            output_parts.append(text)
            if output_sink is not None:
                await output_sink(text)
    tail = decoder.decode(b"", final=True)
    if tail:
        output_parts.append(tail)
        if output_sink is not None:
            await output_sink(tail)


async def _finish_pump(pump_task: asyncio.Task) -> None:
    """等输出读干净；进程被杀时管道会 EOF，这里只吞掉取消。"""
    try:
        await pump_task
    except asyncio.CancelledError:
        pass
