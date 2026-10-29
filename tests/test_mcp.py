"""MCP 客户端、工具桥接和 agent 集成的测试：全部跑本地的假服务器，不连网络。"""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from pocketagent.config import Settings
from pocketagent.core.agent import create_agent, shared_mcp_manager
from pocketagent.llm import LLMResponse, ScriptedProvider, ToolCall
from pocketagent.safety.permissions import AllowAllApprover, ApprovalMode
from pocketagent.tools.base import ToolContext, ToolRegistry, current_tool_output
from pocketagent.tools.mcp import (
    McpError,
    McpManager,
    McpServerConfig,
    bridged_name,
    config_to_public_dict,
    expand_env_value,
    load_mcp_servers,
    parse_call_result,
    save_mcp_servers,
)

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


def fake_config(name: str = "fake", **env: str) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=[str(FAKE_SERVER)],
        env={key: str(value) for key, value in env.items()},
        timeout_seconds=30,
    )


class McpTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.home = root / "home"
        self.home.mkdir()
        self.outputs = root / "tool_outputs"
        self.outputs.mkdir()
        self.managers: list[McpManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.close()
        self._temporary.cleanup()

    def make_manager(self, *configs: McpServerConfig) -> McpManager:
        manager = McpManager(configs=list(configs))
        self.managers.append(manager)
        return manager

    def tool_context(self) -> ToolContext:
        return ToolContext(workspace=self.home, tool_outputs_dir=self.outputs)

    async def connect_and_register(self, *configs: McpServerConfig) -> tuple[McpManager, ToolRegistry, list[str]]:
        manager = self.make_manager(*configs)
        problems = await manager.ensure_connected()
        registry = ToolRegistry()
        problems += manager.register_tools(registry)
        return manager, registry, problems


class McpConfigTests(unittest.TestCase):
    def test_expand_env_value(self) -> None:
        os.environ["MCP_TEST_TOKEN"] = "abc123"
        try:
            self.assertEqual(expand_env_value("Bearer ${MCP_TEST_TOKEN}"), "Bearer abc123")
            self.assertEqual(expand_env_value("${MCP_TEST_MISSING:-默认值}"), "默认值")
            self.assertEqual(expand_env_value("${MCP_TEST_MISSING}"), "")
            self.assertEqual(expand_env_value("没有变量"), "没有变量")
        finally:
            os.environ.pop("MCP_TEST_TOKEN", None)

    def test_load_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            os.environ["MCP_TEST_KEY"] = "secret"
            try:
                (home / "mcp.json").write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "docs": {
                                    "url": "https://example.com/mcp",
                                    "headers": {"Authorization": "Bearer ${MCP_TEST_KEY}"},
                                    "timeout_seconds": 30,
                                },
                                "stdio": {"command": "npx", "args": ["-y", "server"], "enabled": False},
                                "broken": {"args": []},
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                loaded = load_mcp_servers(home)
            finally:
                os.environ.pop("MCP_TEST_KEY", None)
        names = [config.name for config in loaded.servers]
        self.assertEqual(names, ["docs", "stdio"])
        docs = loaded.servers[0]
        self.assertEqual(docs.transport, "http")
        self.assertEqual(docs.headers["Authorization"], "Bearer secret")
        self.assertEqual(docs.timeout_seconds, 30)
        self.assertFalse(loaded.servers[1].enabled)
        self.assertTrue(any("broken" in problem for problem in loaded.problems))

    def test_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_mcp_servers(Path(directory))
        self.assertEqual(loaded.servers, [])
        self.assertEqual(loaded.problems, [])

    def test_bridged_name_sanitizes_and_limits(self) -> None:
        self.assertEqual(bridged_name("my server", "do.thing"), "mcp__my_server__do_thing")
        self.assertIsNone(bridged_name("s" * 40, "t" * 40))

    def test_parse_call_result(self) -> None:
        result = parse_call_result({"content": [{"type": "text", "text": "hi"}], "isError": True})
        self.assertEqual(result.content[0]["text"], "hi")
        self.assertTrue(result.is_error)
        empty = parse_call_result({})
        self.assertEqual(empty.content, [])
        self.assertFalse(empty.is_error)


class McpSettingsTests(McpTestCase):
    """设置界面的配置读写：密钥留空保留原值、坏配置不落盘、返回脱敏视图。"""

    async def test_save_keeps_secret_when_left_blank(self) -> None:
        (self.home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {
                            "command": "npx",
                            "args": ["-y", "server-github"],
                            "env": {"GITHUB_TOKEN": "ghp_old", "OTHER": "keep"},
                            "timeout_seconds": 30,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        existing = load_mcp_servers(self.home)
        saved = save_mcp_servers(
            self.home,
            [
                {
                    "name": "github",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "server-github"],
                    # 界面上看不到值：留空 = 不改；OTHER 从提交里消失了 = 删掉
                    "env": {"GITHUB_TOKEN": ""},
                    "timeout_seconds": 30,
                    "enabled": True,
                },
                {"name": "docs", "transport": "http", "url": "https://example.com/mcp", "enabled": False},
            ],
            existing,
        )
        by_name = {config.name: config for config in saved.servers}
        self.assertEqual(by_name["github"].env, {"GITHUB_TOKEN": "ghp_old"})
        self.assertFalse(by_name["docs"].enabled)
        written = json.loads((self.home / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(written["mcpServers"]["github"]["env"]["GITHUB_TOKEN"], "ghp_old")
        self.assertEqual(len(written["mcpServers"]), 2)

    async def test_save_rejects_bad_server_without_touching_file(self) -> None:
        (self.home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"keep": {"command": "npx"}}}), encoding="utf-8"
        )
        before = (self.home / "mcp.json").read_text(encoding="utf-8")
        with self.assertRaises(McpError):
            save_mcp_servers(self.home, [{"name": "broken"}], load_mcp_servers(self.home))
        self.assertEqual((self.home / "mcp.json").read_text(encoding="utf-8"), before)

    async def test_public_overview_masks_secret_values(self) -> None:
        config = fake_config(FAKE_ONLY_ECHO="1")
        config.env["GITHUB_TOKEN"] = "ghp_secret"
        manager = McpManager(home_dir=self.home, configs=[config])
        self.managers.append(manager)
        await manager.ensure_connected()
        manager.register_tools(ToolRegistry())
        overview = manager.public_overview()
        server = overview["servers"][0]
        self.assertEqual(server["env"]["GITHUB_TOKEN"], "")
        self.assertEqual(server["env_keys"] if "env_keys" in server else list(server["env"]), ["FAKE_ONLY_ECHO", "GITHUB_TOKEN"])
        self.assertTrue(server["connected"])
        self.assertEqual(server["tool_count"], 1)
        self.assertEqual(server["tools"], ["echo"])
        self.assertTrue(overview["config_path"].endswith("mcp.json"))

    async def test_config_to_public_dict_has_no_values(self) -> None:
        config = McpServerConfig(name="x", url="https://a", headers={"Authorization": "Bearer secret"})
        public = config_to_public_dict(config)
        self.assertEqual(public["headers"], {"Authorization": ""})
        self.assertEqual(public["transport"], "http")


class McpStdioTests(McpTestCase):
    async def test_list_and_call_text_tool(self) -> None:
        manager, registry, problems = await self.connect_and_register(fake_config())
        self.assertEqual(problems, [])
        self.assertIn("mcp__fake__echo", registry.names())
        self.assertEqual(len(registry.names()), 5)

        echo = registry.get("mcp__fake__echo")
        result = await echo.run({"text": "你好"}, self.tool_context())
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "你好")

    async def test_image_result_saved_and_reported(self) -> None:
        _, registry, _ = await self.connect_and_register(fake_config())
        picture = registry.get("mcp__fake__picture")
        result = await picture.run({}, self.tool_context())
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.images), 1)
        self.assertTrue(result.images[0].is_file())
        self.assertTrue(result.images[0].name.endswith(".png"))
        self.assertIn("图片已保存", result.content)

    async def test_error_result(self) -> None:
        _, registry, _ = await self.connect_and_register(fake_config())
        failure = registry.get("mcp__fake__fail")
        result = await failure.run({}, self.tool_context())
        self.assertTrue(result.is_error)
        self.assertIn("坏掉", result.content)

    async def test_progress_forwarded_to_tool_output(self) -> None:
        _, registry, _ = await self.connect_and_register(fake_config())
        chatty = registry.get("mcp__fake__chatty")
        chunks: list[str] = []

        async def listener(text: str) -> None:
            chunks.append(text)

        token = current_tool_output.set(listener)
        try:
            result = await chatty.run({}, self.tool_context())
        finally:
            current_tool_output.reset(token)
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "说完了")
        joined = "".join(chunks)
        self.assertIn("第 1 步", joined)
        self.assertIn("第 2 步", joined)
        self.assertIn("50", joined)

    async def test_call_timeout(self) -> None:
        config = fake_config(FAKE_SLEEP_SECONDS="3")
        config.timeout_seconds = 1
        _, registry, _ = await self.connect_and_register(config)
        sleepy = registry.get("mcp__fake__sleepy")
        result = await sleepy.run({}, self.tool_context())
        self.assertTrue(result.is_error)
        self.assertIn("超过", result.content)

    async def test_death_reports_error_then_reconnects(self) -> None:
        state_file = self.home / "died-once"
        config = fake_config(FAKE_DIE_ONCE="1", FAKE_STATE_FILE=str(state_file))
        manager, registry, _ = await self.connect_and_register(config)
        echo = registry.get("mcp__fake__echo")

        first = await echo.run({"text": "第一次"}, self.tool_context())
        self.assertTrue(first.is_error)
        self.assertIn("结束", first.content)

        # 下一次调用会自动重启服务器进程
        second = await echo.run({"text": "第二次"}, self.tool_context())
        self.assertFalse(second.is_error)
        self.assertEqual(second.content, "第二次")

    async def test_enabled_false_is_not_connected(self) -> None:
        config = fake_config()
        config.enabled = False
        manager = self.make_manager(config)
        problems = await manager.ensure_connected()
        self.assertEqual(problems, [])
        registry = ToolRegistry()
        self.assertEqual(manager.register_tools(registry), [])
        self.assertEqual(registry.names(), [])

    async def test_duplicate_bridged_names_skipped(self) -> None:
        # 两个服务器名清洗后一样（a.b 和 a_b），工具也同名 → 拼出来的注册名撞车，第二个跳过
        first = fake_config("a.b", FAKE_TOOL_NAME="c", FAKE_ONLY_ECHO="1")
        second = fake_config("a_b", FAKE_TOOL_NAME="c", FAKE_ONLY_ECHO="1")
        _, registry, problems = await self.connect_and_register(first, second)
        self.assertEqual(registry.names(), ["mcp__a_b__c"])
        self.assertTrue(any("重名" in problem for problem in problems))

    async def test_register_tools_is_idempotent(self) -> None:
        manager, registry, _ = await self.connect_and_register(fake_config())
        before = registry.names()
        self.assertEqual(manager.register_tools(registry), [])
        self.assertEqual(registry.names(), before)

    async def test_statuses(self) -> None:
        manager = self.make_manager(fake_config())
        await manager.ensure_connected()
        statuses = manager.statuses()
        self.assertEqual(len(statuses), 1)
        self.assertTrue(statuses[0].connected)
        self.assertEqual(statuses[0].tool_count, 5)


