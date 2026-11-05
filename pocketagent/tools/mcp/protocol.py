"""MCP 协议层的公共部分：协议常量、错误类型、客户端信息，JSON-RPC 响应和工具列表 / 调用结果的解析，进度通知分发。

stdio 和 HTTP 两种传输都从这里取。这个模块不依赖包里别的模块，放在最底层，避免循环导入。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# 想兼容尽量多的服务器：先按新版本握手，服务器报「不认识这个协议版本」再退回旧的
PROTOCOL_VERSION = "2025-06-18"
OLD_PROTOCOL_VERSION = "2024-11-05"
# 握手（起进程 + initialize + 列工具）的耐心：npx 第一次跑要下载包，冷机器上几十秒不奇怪
STARTUP_TIMEOUT_SECONDS = 60

ProgressListener = Callable[[float | None, str | None], Awaitable[None]]


class McpError(RuntimeError):
    """MCP 这一层的错误（配置、连接、调用超时、服务器报错）。调用方拿到的就是里面的文字。"""


class McpTransportError(McpError):
    """连接本身断了（进程退出、管道关闭）。下一次调用时管理器会尝试重启服务器。"""


class McpServerError(McpError):
    """服务器对这次请求回了 JSON-RPC error。"""


# ---- 客户端信息 ----


def _client_version() -> str:
    try:
        from importlib.metadata import version

        return version("pocketagent")
    except Exception:
        return "0"


CLIENT_INFO = {"name": "pocketagent", "version": _client_version()}


# ---- 协议层公共的小工具 ----


@dataclass
class McpToolInfo:
    """tools/list 里拿到的一个工具。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class McpCallResult:
    """tools/call 的结果：content 原样留着（text / image / resource 都可能），渲染在桥接那层做。"""

    content: list[dict[str, Any]]
    is_error: bool
    structured: dict[str, Any] | None


def _jsonrpc_error_text(error: Any) -> str:
    if not isinstance(error, dict):
        return "服务器返回了一个没有说明的错误"
    message = str(error.get("message") or "没有说明原因")
    code = error.get("code")
    return f"{message}（JSON-RPC 错误码 {code}）" if code is not None else message


def _response_result(message: dict[str, Any]) -> dict[str, Any]:
    """一条 JSON-RPC 响应消息 → result 字典；error 消息抛 McpServerError。"""
    if "error" in message:
        raise McpServerError(_jsonrpc_error_text(message.get("error")))
    result = message.get("result")
    return result if isinstance(result, dict) else {}


def _parse_tool_entry(entry: Any) -> McpToolInfo | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    description = entry.get("description")
    schema = entry.get("inputSchema")
    return McpToolInfo(
        name=name.strip(),
        description=description if isinstance(description, str) else "",
        input_schema=schema if isinstance(schema, dict) else {},
    )


def parse_tools_result(result: dict[str, Any]) -> list[McpToolInfo]:
    tools = [_parse_tool_entry(entry) for entry in result.get("tools") or []]
    return [tool for tool in tools if tool is not None]


def parse_call_result(result: dict[str, Any]) -> McpCallResult:
    content = result.get("content")
    structured = result.get("structuredContent")
    return McpCallResult(
        content=content if isinstance(content, list) else [],
        is_error=bool(result.get("isError")),
        structured=structured if isinstance(structured, dict) else None,
    )


async def _run_progress_listener(handlers: dict[Any, ProgressListener], params: Any) -> None:
    """notifications/progress → 这次调用注册的监听器（工具输出流）。"""
    if not isinstance(params, dict):
        return
    handler = handlers.get(params.get("progressToken"))
    if handler is None:
        return
    progress = params.get("progress")
    message = params.get("message")
    await handler(progress if isinstance(progress, (int, float)) else None, message if isinstance(message, str) else None)


def _swallow_task_error(task: "asyncio.Task") -> None:
    # 进度转发失败不值得惊动任何人，但必须把异常取出来，不然事件循环告警
    if not task.cancelled() and task.exception() is not None:
        pass
