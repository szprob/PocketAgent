import asyncio
import unittest
from typing import Any, Callable

try:
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("没装 aiohttp，跳过 web 测试")

import json
import socket
from unittest import mock

from aiohttp import web

from pocketagent.agent import INTERRUPTED_TOOL_RESULT
from pocketagent.config import AppConfig, Settings
from pocketagent.llm import LLMResponse, ScriptedProvider
from pocketagent.permissions import ApprovalMode
from pocketagent.web import server
from pocketagent.web.server import MANAGER_KEY, create_app
from tests.helpers import TemporaryDirectories, tool_call

TOKEN = "test-token"
AUTH = {"token": TOKEN}
DS_PROFILE = {"id": "ds", "name": "DS", "model": "deepseek-v4.1-flash", "api_key": "sk-ds-secret-key-123"}
QWEN_PROFILE = {"id": "qwen", "name": "Qwen", "model": "Qwen3.8-27B", "reasoning_effort": "medium"}


class WebServerTest(AioHTTPTestCase):
    async def get_application(self):
        self.directories = TemporaryDirectories()
        # 每个测试在建会话前设好剧本；会话创建时用它造假模型
        self.responses: list[LLMResponse] = []
        # 每次造 provider 时的设置，用来断言用的是哪个模型、哪个密钥
        self.provider_settings: list[Settings] = []
        settings = Settings(
            workspace=self.directories.workspace,
            home_dir=self.directories.home_dir,
            approval_mode=ApprovalMode.ASK,
        )
        app_config = AppConfig.from_dict({"models": [DS_PROFILE, QWEN_PROFILE], "default_model": "ds"})
        return create_app(settings, token=TOKEN, provider_factory=self._make_provider, app_config=app_config)

    def _make_provider(self, settings: Settings) -> ScriptedProvider:
        self.provider_settings.append(settings)
        return ScriptedProvider(self.responses)

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        self.directories.cleanup()

    async def _create_session(self) -> str:
        response = await self.client.post("/api/sessions", params=AUTH)
        return (await response.json())["id"]

    async def _receive_until(self, socket, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(socket.receive_json(), timeout=5)
            if predicate(message):
                return message

    @staticmethod
    def _is_event(event_type: str) -> Callable[[dict[str, Any]], bool]:
        def predicate(message: dict[str, Any]) -> bool:
            return message["type"] == "event" and message["event"]["type"] == event_type

        return predicate

    @staticmethod
    def _is_idle_state(message: dict[str, Any]) -> bool:
        return message["type"] == "state" and not message["running"]

    async def test_token_is_required_and_remembered_by_cookie(self) -> None:
        without_token = await self.client.get("/api/info")
        with_token = await self.client.get("/", params=AUTH)
        static_file_by_cookie = await self.client.get("/static/app.js")

        self.assertEqual(without_token.status, 401)
        self.assertEqual(with_token.status, 200)
        self.assertIn("PocketAgent", await with_token.text())
        self.assertEqual(static_file_by_cookie.status, 200)

    async def test_info_and_unknown_session(self) -> None:
        info = await (await self.client.get("/api/info", params=AUTH)).json()
        missing = await self.client.get("/api/sessions/nope", params=AUTH)

        self.assertEqual(info["default_mode"], "ask")
        self.assertIn("full_auto", info["modes"])
        self.assertEqual(missing.status, 404)

    async def test_turn_with_approval_over_websocket(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("call_1", "write_file", '{"path": "a.txt", "content": "x"}')]),
                LLMResponse(text="写好了"),
            ]
        )
        session_id = await self._create_session()

        async with self.client.ws_connect(f"/ws/{session_id}", params=AUTH) as socket:
            snapshot = await socket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["items"], [])

            await socket.send_json({"type": "user_message", "text": "写个文件"})
            approval = await self._receive_until(socket, lambda message: message["type"] == "approval_request")
            self.assertIn("新建 a.txt", approval["preview"])
            await socket.send_json({"type": "approval", "request_id": approval["request_id"], "answer": "allow_once"})
            turn_end = await self._receive_until(socket, self._is_event("turn_end"))
            await self._receive_until(socket, self._is_idle_state)

        self.assertEqual(turn_end["event"]["final_text"], "写好了")
        self.assertEqual((self.directories.workspace / "a.txt").read_text(encoding="utf-8"), "x")
        history = await (await self.client.get(f"/api/sessions/{session_id}", params=AUTH)).json()
        self.assertEqual([item["kind"] for item in history["items"]], ["user", "tool", "assistant"])
        self.assertEqual(history["items"][1]["result"], "已新建 a.txt")
        sessions = await (await self.client.get("/api/sessions", params=AUTH)).json()
        self.assertEqual(sessions["sessions"][0]["title"], "写个文件")

    async def test_interrupt_while_waiting_for_approval(self) -> None:
        self.responses.append(LLMResponse(tool_calls=[tool_call("call_1", "bash", '{"command": "touch never"}')]))
        session_id = await self._create_session()

        async with self.client.ws_connect(f"/ws/{session_id}", params=AUTH) as socket:
            await socket.receive_json()
            await socket.send_json({"type": "user_message", "text": "跑个命令"})
            await self._receive_until(socket, lambda message: message["type"] == "approval_request")

            await socket.send_json({"type": "user_message", "text": "插一句"})
            busy_error = await self._receive_until(socket, lambda message: message["type"] == "error")
            self.assertIn("还没结束", busy_error["message"])

            await socket.send_json({"type": "interrupt"})
            await self._receive_until(socket, lambda message: message["type"] == "approval_resolved")
            await self._receive_until(socket, self._is_idle_state)

        self.assertFalse((self.directories.workspace / "never").exists())
        history = await (await self.client.get(f"/api/sessions/{session_id}", params=AUTH)).json()
        self.assertEqual(history["items"][-1]["result"], INTERRUPTED_TOOL_RESULT)

    async def test_set_mode_is_broadcast(self) -> None:
        session_id = await self._create_session()

        async with self.client.ws_connect(f"/ws/{session_id}", params=AUTH) as socket:
            await socket.receive_json()
            await socket.send_json({"type": "set_mode", "mode": "auto_edit"})
            state = await self._receive_until(socket, lambda message: message["type"] == "state")
            await socket.send_json({"type": "set_mode", "mode": "bogus"})
            error = await self._receive_until(socket, lambda message: message["type"] == "error")

        self.assertEqual(state["mode"], "auto_edit")
        self.assertIn("bogus", error["message"])

    async def _get_json(self, path: str) -> dict[str, Any]:
        return await (await self.client.get(path, params=AUTH)).json()

    async def _put_config(self, payload: dict[str, Any]):
        return await self.client.put("/api/config", params=AUTH, json=payload)

    async def test_config_hides_key_and_keeps_it_when_left_blank(self) -> None:
        config = await self._get_json("/api/config")
        self.assertNotIn("api_key", config["models"][0])
        self.assertEqual(config["models"][0]["api_key_hint"], "sk-ds-…-123")

        renamed = dict(DS_PROFILE, name="DS 改名", api_key="")
        response = await self._put_config({"models": [renamed, QWEN_PROFILE], "default_model": "ds"})
        saved_on_disk = json.loads((self.directories.home_dir / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(saved_on_disk["models"][0]["name"], "DS 改名")
        self.assertEqual(saved_on_disk["models"][0]["api_key"], "sk-ds-secret-key-123")

        cleared = dict(DS_PROFILE, api_key="", clear_api_key=True)
        await self._put_config({"models": [cleared, QWEN_PROFILE]})
        config = await self._get_json("/api/config")
        self.assertEqual(config["models"][0]["api_key_hint"], "")

    async def test_invalid_config_is_rejected(self) -> None:
        response = await self._put_config({"models": [{"name": "没有模型名"}]})

        self.assertEqual(response.status, 400)
        self.assertIn("模型名不能为空", (await response.json())["error"])

    async def test_connection_test_uses_saved_key_when_left_blank(self) -> None:
        self.responses.append(LLMResponse(text="收到"))

        response = await self.client.post("/api/config/test", params=AUTH, json=dict(DS_PROFILE, api_key=""))
        result = await response.json()

        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "收到")
        self.assertEqual(self.provider_settings[-1].api_key, "sk-ds-secret-key-123")

    async def test_switch_model_for_one_session(self) -> None:
        session_id = await self._create_session()

        async with self.client.ws_connect(f"/ws/{session_id}", params=AUTH) as socket_client:
            snapshot = await socket_client.receive_json()
            await socket_client.send_json({"type": "set_model", "model_id": "qwen"})
            state = await self._receive_until(socket_client, lambda message: message["type"] == "state")
            await socket_client.send_json({"type": "set_model", "model_id": "nope"})
            error = await self._receive_until(socket_client, lambda message: message["type"] == "error")

        self.assertEqual(snapshot["model_id"], "ds")
        self.assertEqual(state["model_id"], "qwen")
        self.assertEqual(self.provider_settings[-1].model, "Qwen3.8-27B")
        self.assertEqual(self.provider_settings[-1].reasoning_effort, "medium")
        self.assertIn("没有这个模型", error["message"])

    async def test_resumed_session_keeps_its_model(self) -> None:
        self.responses.append(LLMResponse(text="好"))
        session_id = await self._create_session()
        async with self.client.ws_connect(f"/ws/{session_id}", params=AUTH) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "set_model", "model_id": "qwen"})
            await socket_client.send_json({"type": "user_message", "text": "你好"})
            await self._receive_until(socket_client, self._is_event("turn_end"))

        # 模拟服务重启：内存里的会话清掉，从磁盘重新加载
        self.app[MANAGER_KEY].sessions.clear()
        snapshot = await self._get_json(f"/api/sessions/{session_id}")

        self.assertEqual(snapshot["model_id"], "qwen")

    async def test_saving_config_moves_sessions_off_deleted_model(self) -> None:
        session_id = await self._create_session()

        await self._put_config({"models": [QWEN_PROFILE], "max_steps": 7})
        snapshot = await self._get_json(f"/api/sessions/{session_id}")
        info = await self._get_json("/api/info")

        self.assertEqual(snapshot["model_id"], "qwen")
        self.assertEqual(self.app[MANAGER_KEY].sessions[session_id].settings.max_steps, 7)
        self.assertEqual([model["id"] for model in info["models"]], ["qwen"])


class StartupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.settings = Settings(workspace=self.directories.workspace, home_dir=self.directories.home_dir)

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_busy_port_moves_to_next_one_unless_port_was_given(self) -> None:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            busy_port = occupied.getsockname()[1]
            runner = web.AppRunner(create_app(self.settings, token=""))
            await runner.setup()
            try:
                with mock.patch.object(server, "DEFAULT_PORT", busy_port):
                    bound_port = await server._start_site(runner, "127.0.0.1", None)
                with self.assertRaises(SystemExit):
                    await server._start_site(runner, "127.0.0.1", busy_port)
            finally:
                await runner.cleanup()

        self.assertGreater(bound_port, busy_port)

    def test_token_is_generated_once_and_saved(self) -> None:
        first = server._resolve_token(self.settings, AppConfig(), token=None, require_token=True)
        second = server._resolve_token(
            self.settings, AppConfig.load(self.settings.config_path), token=None, require_token=True
        )
        explicit = server._resolve_token(self.settings, AppConfig(), token="mine", require_token=True)
        disabled = server._resolve_token(self.settings, AppConfig(), token=None, require_token=False)

        self.assertEqual(first, second)
        self.assertEqual(explicit, "mine")
        self.assertEqual(disabled, "")


if __name__ == "__main__":
    unittest.main()