class McpAgentTests(McpTestCase):
    async def test_run_turn_attaches_mcp_tools_automatically(self) -> None:
        """不显式 attach：agent 开跑时自动连上、注册、进系统提示。"""
        (self.home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(FAKE_SERVER)]}}}),
            encoding="utf-8",
        )
        settings = Settings(
            workspace=self.home,
            home_dir=self.home,
            approval_mode=ApprovalMode.FULL_AUTO,
            skills_enabled=False,
        )
        provider = ScriptedProvider(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="mcp__fake__echo",
                            arguments={"text": "来自 MCP"},
                            raw_arguments=json.dumps({"text": "来自 MCP"}),
                        )
                    ]
                ),
                LLMResponse(text="拿到了"),
            ]
        )
        agent = create_agent(settings, approver=AllowAllApprover(), provider=provider)
        try:
            # 没显式 attach：工具是在 run_turn 开头自己接上的
            self.assertNotIn("mcp__fake__echo", agent.context.tools.names())
            final_text = await agent.run_turn("调用一下 MCP 工具")
            self.assertIn("mcp__fake__echo", agent.context.tools.names())
        finally:
            await shared_mcp_manager(settings.home_dir).close()

        self.assertEqual(final_text, "拿到了")
        tool_messages = [message for message in agent.context.messages if message.get("role") == "tool"]
        self.assertEqual(tool_messages[-1]["content"], "来自 MCP")
        system_prompt = str(agent.context.messages[0]["content"])
        self.assertIn("mcp__fake__echo", system_prompt)

    async def test_reload_tools_keeps_mcp_tools(self) -> None:
        (self.home / "mcp.json").write_text(
            json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(FAKE_SERVER)]}}}),
            encoding="utf-8",
        )
        settings = Settings(workspace=self.home, home_dir=self.home, skills_enabled=False)
        agent = create_agent(settings, approver=AllowAllApprover(), provider=ScriptedProvider([]))
        try:
            await agent.attach_mcp(shared_mcp_manager(settings.home_dir))
            self.assertIn("mcp__fake__echo", agent.context.tools.names())
            agent.reload_tools()
            self.assertIn("mcp__fake__echo", agent.context.tools.names())
        finally:
            await shared_mcp_manager(settings.home_dir).close()


