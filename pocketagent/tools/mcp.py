"""MCP（Model Context Protocol）客户端：把 MCP 服务器的工具接进工具注册表。

配置在 ~/.pocketagent/mcp.json（和 Claude Desktop / VS Code 等同一个格式）：

    {
      "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
        "docs": {"url": "https://mcp.example.com/mcp",
                  "headers": {"Authorization": "Bearer ${DOCS_KEY}"}, "timeout_seconds": 60}
      }
    }

- 配了 command 的是 stdio 传输：起一个子进程，JSON-RPC 消息一行一条地收发；
  配了 url 的是 streamable HTTP 传输（2025-03-26 之后的标准；更早的纯 SSE 接入方式不支持）。
- command、args、env 的值、url、headers 的值、cwd 里的 ${VAR} 和 ${VAR:-默认值} 会用环境变量展开，
  变量没设又没默认值时展开成空串（密钥漏配会以鉴权失败的形式暴露出来，而不是悄悄带上字面量）。
- 每个服务器可以单独 "enabled": false 停用；"timeout_seconds" 是一次工具调用的超时，默认 120 秒。
- 工具注册名是 mcp__<服务器名>__<工具名>。MCP 工具能做什么从外面说不清，所以一律按「执行命令」
  类走审批（ask 模式每次问，ai_auto 交给 AI 判断）；界面上可以「本任务总是允许」某个工具。
- 连接断了：这一次调用直接报错，不自动重试——请求可能已经在服务器那边执行了，重试会把有副作用
  的操作做两遍。再调用时会自动重启服务器进程。
- 服务器主动发来的进度（notifications/progress）会转发到界面的工具输出流里。

连接固定跑在 McpManager 自己的线程和事件循环上（TUI 每一轮是 asyncio.run 新开的循环，
子进程管道没法跨循环用）；web 端所有会话共用一份连接（SessionManager 持有一个 McpManager），
命令行一个进程一份。
"""

import asyncio
import base64
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pocketagent.safety.processes import terminate_process_tree
from pocketagent.tools.base import (
    Tool,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolResult,
    current_tool_output,
)

# 想兼容尽量多的服务器：先按新版本握手，服务器报「不认识这个协议版本」再退回旧的
PROTOCOL_VERSION = "2025-06-18"
OLD_PROTOCOL_VERSION = "2024-11-05"
# 握手（起进程 + initialize + 列工具）的耐心：npx 第一次跑要下载包，冷机器上几十秒不奇怪
STARTUP_TIMEOUT_SECONDS = 60
DEFAULT_CALL_TIMEOUT_SECONDS = 120
MAX_LIST_PAGES = 100
# stdio 一行消息的上限：工具结果里带 base64 图片时一行能有几 MB
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MCP_CONFIG_FILE = "mcp.json"

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


# ---- 配置 ----


@dataclass
class McpServerConfig:
    """一个 MCP 服务器的配置。command / args / env 是 stdio 传输，url / headers 是 HTTP 传输。"""

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_CALL_TIMEOUT_SECONDS
    enabled: bool = True

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


@dataclass
class McpConfigs:
    """一次读配置文件的结果：能用的服务器 + 解析不了的问题（问题不挡住能用的部分）。"""

    servers: list[McpServerConfig] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


_ENV_VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env_value(text: str) -> str:
    """把 ${VAR}、${VAR:-默认值} 换成环境变量的值；没设又没默认值就换成空串。"""
    return _ENV_VARIABLE_PATTERN.sub(_expand_one, text)


