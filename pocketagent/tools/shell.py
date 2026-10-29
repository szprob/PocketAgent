"""bash 工具：在工作目录里执行 shell 命令。

默认关在沙箱里（见 sandbox.py）：只能写工作目录、临时目录和缓存目录，读不到密钥，不能联网。
模型确实需要跳出沙箱时带上 outside_sandbox=true，这种调用一定会请用户确认（「完全访问」模式除外）。

命令输出边跑边读：通过 current_tool_output 回调实时发给界面，模型最后拿到的仍然是完整结果。
"""

import asyncio
import codecs
from typing import Any

from pocketagent.safety.processes import terminate_process_tree
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
    "不要用它来读写文件（用 read_file / edit_file）。"
    "dev server、watch 这类不会自己退出的命令设 run_in_background=true，"
    "拿到 id 后用 bash_output 看输出、用 kill_shell 结束。"
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
            "run_in_background": {
                "type": "boolean",
                "description": "放到后台运行，立刻返回 id（适合 dev server / watch；后台命令不受 timeout_seconds 限制）",
            },
        }
        if sandboxed:
            properties["outside_sandbox"] = {
                "type": "boolean",
                "description": "在沙箱外执行（能联网、能写任何地方），需要用户确认；默认 false",
            }
        self.parameters = {"type": "object", "properties": properties, "required": ["command"]}

    def describe(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", ""))
        if arguments.get("outside_sandbox"):
            command += "  ［沙箱外］"
        if arguments.get("run_in_background"):
            command += "  ［后台］"
        return command

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        # 沙箱整体关掉时本来就不隔离，跳不跳出去没区别
        wants_out = bool(arguments.get("outside_sandbox")) and context.sandbox is not None
        return ToolAccess(outside_sandbox=wants_out, command=str(arguments.get("command", "")))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        timeout_seconds = min(int(arguments.get("timeout_seconds", context.shell_timeout_seconds)), MAX_TIMEOUT_SECONDS)
        policy = None if arguments.get("outside_sandbox") else context.sandbox
        command_line, sandbox_note = wrap_command(arguments["command"], policy)
        if arguments.get("run_in_background"):
            manager = _background_manager(context)
            if manager is None:
                return ToolResult("当前上下文没有配置后台命令管理器", is_error=True)
            try:
                record = await manager.start(arguments["command"], command_line, sandbox_note)
            except (OSError, TimeoutError) as error:
                return ToolResult(f"后台命令启动失败：{error}", is_error=True)
            note = f"\n{sandbox_note}" if sandbox_note else ""
            text = (
                f"已在后台启动（{record['id']}）：{arguments['command']}\n"
                "用 bash_output 查看输出（可带 wait_seconds 等新输出），用 kill_shell 结束。"
                f"{note}"
            )
            if record.get("error"):
                text += f"\n{record['error']}"
            return ToolResult(text, is_error=record["status"] == "error")
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


MAX_OUTPUT_WAIT_SECONDS = 30


class BashOutputTool(Tool):
    """读后台命令的新输出。id 不填就列出这个会话里所有后台命令。"""

    name = "bash_output"
    kind = ToolKind.READ
    description = (
        "查看后台命令（bash run_in_background 启动的）的输出。带 id 只看这个命令上次读取之后的新输出；"
        "不带 id 列出所有后台命令。可以带 wait_seconds 等一会儿新输出。命令跑完了也能取最后的输出。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "bash 后台启动时返回的 id，如 bash_1；不填就列出全部"},
            "wait_seconds": {
                "type": "integer",
                "description": f"最多等这么多秒的新输出（0-{MAX_OUTPUT_WAIT_SECONDS}，默认 0）",
            },
        },
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        shell_id = str(arguments.get("id") or "")
        return f"查看后台命令 {shell_id}" if shell_id else "列出后台命令"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _background_manager(context)
        if manager is None:
            return ToolResult("当前上下文没有配置后台命令管理器", is_error=True)
        shell_id = str(arguments.get("id") or "").strip()
        if not shell_id:
            return _list_background_shells(context)
        try:
            record = manager.status(shell_id)
        except ValueError:
            return _unknown_shell(shell_id, context)
        offsets = context.tool_state.setdefault("background_offsets", {})
        offset = int(offsets.get(shell_id, 0))
        wait_seconds = min(max(int(arguments.get("wait_seconds") or 0), 0), MAX_OUTPUT_WAIT_SECONDS)
        if wait_seconds:
            record = await _wait_for_background(manager, shell_id, offset, wait_seconds)
        result = manager.read(shell_id, offset=offset)
        offsets[shell_id] = result["next_offset"]
        output = result["output"]
        status = _background_status_text(record)
        text = f"{status}\n{output}" if output else f"{status}\n（这期间没有新输出）"
        return ToolResult(text, is_error=not _background_running(record) and record.get("exit_code") not in (0, None))


class KillShellTool(Tool):
    """结束这个会话里的后台命令。只认后台命令表里的 id，不碰任意进程，所以算只读工具、不用审批。"""

    name = "kill_shell"
    kind = ToolKind.READ
    description = "结束一个后台命令（bash run_in_background 启动的），连它派生的子进程一起结束。"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "bash 后台启动时返回的 id，如 bash_1"}},
        "required": ["id"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return f"结束后台命令 {arguments.get('id', '')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _background_manager(context)
        if manager is None:
            return ToolResult("当前上下文没有配置后台命令管理器", is_error=True)
        shell_id = str(arguments.get("id") or "").strip()
        try:
            before = manager.status(shell_id)
        except ValueError:
            return _unknown_shell(shell_id, context)
        was_running = _background_running(before)
        try:
            record = await manager.stop(shell_id, grace_seconds=2)
        except (OSError, ValueError) as error:
            return ToolResult(f"结束后台命令失败：{error}", is_error=True)
        action = "已结束" if was_running else "本来就已经退出"
        return ToolResult(f"{action}（{shell_id}）：{_background_status_text(record)}")


def _background_manager(context: ToolContext) -> Any:
    return context.background_tasks


def _list_background_shells(context: ToolContext) -> ToolResult:
    manager = _background_manager(context)
    if manager is None:
        return ToolResult("当前上下文没有配置后台命令管理器", is_error=True)
    records = manager.list()
    if not records:
        return ToolResult("没有后台命令")
    lines = ["后台命令："]
    for record in records:
        lines.append(f"  {_background_status_text(record)}  {record['command']}")
    return ToolResult("\n".join(lines))


def _unknown_shell(shell_id: str, context: ToolContext) -> ToolResult:
    manager = _background_manager(context)
    known_ids = "、".join(record["id"] for record in manager.list()) if manager is not None else "没有"
    return ToolResult(f"没有 {shell_id} 这个后台命令（现有的：{known_ids or '没有'}）", is_error=True)


def _background_running(record: dict[str, Any]) -> bool:
    return record.get("status") in {"starting", "running"}


def _background_status_text(record: dict[str, Any]) -> str:
    shell_id = record["id"]
    if _background_running(record):
        return f"[{shell_id} 还在运行；要结束用 kill_shell]"
    return f"[{shell_id} 已退出，exit code {record.get('exit_code')}]"


async def _wait_for_background(
    manager: Any, shell_id: str, offset: int, timeout_seconds: float
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        record = manager.status(shell_id)
        if record["output_bytes"] > offset or not _background_running(record):
            return record
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return record
        await asyncio.sleep(min(0.1, remaining))


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
