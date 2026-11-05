"""连接管理：McpManager 负责各个 MCP 服务器的连接、重连和关闭。

连接都跑在一条专用线程的事件循环上；工具列表连上时缓存下来，会话只管把桥接工具注册进自己的注册表。
"""

import asyncio
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocketagent.tools.base import ToolRegistry
from pocketagent.tools.mcp.bridge import McpToolBridge, bridged_name
from pocketagent.tools.mcp.config import (
    McpConfigs,
    McpServerConfig,
    config_to_public_dict,
    load_mcp_servers,
    mcp_config_path,
)
from pocketagent.tools.mcp.http_transport import HttpMcpClient
from pocketagent.tools.mcp.protocol import McpCallResult, McpError, McpToolInfo, ProgressListener
from pocketagent.tools.mcp.stdio_transport import StdioMcpClient


def start_client(config: McpServerConfig) -> StdioMcpClient | HttpMcpClient:
    if config.transport == "http":
        return HttpMcpClient(config)
    return StdioMcpClient(config)


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