class _FakeMcpHttpHandler(http.server.BaseHTTPRequestHandler):
    """本机回环上的假 MCP 服务：JSON 和 SSE 两种回应都覆盖到。"""

    server_version = "FakeMcp/1"

    def log_message(self, format: str, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        session_id = self.headers.get("mcp-session-id")
        if session_id:
            self.server.received_session_ids.append(session_id)  # type: ignore[attr-defined]
        method = message.get("method")
        if method == "initialize":
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-http", "version": "0"},
                    },
                },
                extra_headers={"mcp-session-id": "session-1"},
            )
        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif method == "tools/list":
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "原样返回",
                                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                            }
                        ]
                    },
                }
            )
        elif method == "tools/call":
            params = message.get("params") or {}
            token = (params.get("_meta") or {}).get("progressToken")
            progress = {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 60, "message": "HTTP 进度"},
            }
            answer = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": (params.get("arguments") or {}).get("text", "")}]
                },
            }
            body = (
                f"event: message\ndata: {json.dumps(progress)}\n\n"
                f"event: message\ndata: {json.dumps(answer)}\n\n"
            )
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        else:
            self._send_json(
                {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": method}}
            )

    def _send_json(self, payload: dict, extra_headers: dict | None = None) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)


class McpHttpTests(McpTestCase):
    """streamable HTTP：本机起一个标准库的 HTTP 服务，验证 JSON / SSE 回应和会话头。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeMcpHttpHandler)
        self.http_server.received_session_ids = []  # type: ignore[attr-defined]
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        port = self.http_server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/mcp"

    async def asyncTearDown(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        await super().asyncTearDown()

    async def test_http_transport_end_to_end(self) -> None:
        config = McpServerConfig(name="http", url=self.url, timeout_seconds=10)
        manager, registry, problems = await self.connect_and_register(config)
        self.assertEqual(problems, [])
        echo = registry.get("mcp__http__echo")

        chunks: list[str] = []

        async def listener(text: str) -> None:
            chunks.append(text)

        token = current_tool_output.set(listener)
        try:
            result = await echo.run({"text": "走 HTTP"}, self.tool_context())
        finally:
            current_tool_output.reset(token)
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "走 HTTP")
        self.assertIn("HTTP 进度", "".join(chunks))
        self.assertIn("session-1", self.http_server.received_session_ids)


if __name__ == "__main__":
    unittest.main()