def _expand_one(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    value = os.environ.get(name)
    if value is None:
        return default if default is not None else ""
    return value


def _expand_everywhere(text: str) -> str:
    return expand_env_value(text) if isinstance(text, str) else text


def mcp_config_path(home_dir: Path) -> Path:
    return Path(home_dir) / MCP_CONFIG_FILE


def load_mcp_servers(home_dir: Path | None) -> McpConfigs:
    """读 ~/.pocketagent/mcp.json。文件不存在（还没配过）不是错误。"""
    if home_dir is None:
        return McpConfigs()
    path = mcp_config_path(home_dir)
    if not path.is_file():
        return McpConfigs()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return McpConfigs(problems=[f"MCP 配置 {path} 读不了：{error}"])
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        return McpConfigs(problems=[f"MCP 配置 {path} 顶层要是一个对象，里面 mcpServers 列各个服务器"])
    servers: list[McpServerConfig] = []
    problems: list[str] = []
    for name, entry in data["mcpServers"].items():
        try:
            servers.append(_parse_server(str(name), entry))
        except McpError as error:
            problems.append(str(error))
    return McpConfigs(servers=servers, problems=problems)


def _parse_server(name: str, entry: Any) -> McpServerConfig:
    server_name = name.strip()
    if not server_name:
        raise McpError("MCP 服务器名字不能为空")
    if not isinstance(entry, dict):
        raise McpError(f"MCP 服务器 {server_name} 的配置要是一个对象")
    command = _optional_expanded_text(entry.get("command"))
    url = _optional_expanded_text(entry.get("url"))
    if command and url:
        raise McpError(f"MCP 服务器 {server_name} 同时配了 command 和 url，只能选一种传输方式")
    if not command and not url:
        raise McpError(f"MCP 服务器 {server_name} 没配 command（stdio）也没配 url（HTTP）")
    return McpServerConfig(
        name=server_name,
        command=command,
        args=[_expand_everywhere(str(item)) for item in _string_list(entry.get("args"))],
        env={str(key): _expand_everywhere(str(value)) for key, value in _dict_of_strings(entry.get("env"), server_name).items()},
        cwd=_optional_expanded_text(entry.get("cwd")),
        url=url,
        headers={str(key): _expand_everywhere(str(value)) for key, value in _dict_of_strings(entry.get("headers"), server_name).items()},
        timeout_seconds=_timeout_seconds(entry.get("timeout_seconds"), server_name),
        enabled=_boolean(entry.get("enabled"), default=True),
    )


def _optional_expanded_text(value: Any) -> str | None:
    if value is None:
        return None
    text = expand_env_value(str(value)).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dict_of_strings(value: Any, server_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpError(f"MCP 服务器 {server_name} 的 env / headers 要是字符串到字符串的对象")
    return {str(key): str(item) for key, item in value.items()}


def _timeout_seconds(value: Any, server_name: str) -> int:
    if value in (None, ""):
        return DEFAULT_CALL_TIMEOUT_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise McpError(f"MCP 服务器 {server_name} 的 timeout_seconds 要是整数：{value}")
    if seconds <= 0:
        raise McpError(f"MCP 服务器 {server_name} 的 timeout_seconds 要大于 0：{value}")
    return seconds


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def config_to_public_dict(config: McpServerConfig) -> dict[str, Any]:
    """发给设置界面的版本：env / headers 只给键名，值不回传（留空表示不改）。"""
    return {
        "name": config.name,
        "transport": config.transport,
        "command": config.command,
        "args": list(config.args),
        "env": {key: "" for key in config.env},
        "url": config.url,
        "headers": {key: "" for key in config.headers},
        "timeout_seconds": config.timeout_seconds,
        "enabled": config.enabled,
    }


def save_mcp_servers(home_dir: Path, entries: list[Any], existing: McpConfigs) -> McpConfigs:
    """把设置界面提交的服务器写成 mcp.json。

    env / headers 里留空的值按同名键保留原值（界面上看不到密钥，留空就是不改）；
    提交里没有的键等于删掉，列表里没有的服务器等于删掉这个服务器。
    """
    existing_by_name = {config.name: config for config in existing.servers}
    payload: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("服务器配置的格式不对")
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            problems.append("有服务器没填名字")
            continue
        old = existing_by_name.get(name)
        transport = entry.get("transport")
        if transport not in ("stdio", "http"):
            transport = "http" if entry.get("url") else "stdio"
        data: dict[str, Any] = {
            "enabled": bool(entry.get("enabled", True)),
            "timeout_seconds": entry.get("timeout_seconds") or DEFAULT_CALL_TIMEOUT_SECONDS,
        }
        if transport == "http":
            data["url"] = str(entry.get("url") or "").strip()
            data["headers"] = _merge_secret_values(entry.get("headers"), old.headers if old else {})
        else:
            data["command"] = str(entry.get("command") or "").strip()
            arguments = entry.get("args")
            data["args"] = [str(item) for item in arguments] if isinstance(arguments, list) else []
            data["env"] = _merge_secret_values(entry.get("env"), old.env if old else {})
        try:
            _parse_server(name, data)
        except McpError as error:
            problems.append(str(error))
            continue
        payload[name] = data
    if problems:
        # 有问题的配置不落盘：别把现有配置写坏
        raise McpError("；".join(problems))
    path = mcp_config_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"mcpServers": payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    # 里面有密钥 / token，只给自己读写
    temporary.chmod(0o600)
    temporary.replace(path)
    return load_mcp_servers(home_dir)


def _merge_secret_values(submitted: Any, previous: dict[str, str]) -> dict[str, str]:
    """设置界面提交的 env / headers：留空的按同名键沿用原来的值（和模型密钥一个逻辑）。"""
    if not isinstance(submitted, dict):
        return dict(previous)
    merged: dict[str, str] = {}
    for key, value in submitted.items():
        name = str(key)
        text = str(value)
        merged[name] = previous.get(name, "") if text == "" else text
    return merged


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


# ---- stdio 传输 ----


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


# ---- streamable HTTP 传输 ----


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


def start_client(config: McpServerConfig) -> StdioMcpClient | HttpMcpClient:
    if config.transport == "http":
        return HttpMcpClient(config)
    return StdioMcpClient(config)


# ---- 连接管理 ----


@dataclass
class McpServerStatus:
    """界面上要显示的一个服务器状态。"""

    name: str
    connected: bool
    tool_count: int


class McpManager:
    """所有 MCP 服务器的连接：连一次，工具列表缓存下来，会话只管注册桥接。

    连接固定跑在一条专用线程的事件循环上，公开的异步方法从任何循环调用都会转过去——
    TUI 的每一轮是 asyncio.run 新开的循环，子进程管道和后台任务没法跨循环用，必须有个固定的家。
    web 端整个服务共用一个 manager（多个任务共用服务器进程）；命令行一个进程一个。
    """

    def __init__(self, home_dir: Path | None = None, configs: list[McpServerConfig] | None = None) -> None:
        self.home_dir = home_dir
        # configs 直接给（测试用）就不读文件；None = 每次从 ~/.pocketagent/mcp.json 读
        self._given_configs = list(configs) if configs is not None else None
        self._clients: dict[str, StdioMcpClient | HttpMcpClient] = {}
        self._tool_lists: dict[str, list[McpToolInfo]] = {}
        self._problems_by_server: dict[str, str] = {}
        # 只在 manager 自己的循环里用；所有会碰它的代码都经 _on_manager_loop 转过去
        self._connect_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._thread_ready = threading.Event()

    # ---- 专用线程 ----

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is None or self._loop.is_closed():
                self._thread_ready.clear()
                self._thread = threading.Thread(target=self._run_manager_loop, name="pocketagent-mcp", daemon=True)
                self._thread.start()
                self._thread_ready.wait(timeout=10)
            return self._loop or asyncio.new_event_loop()

    def _run_manager_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._thread_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _on_manager_loop(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """把 coroutine 放到 manager 自己的循环上跑；本来就在那个循环上就直接跑。"""
        manager_loop = self._ensure_loop()
        if asyncio.get_running_loop() is manager_loop:
            return await coroutine
        concurrent_future = asyncio.run_coroutine_threadsafe(coroutine, manager_loop)
        try:
            return await asyncio.wrap_future(concurrent_future)
        except asyncio.CancelledError:
            # agent 那边中断了这一轮：把 manager 循环上正在跑的调用也取消掉
            # （stdio 客户端会顺手给服务器发 notifications/cancelled）
            concurrent_future.cancel()
            raise

    # ---- 连接 ----

    def load(self) -> McpConfigs:
        if self._given_configs is not None:
            return McpConfigs(servers=list(self._given_configs))
        return load_mcp_servers(self.home_dir)

    async def ensure_connected(self) -> list[str]:
        """把配了的服务器都连上（并发的）；已连着的跳过。返回问题列表，给调用方显示。"""
        # 一个服务器都没有（还没配过）就不必为它起线程
        configs = self.load()
        if not any(config.enabled for config in configs.servers) and not self._clients:
            return list(configs.problems)
        return await self._on_manager_loop(self._connect_all())

    async def reload(self, force: bool = False) -> list[str]:
        """重新读配置文件，接上的服务器按新配置增删（设置保存后用）。

        force=True 时已连着的也重连（设置里点「重新连接」）。
        """
        return await self._on_manager_loop(self._connect_all(force))

    async def _connect_all(self, force: bool = False) -> list[str]:
        # 和 _restart 互斥：设置保存触发重连的同时有工具调用要重连，不能各起一个进程
        async with self._connect_lock:
            return await self._connect_all_locked(force)

    async def _connect_all_locked(self, force: bool = False) -> list[str]:
        configs = self.load()
        problems = list(configs.problems)
        jobs: list[asyncio.Task] = []
        seen_names: set[str] = set()
        for config in configs.servers:
            seen_names.add(config.name)
            if not config.enabled:
                jobs.append(asyncio.create_task(self._drop_client(config.name)))
                continue
            client = self._clients.get(config.name)
            if not force and client is not None and client.alive and client.config == config:
                continue
            jobs.append(asyncio.create_task(self._connect_one(config)))
        # 配置里删掉的：连着也关掉
        for name in list(self._clients):
            if name not in seen_names:
                jobs.append(asyncio.create_task(self._drop_client(name)))
        if jobs:
            await asyncio.gather(*jobs)
        return problems

    async def _drop_client(self, server_name: str) -> None:
        client = self._clients.pop(server_name, None)
        self._tool_lists.pop(server_name, None)
        self._problems_by_server.pop(server_name, None)
        if client is not None:
            await _close_quietly(client)

    async def _connect_one(self, config: McpServerConfig) -> None:
        """连一个服务器并把工具列表存好；失败记下问题，不挡住别的服务器。"""
        await self._drop_client(config.name)
        client = start_client(config)
        try:
            await client.start()
            self._tool_lists[config.name] = await client.list_tools()
        except Exception as error:
            # 一个服务器连不上不该拖垮别的服务器；报成问题，界面和命令行都能看到
            self._problems_by_server[config.name] = f"{type(error).__name__}: {error}"
            await _close_quietly(client)
            return
        self._clients[config.name] = client
        self._problems_by_server.pop(config.name, None)

    async def _restart(self, server_name: str) -> StdioMcpClient | HttpMcpClient:
        """服务器进程没了：重新连一次（拿锁，防止几个调用同时各自起一个进程）。"""
        async with self._connect_lock:
            config = _find_config(self.load().servers, server_name)
            if config is None or not config.enabled:
                raise McpError(f"MCP 服务器 {server_name} 不在配置里（或已停用）")
            await self._connect_one(config)
            client = self._clients.get(server_name)
            if client is None:
                raise McpError(
                    f"MCP 服务器 {server_name} 重连失败：{self._problems_by_server.get(server_name, '没有说明原因')}"
                )
            return client

    def public_overview(self) -> dict[str, Any]:
        """设置界面要的：每个服务器的配置（密钥脱敏）、连接状态、工具、问题。"""
        configs = self.load()
        statuses = {status.name: status for status in self.statuses()}
        servers = []
        for config in configs.servers:
            entry = config_to_public_dict(config)
            status = statuses.get(config.name)
            entry["connected"] = bool(status and status.connected)
            entry["tool_count"] = status.tool_count if status else 0
            entry["tools"] = [info.name for info in self._tool_lists.get(config.name) or []]
            entry["problem"] = self._problems_by_server.get(config.name, "")
            servers.append(entry)
        return {
            "config_path": str(mcp_config_path(self.home_dir)) if self.home_dir else "",
            "servers": servers,
            "problems": list(configs.problems),
        }

    def statuses(self) -> list[McpServerStatus]:
        result: list[McpServerStatus] = []
        for config in self.load().servers:
            if not config.enabled:
                continue
            client = self._clients.get(config.name)
            connected = client is not None and client.alive
            result.append(
                McpServerStatus(
                    name=config.name,
                    connected=connected,
                    tool_count=len(self._tool_lists.get(config.name) or []) if connected else 0,
                )
            )
        return result

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """把各服务器的工具包装成 McpToolBridge 注册进注册表（幂等：同一个注册表重复调用不加也不报重名）。"""
        problems: list[str] = []
        for config in self.load().servers:
            if not config.enabled:
                continue
            for info in self._tool_lists.get(config.name) or []:
                tool_name = bridged_name(config.name, info.name)
                if tool_name is None:
                    problems.append(
                        f"MCP 工具 {config.name} / {info.name} 的名字拼出来太长（超过 64 字符），没有注册"
                    )
                    continue
                existing = registry.get(tool_name)
                if existing is not None:
                    # 幂等：同一个注册表第二次 attach 时已经在了
                    if isinstance(existing, McpToolBridge) and existing.server_name == config.name:
                        continue
                    problems.append(f"MCP 工具 {tool_name} 和已有的工具重名，没有注册")
                    continue
                registry.register(McpToolBridge(self, config.name, info))
        return problems

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        on_progress: ProgressListener | None = None,
    ) -> McpCallResult:
        async def call_with_restart() -> McpCallResult:
            client = self._clients.get(server_name)
            if client is None or not client.alive:
                client = await self._restart(server_name)
            return await client.call_tool(tool_name, arguments, on_progress)

        return await self._on_manager_loop(call_with_restart())

    async def close(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or not loop.is_running():
            self._clients.clear()
            self._tool_lists.clear()
            return
        await self._on_manager_loop(self._close_clients())
        loop.call_soon_threadsafe(loop.stop)
        if thread is not threading.current_thread():
            thread.join(timeout=5)
        self._loop = None
        self._thread = None

    async def _close_clients(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        self._tool_lists.clear()
        for client in clients:
            await _close_quietly(client)


async def _close_quietly(client: StdioMcpClient | HttpMcpClient) -> None:
    try:
        await client.close()
    except Exception:
        pass


def _find_config(configs: list[McpServerConfig], server_name: str) -> McpServerConfig | None:
    for config in configs:
        if config.name == server_name:
            return config
    return None


# ---- 桥接成内置工具 ----


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

    def __init__(self, manager: McpManager, server_name: str, info: McpToolInfo) -> None:
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
