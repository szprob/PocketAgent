"""stdio 传输：起一个子进程，JSON-RPC 消息经它的 stdin / stdout 一行一条地收发。"""

import asyncio
import json
import os
from typing import Any

from pocketagent.safety.processes import terminate_process_tree
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
    _jsonrpc_error_text,
    _run_progress_listener,
    _swallow_task_error,
    parse_call_result,
    parse_tools_result,
)

MAX_LIST_PAGES = 100
# stdio 一行消息的上限：工具结果里带 base64 图片时一行能有几 MB
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class StdioMcpClient:
    """一个 stdio 传输的 MCP 服务器：子进程 + 按行收发的 JSON-RPC。

    多个调用可以同时挂着（按 id 对上各自的回应）；写入用锁串行，读是一个独立任务。
    """

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.alive = False
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._progress_handlers: dict[Any, ProgressListener] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._stderr_tail = ""
        self._disconnect_reason = ""

    async def start(self) -> None:
        environment = {**os.environ, **self.config.env}
        self._process = await asyncio.create_subprocess_exec(
            self.config.command or "",
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=self.config.cwd or None,
            start_new_session=True,
            limit=MAX_MESSAGE_BYTES,
        )
        self.alive = True
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    async def _initialize(self) -> None:
        params: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        try:
            await self._request("initialize", params, STARTUP_TIMEOUT_SECONDS)
        except McpServerError as error:
            # 旧服务器不认识新版本号：按它认识的版本再握一次手
            if "protocol" not in str(error).lower():
                raise
            params["protocolVersion"] = OLD_PROTOCOL_VERSION
            await self._request("initialize", params, STARTUP_TIMEOUT_SECONDS)
        await self._notify("notifications/initialized")

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            try:
                line = await process.stdout.readline()
            except ValueError:
                self._on_disconnected(f"一行消息超过 {MAX_MESSAGE_BYTES // (1024 * 1024)}MB 上限")
                return
            if not line:
                self._on_disconnected("服务器进程结束了")
                return
            self._handle_message(line)

    async def _drain_stderr(self) -> None:
        """stderr 只攒最后一段：服务器崩了的时候拿它说明原因，平时丢掉。"""
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                return
            self._stderr_tail = (self._stderr_tail + chunk.decode("utf-8", "replace"))[-8000:]

    def _handle_message(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            # 有些服务器往 stdout 打日志（按协议应该打 stderr），忽略这些行
            return
        if not isinstance(message, dict):
            return
        if "id" in message and "method" in message:
            self._handle_server_request(message)
        elif "id" in message:
            self._resolve_response(message)
        else:
            self._handle_notification(message)

    def _resolve_response(self, message: dict[str, Any]) -> None:
        future = self._pending.get(message.get("id"))
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(McpServerError(_jsonrpc_error_text(message.get("error"))))
        else:
            future.set_result(message.get("result") or {})

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        """服务器反过来请求客户端（ping、roots/list 这类）。只答 ping，其余按协议回「不支持」。"""
        request_id = message.get("id")
        if message.get("method") == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"pocketagent 不支持服务端请求 {message.get('method')}"},
            }
        task = asyncio.create_task(self._send_quietly(reply))
        task.add_done_callback(_swallow_task_error)

    def _handle_notification(self, message: dict[str, Any]) -> None:
        # 进度通知转给这次调用挂的监听器；tools/list_changed 这类先不管（重连时会重新列一遍）
        if message.get("method") == "notifications/progress":
            task = asyncio.create_task(_run_progress_listener(self._progress_handlers, message.get("params")))
            task.add_done_callback(_swallow_task_error)

    async def _send_quietly(self, payload: dict[str, Any]) -> None:
        try:
            await self._send(payload)
        except (McpError, OSError):
            pass

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpTransportError("服务器进程还没起来")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                process.stdin.write(line.encode("utf-8"))
                await process.stdin.drain()
            except (ConnectionResetError, BrokenPipeError, RuntimeError) as error:
                self._on_disconnected(f"写不进服务器进程了：{type(error).__name__}")
                raise McpTransportError(self.failure_hint() or "写不进服务器进程了")

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        timeout_seconds: float,
        on_progress: ProgressListener | None = None,
    ) -> dict[str, Any]:
        if not self.alive:
            raise McpTransportError(f"{self._disconnect_reason or '连接已经断开'}{self.failure_hint()}")
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        request_params = dict(params or {})
        progress_token = None
        if on_progress is not None:
            progress_token = request_id
            request_params.setdefault("_meta", {})["progressToken"] = progress_token
            self._progress_handlers[progress_token] = on_progress
        if request_params:
            payload["params"] = request_params
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(payload)
            return await asyncio.wait_for(future, timeout_seconds)
        except asyncio.TimeoutError:
            raise McpError(f"请求 {method} 超过 {int(timeout_seconds)} 秒没有回应")
        except asyncio.CancelledError:
            # 用户中断了这一轮：告诉服务器别再做这次请求了（它可能正在跑长任务）
            await self._send_quietly(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}
            )
            raise
        finally:
            self._pending.pop(request_id, None)
            if progress_token is not None:
                self._progress_handlers.pop(progress_token, None)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def list_tools(self) -> list[McpToolInfo]:
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params, self.config.timeout_seconds)
            tools.extend(parse_tools_result(result))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        return tools

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], on_progress: ProgressListener | None = None
    ) -> McpCallResult:
        params = {"name": tool_name, "arguments": arguments or {}}
        result = await self._request("tools/call", params, self.config.timeout_seconds, on_progress=on_progress)
        return parse_call_result(result)

    def _on_disconnected(self, reason: str) -> None:
        if not self.alive:
            return
        self.alive = False
        self._disconnect_reason = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpTransportError(f"{reason}{self.failure_hint()}"))
        self._pending.clear()

    def failure_hint(self) -> str:
        """进程出问题时带上它 stderr 的最后几行，用户能看出是包没装还是密钥没配。"""
        tail = self._stderr_tail.strip()
        if not tail:
            return ""
        return f"；服务器 stderr 最后的输出：\n{tail[-1500:]}"

    async def close(self) -> None:
        self.alive = False
        process = self._process
        if process is not None and process.stdin is not None:
            # 关掉 stdin，规矩的服务器会自己退出
            process.stdin.close()
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                await terminate_process_tree(process)
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpTransportError("连接已关闭"))
        self._pending.clear()
