"""streamable HTTP 传输：每个请求一个 POST，回应可能是 JSON 也可能是 SSE（text/event-stream）。

这是 2025-03-26 之后的标准；更早的纯 SSE 接入方式不支持。
"""

import json
from typing import Any

import httpx

from pocketagent.tools.mcp.config import McpServerConfig
from pocketagent.tools.mcp.protocol import (
    CLIENT_INFO,
    OLD_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    STARTUP_TIMEOUT_SECONDS,
    McpCallResult,
    McpError,
    McpServerError,
    McpToolInfo,
    McpTransportError,
    ProgressListener,
    _response_result,
    _run_progress_listener,
    parse_call_result,
    parse_tools_result,
)


def _sse_messages(text: str) -> list[dict[str, Any]]:
    """把 text/event-stream 的正文拆成一条条消息。event:/retry:/注释行都不用管，只认 data: 行。"""
    data_lines: list[str] = []
    messages: list[dict[str, Any]] = []

    def flush() -> None:
        if not data_lines:
            return
        messages.extend(_parse_data_lines(data_lines))
        data_lines.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    flush()
    return messages


def _parse_data_lines(data_lines: list[str]) -> list[dict[str, Any]]:
    try:
        message = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return []
    return [message] if isinstance(message, dict) else []


class HttpMcpClient:
    """一个 streamable HTTP 传输的 MCP 服务器：每个请求一个 POST，回应可能是 JSON 也可能是 SSE。"""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.alive = False
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._next_id = 1
        self._progress_handlers: dict[Any, ProgressListener] = {}

    async def start(self) -> None:
        self._client = httpx.AsyncClient()
        await self._initialize()
        self.alive = True

    async def _initialize(self) -> None:
        params: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        try:
            await self._request("initialize", params, STARTUP_TIMEOUT_SECONDS)
        except McpServerError as error:
            if "protocol" not in str(error).lower():
                raise
            params["protocolVersion"] = OLD_PROTOCOL_VERSION
            await self._request("initialize", params, STARTUP_TIMEOUT_SECONDS)
        await self._notify("notifications/initialized")

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", **self.config.headers}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        timeout_seconds: float,
        on_progress: ProgressListener | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        request_params = dict(params or {})
        progress_token = None
        if on_progress is not None:
            progress_token = f"progress-{request_id}"
            request_params.setdefault("_meta", {})["progressToken"] = progress_token
            self._progress_handlers[progress_token] = on_progress
        if request_params:
            payload["params"] = request_params
        try:
            return await self._post(payload, timeout_seconds, request_id)
        finally:
            if progress_token is not None:
                self._progress_handlers.pop(progress_token, None)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        response = await self._post_raw(payload, STARTUP_TIMEOUT_SECONDS)
        if response.status_code not in (200, 202):
            raise McpError(f"HTTP {response.status_code}：{response.text[:300]}")

    async def _post_raw(self, payload: dict[str, Any], timeout_seconds: float) -> httpx.Response:
        if self._client is None:
            raise McpTransportError("HTTP 客户端还没起来")
        try:
            return await self._client.post(
                self.config.url or "",
                json=payload,
                headers=self._request_headers(),
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise McpTransportError(f"{type(error).__name__}: {error}") from error

    async def _post(self, payload: dict[str, Any], timeout_seconds: float, request_id: int) -> dict[str, Any]:
        response = await self._post_raw(payload, timeout_seconds)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code == 202:
            # 通知（没有 id）正常情况下就是 202 没有正文
            if "id" in payload:
                raise McpError("服务器回了 202，没有给这次请求的结果")
            return {}
        if response.status_code == 404:
            # 按协议这是会话失效：标成断了，下一次调用会让管理器重新握手
            self.alive = False
            raise McpTransportError(f"HTTP 404（会话失效或地址不对）：{self.config.url}")
        if response.status_code >= 400:
            raise McpError(f"HTTP {response.status_code}：{response.text[:300]}")
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return await self._response_from_sse(response.text, request_id)
        message = _json_or_none(response.text)
        if message is None:
            raise McpError(f"服务器返回的不是 JSON：{response.text[:200]}")
        if "method" in message and "id" not in message:
            await self._dispatch_notification(message)
            raise McpError("服务器只回了一条通知，没有这次请求的结果")
        return _response_result(message)

    async def _response_from_sse(self, text: str, request_id: int) -> dict[str, Any]:
        for message in _sse_messages(text):
            if message.get("id") == request_id and "method" not in message:
                return _response_result(message)
            if "method" in message and "id" not in message:
                await self._dispatch_notification(message)
        raise McpError("服务器的流式响应里没有这次请求的结果")

    async def _dispatch_notification(self, message: dict[str, Any]) -> None:
        if message.get("method") == "notifications/progress":
            await _run_progress_listener(self._progress_handlers, message.get("params"))

    async def list_tools(self) -> list[McpToolInfo]:
        result = await self._request("tools/list", {}, self.config.timeout_seconds)
        return parse_tools_result(result)

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], on_progress: ProgressListener | None = None
    ) -> McpCallResult:
        params = {"name": tool_name, "arguments": arguments or {}}
        result = await self._request("tools/call", params, self.config.timeout_seconds, on_progress=on_progress)
        return parse_call_result(result)

    def failure_hint(self) -> str:
        return ""

    async def close(self) -> None:
        self.alive = False
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _json_or_none(text: str) -> dict[str, Any] | None:
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None
