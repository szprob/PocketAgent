"""桥接：把 MCP 服务器的一个工具包装成注册表里的内置工具。

tools/call 的结果（文字、图片、资源）也在这里渲染成 ToolResult，二进制内容落盘到会话的工具输出目录。
"""

import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult, current_tool_output
from pocketagent.tools.mcp.protocol import McpCallResult, McpError, McpToolInfo, ProgressListener

if TYPE_CHECKING:
    # manager 运行时要导入本模块来注册桥接工具；这里只为类型标注引用它，放进 TYPE_CHECKING 免得循环导入
    from pocketagent.tools.mcp.manager import McpManager


_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]")


def bridged_name(server_name: str, tool_name: str) -> str | None:
    """mcp__<服务器>__<工具>。OpenAI 的函数名最长 64 字符，超了返回 None 让调用方跳过。"""
    server = _UNSAFE_NAME_CHARACTERS.sub("_", server_name.strip()) or "server"
    tool = _UNSAFE_NAME_CHARACTERS.sub("_", tool_name.strip()) or "tool"
    full_name = f"mcp__{server}__{tool}"
    return full_name if len(full_name) <= 64 else None


class McpToolBridge(Tool):
    """把 MCP 服务器的一个工具包装成内置工具。服务器断开时下一次调用会自动重连。"""

    kind = ToolKind.EXECUTE

    def __init__(self, manager: "McpManager", server_name: str, info: McpToolInfo) -> None:
        self.manager = manager
        self.server_name = server_name
        self.mcp_tool_name = info.name
        self.name = bridged_name(server_name, info.name) or f"mcp__{server_name}"
        description = (info.description or "").strip() or "（服务器没有给这个工具写说明）"
        self.description = f"MCP 服务器 {server_name} 的工具 {info.name}。{description}"
        self.parameters = _normalize_input_schema(info.input_schema)

    def describe(self, arguments: dict[str, Any]) -> str:
        text = json.dumps(arguments, ensure_ascii=False)
        summary = f"{self.server_name} · {self.mcp_tool_name} {text}"
        return summary if len(summary) <= 120 else summary[:117] + "..."

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        # 进度通知从 MCP 专用线程发回来，要转回 agent 所在的循环再走事件总线，所以把两个都先抓住
        progress_listener = _tool_progress_listener(current_tool_output.get(), asyncio.get_running_loop())
        try:
            result = await self.manager.call_tool(self.server_name, self.mcp_tool_name, arguments, progress_listener)
        except McpError as error:
            return ToolResult(str(error), is_error=True)
        return render_call_result(result, context, self.server_name)


def _normalize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """服务器给的 inputSchema 直接发给模型前修一下：必须是 object，properties / required 得像样。"""
    fixed = dict(schema)
    if fixed.get("type") != "object":
        fixed["type"] = "object"
    properties = fixed.get("properties")
    if not isinstance(properties, dict):
        fixed["properties"] = {}
    required = fixed.get("required")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        fixed.pop("required", None)
    return fixed


def _tool_progress_listener(
    listener: Callable[[str], Awaitable[None]] | None, agent_loop: asyncio.AbstractEventLoop
) -> ProgressListener:
    """服务器的进度通知 → 界面的工具输出流（bash 实时输出那条通道）。"""

    async def on_progress(progress: float | None, message: str | None) -> None:
        if listener is None:
            return
        parts = [message.strip()] if message and message.strip() else ["进行中"]
        if progress is not None:
            parts.append(f"（进度 {progress}）")
        coroutine = listener("".join(parts) + "\n")
        # 平时（web、命令行 REPL）这条回调就在 agent 循环上；TUI 一轮一个循环，从 MCP 线程转回去
        if asyncio.get_running_loop() is agent_loop:
            await coroutine
        else:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, agent_loop))

    return on_progress


# 工具结果里的二进制内容按 mimeType 落盘时的扩展名；认不出的用 bin
_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "video/mp4": "mp4",
    "application/pdf": "pdf",
    "application/json": "json",
}
_UNSAFE_FILENAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")


def _extension_for(mime_type: str | None) -> str:
    if mime_type in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[mime_type]
    subtype = (mime_type or "").split("/")[-1]
    subtype = _UNSAFE_FILENAME_CHARACTERS.sub("", subtype)
    return subtype[:8].lower() or "bin"


def _save_binary_output(context: ToolContext, stem: str, mime_type: str | None, data: bytes) -> Path | None:
    """把图片 / 资源这类二进制内容写进这个会话的工具输出目录；写不了返回 None。"""
    directory = context.tool_outputs_dir
    if directory is None:
        return None
    safe_stem = _UNSAFE_FILENAME_CHARACTERS.sub("_", stem).strip("._-")[:60] or "mcp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe_stem}.{_extension_for(mime_type)}"
        path.write_bytes(data)
    except OSError:
        return None
    return path


def render_call_result(result: McpCallResult, context: ToolContext, server_name: str) -> ToolResult:
    """tools/call 的结果 → ToolResult：text 直接进内容，图片落盘后既能看图也留了路径。"""
    texts: list[str] = []
    images: list[Path] = []
    for index, item in enumerate(result.content):
        texts.append(_render_content_item(item, context, server_name, index, images))
    if result.structured:
        texts.append("结构化输出：\n" + json.dumps(result.structured, ensure_ascii=False, indent=2))
    content = "\n\n".join(text for text in texts if text) or "（服务器没有返回内容）"
    return ToolResult(content, is_error=result.is_error, images=images)


def _render_content_item(item: Any, context: ToolContext, server_name: str, index: int, images: list[Path]) -> str:
    if not isinstance(item, dict):
        return json.dumps(item, ensure_ascii=False)
    kind = item.get("type")
    if kind == "text" and isinstance(item.get("text"), str):
        return item["text"]
    if kind in ("image", "audio"):
        try:
            data = base64.b64decode(item.get("data") or "")
        except (ValueError, TypeError):
            return f"[{kind} 内容不是合法的 base64]"
        mime_type = item.get("mimeType") if isinstance(item.get("mimeType"), str) else None
        path = _save_binary_output(context, f"mcp-{server_name}-{index}", mime_type, data)
        if path is None:
            return f"[收到一段 {mime_type or kind} 内容，{len(data)} 字节，但这个会话没有地方保存]"
        if kind == "image":
            images.append(path)
            return f"[图片已保存：{path}]"
        return f"[音频已保存：{path}]"
    if kind == "resource":
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        uri = str(resource.get("uri") or "")
        if isinstance(resource.get("text"), str):
            return f"[资源 {uri}]\n{resource['text']}"
        if isinstance(resource.get("blob"), str):
            try:
                data = base64.b64decode(resource["blob"])
            except (ValueError, TypeError):
                return f"[资源 {uri} 的内容不是合法的 base64]"
            path = _save_binary_output(context, f"mcp-resource-{index}", resource.get("mimeType"), data)
            if path is None:
                return f"[资源 {uri}：{len(data)} 字节，但这个会话没有地方保存]"
            return f"[资源 {uri} 已保存到 {path}]"
        return f"[资源 {uri}]"
    if kind == "resource_link":
        return f"资源链接 {item.get('name', '')}：{item.get('uri', '')}"
    return json.dumps(item, ensure_ascii=False)
