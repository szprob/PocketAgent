import asyncio
import base64
import json
import socket
import unittest
from typing import Any, Callable
from unittest import mock

try:
    from aiohttp import FormData, web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("没装 aiohttp，跳过 web 测试")

from pocketagent.agent import INTERRUPTED_TOOL_RESULT
from pocketagent.config import AppConfig, Settings
from pocketagent.llm import LLMResponse, ScriptedProvider, Usage
from pocketagent.permissions import ApprovalMode
from pocketagent.web import server
from pocketagent.web.server import MANAGER_KEY, create_app
from tests.helpers import TemporaryDirectories, tool_call

TOKEN = "test-token"
AUTH = {"token": TOKEN}
DS_PROFILE = {
    "id": "ds",
    "name": "DS",
    "model": "deepseek-v4.1-flash",
    "api_key": "sk-ds-secret-key-123",
    "supports_images": True,
}
QWEN_PROFILE = {"id": "qwen", "name": "Qwen", "model": "Qwen3.8-27B", "reasoning_effort": "medium"}
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class GatedProvider:
    """第一次调用要等 gate 打开才返回，用来在 agent 还在跑的时候插话、排队。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.gate = asyncio.Event()
        self.inner = ScriptedProvider(responses)
        self.call_count = 0

    @property
    def received_messages(self) -> list[list[dict[str, Any]]]:
        return self.inner.received_messages

    async def complete(self, messages, tools, listener) -> LLMResponse:
        self.call_count += 1
        if self.call_count == 1:
            await self.gate.wait()
        return await self.inner.complete(messages, tools, listener)


def is_type(message_type: str) -> Callable[[dict[str, Any]], bool]:
    def predicate(message: dict[str, Any]) -> bool:
        return message["type"] == message_type

    return predicate


def is_event(event_type: str) -> Callable[[dict[str, Any]], bool]:
    def predicate(message: dict[str, Any]) -> bool:
        return message["type"] == "event" and message["event"]["type"] == event_type

    return predicate


def is_idle_state(message: dict[str, Any]) -> bool:
    return message["type"] == "state" and not message["running"]


def is_running_state(message: dict[str, Any]) -> bool:
    return message["type"] == "state" and message["running"]


class WebServerTest(AioHTTPTestCase):
    async def get_application(self):
        self.directories = TemporaryDirectories()
        # 每个测试在建任务前设好剧本；建任务时用它造假模型
        self.responses: list[LLMResponse] = []
        # 想用特别的假模型（比如 GatedProvider）时设这个
        self.provider_override = None
        # 每次造 provider 时的设置，用来断言用的是哪个模型、哪个密钥
        self.provider_settings: list[Settings] = []
        self.scripted_providers: list[ScriptedProvider] = []
        settings = Settings(
            workspace=self.directories.workspace,
            home_dir=self.directories.home_dir,
            approval_mode=ApprovalMode.ASK,
        )
        app_config = AppConfig.from_dict({"models": [DS_PROFILE, QWEN_PROFILE], "default_model": "ds"})
        return create_app(settings, token=TOKEN, provider_factory=self._make_provider, app_config=app_config)

    def _make_provider(self, settings: Settings):
        self.provider_settings.append(settings)
        if self.provider_override is not None:
            return self.provider_override
        provider = ScriptedProvider(self.responses)
        self.scripted_providers.append(provider)
        return provider

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        self.directories.cleanup()

    # ---- 小工具 ----

    async def _get_json(self, path: str) -> Any:
        return await (await self.client.get(path, params=AUTH)).json()

    async def _send_json(self, method: str, path: str, payload: Any):
        return await self.client.request(method, path, params=AUTH, json=payload)

    async def _create_session(self, **payload) -> str:
        response = await self._send_json("POST", "/api/sessions", payload)
        return (await response.json())["id"]

    async def _receive_until(self, socket_client, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(socket_client.receive_json(), timeout=5)
            if predicate(message):
                return message

    def _connect(self, session_id: str):
        return self.client.ws_connect(f"/ws/{session_id}", params=AUTH)

    # ---- 鉴权、基本信息 ----

    async def test_token_is_required_and_remembered_by_cookie(self) -> None:
        without_token = await self.client.get("/api/info")
        with_token = await self.client.get("/", params=AUTH)
        static_file_by_cookie = await self.client.get("/static/js/main.js")

        self.assertEqual(without_token.status, 401)
        self.assertEqual(with_token.status, 200)
        self.assertIn("PocketAgent", await with_token.text())
        self.assertEqual(static_file_by_cookie.status, 200)

    async def test_info_lists_three_web_modes_and_models(self) -> None:
        info = await self._get_json("/api/info")
        missing = await self.client.get("/api/sessions/nope", params=AUTH)

        self.assertEqual(info["modes"], ["ask", "ai_auto", "full_auto"])
        self.assertEqual([model["id"] for model in info["models"]], ["ds", "qwen"])
        self.assertTrue(info["models"][0]["supports_images"])
        self.assertEqual(info["busy_input_mode"], "interject")
        self.assertEqual(missing.status, 404)

    # ---- 一轮对话和审批 ----

    async def test_turn_with_approval_over_websocket(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("call_1", "write_file", '{"path": "a.txt", "content": "x"}')]),
                LLMResponse(text="写好了", usage=Usage(prompt_tokens=100, completion_tokens=7)),
            ]
        )
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            snapshot = await socket_client.receive_json()
            self.assertEqual(snapshot["items"], [])
            await socket_client.send_json({"type": "user_message", "text": "写个文件"})
            approval = await self._receive_until(socket_client, is_type("approval_request"))
            self.assertIn("新建 a.txt", approval["preview"])
            await socket_client.send_json({"type": "approval", "request_id": approval["request_id"], "answer": "allow_once"})
            turn_end = await self._receive_until(socket_client, is_event("turn_end"))
            await self._receive_until(socket_client, is_idle_state)

        self.assertEqual(turn_end["event"]["final_text"], "写好了")
        self.assertEqual((self.directories.workspace / "a.txt").read_text(encoding="utf-8"), "x")
        history = await self._get_json(f"/api/sessions/{session_id}")
        self.assertEqual([item["kind"] for item in history["items"]], ["user", "tool", "assistant"])
        self.assertEqual(history["items"][1]["result"], "已新建 a.txt")
        self.assertEqual(history["title"], "写个文件")
        tasks = await self._get_json("/api/sessions")
        self.assertEqual(tasks["sessions"][0]["title"], "写个文件")
        # 第一次调用没报 usage，按估算记；第二次 107
        self.assertGreater(history["usage"]["total"], 107)

    async def test_interrupt_returns_pending_inputs_to_the_composer(self) -> None:
        self.responses.append(LLMResponse(tool_calls=[tool_call("call_1", "bash", '{"command": "touch never"}')]))
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "跑个命令"})
            await self._receive_until(socket_client, is_type("approval_request"))
            await socket_client.send_json({"type": "user_message", "text": "插一句"})
            await self._receive_until(socket_client, is_event("interjection_queued"))

            await socket_client.send_json({"type": "interrupt"})
            returned = await self._receive_until(socket_client, is_type("returned_inputs"))
            await self._receive_until(socket_client, is_type("approval_resolved"))
            await self._receive_until(socket_client, is_idle_state)

        self.assertEqual([item["text"] for item in returned["inputs"]], ["插一句"])
        self.assertFalse((self.directories.workspace / "never").exists())
        history = await self._get_json(f"/api/sessions/{session_id}")
        self.assertEqual(history["items"][-1]["result"], INTERRUPTED_TOOL_RESULT)

    async def test_interjection_reaches_the_model_mid_turn(self) -> None:
        gated = GatedProvider(
            [
                LLMResponse(tool_calls=[tool_call("call_1", "list_dir", "{}")]),
                LLMResponse(text="好的，按你说的改"),
            ]
        )
        self.provider_override = gated
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "看看目录"})
            await self._receive_until(socket_client, is_running_state)
            await socket_client.send_json({"type": "user_message", "text": "顺便看看 README"})
            await self._receive_until(socket_client, is_event("interjection_queued"))
            gated.gate.set()
            applied = await self._receive_until(socket_client, is_event("interjection_applied"))
            await self._receive_until(socket_client, is_event("turn_end"))

        second_call = gated.received_messages[1]
        self.assertEqual(applied["event"]["user_text"], "顺便看看 README")
        self.assertEqual([message["role"] for message in second_call[-2:]], ["tool", "user"])
        self.assertEqual(second_call[-1]["content"], "顺便看看 README")

    async def test_queue_mode_runs_the_next_message_after_the_turn(self) -> None:
        await self._send_json("PUT", "/api/config", {"busy_input_mode": "queue"})
        gated = GatedProvider([LLMResponse(text="第一轮"), LLMResponse(text="第二轮")])
        self.provider_override = gated
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "一"})
            await self._receive_until(socket_client, is_running_state)
            await socket_client.send_json({"type": "user_message", "text": "二"})
            queued_state = await self._receive_until(
                socket_client, lambda message: message["type"] == "state" and message["queued"]
            )
            gated.gate.set()
            first_end = await self._receive_until(socket_client, is_event("turn_end"))
            second_start = await self._receive_until(socket_client, is_event("turn_start"))
            second_end = await self._receive_until(socket_client, is_event("turn_end"))

        self.assertEqual([item["text"] for item in queued_state["queued"]], ["二"])
        self.assertEqual(first_end["event"]["final_text"], "第一轮")
        self.assertEqual(second_start["event"]["user_text"], "二")
        self.assertEqual(second_end["event"]["final_text"], "第二轮")

    async def test_ai_review_allows_safe_calls_and_escalates_risky_ones(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("call_1", "write_file", '{"path": "ok.txt", "content": "x"}')]),
                LLMResponse(text='{"allow": true, "reason": "新建项目文件"}'),
                LLMResponse(tool_calls=[tool_call("call_2", "bash", '{"command": "rm -rf /tmp/x"}')]),
                LLMResponse(text='{"allow": false, "reason": "要删目录"}'),
            ]
        )
        session_id = await self._create_session(mode="ai_auto")

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "建个文件再清理"})
            allowed = await self._receive_until(socket_client, is_event("tool_review"))
            refused = await self._receive_until(socket_client, is_event("tool_review"))
            approval = await self._receive_until(socket_client, is_type("approval_request"))
            await socket_client.send_json({"type": "approval", "request_id": approval["request_id"], "answer": "deny"})
            await self._receive_until(socket_client, is_event("turn_end"))

        self.assertTrue(allowed["event"]["allow"])
        self.assertFalse(refused["event"]["allow"])
        self.assertIn("AI 审批没有放行：要删目录", approval["summary"])
        self.assertTrue((self.directories.workspace / "ok.txt").exists())
        history = await self._get_json(f"/api/sessions/{session_id}")
        tools = [item for item in history["items"] if item["kind"] == "tool"]
        self.assertEqual(tools[0]["review"], {"allow": True, "reason": "新建项目文件"})
        self.assertTrue(tools[1]["denied"])

    # ---- 附件 ----

    async def _upload(self, session_id: str, files: dict[str, bytes]) -> list[dict[str, Any]]:
        form = FormData()
        for name, data in files.items():
            form.add_field("file", data, filename=name)
        response = await self.client.post(f"/api/sessions/{session_id}/attachments", params=AUTH, data=form)
        return (await response.json())["attachments"]

    async def test_attachments_reach_the_model(self) -> None:
        self.responses.append(LLMResponse(text="看到了"))
        session_id = await self._create_session()
        uploaded = await self._upload(session_id, {"shot.png": TINY_PNG, "notes.txt": "要点：先修 bug".encode("utf-8")})

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json(
                {"type": "user_message", "text": "看下附件", "attachment_ids": [item["id"] for item in uploaded]}
            )
            await self._receive_until(socket_client, is_event("turn_end"))

        sent = self.scripted_providers[-1].received_messages[0][-1]
        self.assertEqual(sent["content"][0]["text"], "看下附件")
        self.assertTrue(sent["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("要点：先修 bug", sent["content"][-1]["text"])
        history = await self._get_json(f"/api/sessions/{session_id}")
        self.assertEqual(history["items"][0]["text"], "看下附件")
        self.assertEqual([item["kind"] for item in history["items"][0]["attachments"]], ["image", "text"])
        image = await self.client.get(f"/api/sessions/{session_id}/attachments/{uploaded[0]['id']}", params=AUTH)
        self.assertEqual(await image.read(), TINY_PNG)

    # ---- 文件变更 ----

    async def test_file_changes_diff_and_revert(self) -> None:
        (self.directories.workspace / "app.py").write_text("print('v1')\n", encoding="utf-8")
        self.responses.extend(
            [
                LLMResponse(
                    tool_calls=[
                        tool_call("c1", "edit_file", '{"path": "app.py", "old_string": "v1", "new_string": "v2"}'),
                        tool_call("c2", "write_file", '{"path": "new.md", "content": "# 新文件"}'),
                    ]
                ),
                LLMResponse(text="改好了"),
            ]
        )
        session_id = await self._create_session(mode="full_auto")
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "改一下"})
            await self._receive_until(socket_client, is_event("turn_end"))
            await self._receive_until(socket_client, is_idle_state)

        changes = (await self._get_json(f"/api/sessions/{session_id}/changes"))["changes"]
        by_path = {change["display_path"]: change for change in changes}
        app_path = by_path["app.py"]["path"]
        diff = await self.client.get(f"/api/sessions/{session_id}/changes/diff", params={**AUTH, "path": app_path})
        await self._send_json("POST", f"/api/sessions/{session_id}/changes/revert", {"path": app_path})
        await self._send_json("POST", f"/api/sessions/{session_id}/changes/revert", {"path": by_path["new.md"]["path"]})

        self.assertEqual(by_path["app.py"]["status"], "modified")
        self.assertEqual(by_path["new.md"]["status"], "added")
        self.assertIn("+print('v2')", (await diff.json())["diff"])
        self.assertEqual((self.directories.workspace / "app.py").read_text(encoding="utf-8"), "print('v1')\n")
        self.assertFalse((self.directories.workspace / "new.md").exists())

    # ---- 工作区、任务、用量 ----

    async def test_workspaces_and_task_management(self) -> None:
        other_workspace = self.directories.home_dir / "other"
        other_workspace.mkdir(parents=True)
        bad = await self._send_json("POST", "/api/workspaces", {"path": "/no/such/dir"})
        await self._send_json("POST", "/api/workspaces", {"path": str(other_workspace)})
        session_id = await self._create_session(workspace=str(other_workspace), model_id="qwen")

        await self._send_json("PATCH", f"/api/sessions/{session_id}", {"title": "改名后的任务"})
        snapshot = await self._get_json(f"/api/sessions/{session_id}")
        info = await self._get_json("/api/info")
        deleted = await self._send_json("DELETE", f"/api/sessions/{session_id}", {})
        after_delete = await self.client.get(f"/api/sessions/{session_id}", params=AUTH)

        self.assertEqual(bad.status, 400)
        self.assertIn(str(other_workspace.resolve()), [item["path"] for item in info["workspaces"]])
        self.assertEqual(snapshot["workspace"], str(other_workspace.resolve()))
        self.assertEqual(snapshot["model_id"], "qwen")
        self.assertEqual(snapshot["title"], "改名后的任务")
        self.assertEqual(deleted.status, 200)
        self.assertEqual(after_delete.status, 404)

    async def test_usage_summary(self) -> None:
        self.responses.append(LLMResponse(text="好", usage=Usage(prompt_tokens=40, completion_tokens=2)))
        session_id = await self._create_session()
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "你好"})
            await self._receive_until(socket_client, is_event("turn_end"))

        usage = await self._get_json("/api/usage")

        self.assertEqual(usage["periods"][0]["total"], 42)
        self.assertEqual(usage["by_model"][0]["model"], "DS")

    # ---- 设置 ----

    async def test_config_hides_key_and_keeps_it_when_left_blank(self) -> None:
        config = await self._get_json("/api/config")
        self.assertNotIn("api_key", config["models"][0])
        self.assertEqual(config["models"][0]["api_key_hint"], "sk-ds-…-123")

        renamed = dict(DS_PROFILE, name="DS 改名", api_key="")
        response = await self._send_json("PUT", "/api/config", {"models": [renamed, QWEN_PROFILE], "user_name": "宋"})
        saved_on_disk = json.loads((self.directories.home_dir / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(response.status, 200)
        self.assertEqual(saved_on_disk["models"][0]["name"], "DS 改名")
        self.assertEqual(saved_on_disk["models"][0]["api_key"], "sk-ds-secret-key-123")
        self.assertEqual(saved_on_disk["user_name"], "宋")

        cleared = dict(DS_PROFILE, api_key="", clear_api_key=True)
        await self._send_json("PUT", "/api/config", {"models": [cleared, QWEN_PROFILE]})
        config = await self._get_json("/api/config")
        self.assertEqual(config["models"][0]["api_key_hint"], "")

    async def test_invalid_config_is_rejected(self) -> None:
        response = await self._send_json("PUT", "/api/config", {"models": [{"name": "没有模型名"}]})
        bad_mode = await self._send_json("PUT", "/api/config", {"busy_input_mode": "shout"})

        self.assertEqual(response.status, 400)
        self.assertIn("模型名不能为空", (await response.json())["error"])
        self.assertEqual(bad_mode.status, 400)

    async def test_connection_test_uses_saved_key_when_left_blank(self) -> None:
        self.responses.append(LLMResponse(text="收到"))

        response = await self._send_json("POST", "/api/config/test", dict(DS_PROFILE, api_key=""))
        result = await response.json()

        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "收到")
        self.assertEqual(self.provider_settings[-1].api_key, "sk-ds-secret-key-123")

    async def test_switch_model_and_mode_for_one_session(self) -> None:
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            snapshot = await socket_client.receive_json()
            await socket_client.send_json({"type": "set_model", "model_id": "qwen"})
            model_state = await self._receive_until(socket_client, is_type("state"))
            await socket_client.send_json({"type": "set_mode", "mode": "ai_auto"})
            mode_state = await self._receive_until(socket_client, is_type("state"))
            await socket_client.send_json({"type": "set_model", "model_id": "nope"})
            error = await self._receive_until(socket_client, is_type("error"))

        self.assertEqual(snapshot["model_id"], "ds")
        self.assertEqual(model_state["model_id"], "qwen")
        self.assertEqual(mode_state["mode"], "ai_auto")
        self.assertEqual(self.provider_settings[-1].model, "Qwen3.8-27B")
        self.assertIn("没有这个模型", error["message"])

    async def test_resumed_session_keeps_its_model_and_mode(self) -> None:
        self.responses.append(LLMResponse(text="好"))
        session_id = await self._create_session(model_id="qwen", mode="full_auto")
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "你好"})
            await self._receive_until(socket_client, is_event("turn_end"))

        # 模拟服务重启：内存里的任务清掉，从磁盘重新加载
        self.app[MANAGER_KEY].sessions.clear()
        snapshot = await self._get_json(f"/api/sessions/{session_id}")

        self.assertEqual(snapshot["model_id"], "qwen")
        self.assertEqual(snapshot["mode"], "full_auto")

    async def test_saving_config_moves_sessions_off_deleted_model(self) -> None:
        session_id = await self._create_session()

        await self._send_json("PUT", "/api/config", {"models": [QWEN_PROFILE], "max_steps": 7})
        snapshot = await self._get_json(f"/api/sessions/{session_id}")

        self.assertEqual(snapshot["model_id"], "qwen")
        self.assertEqual(self.app[MANAGER_KEY].sessions[session_id].settings.max_steps, 7)

    # ---- 自动化 ----

    async def test_automation_create_run_and_record_result(self) -> None:
        self.responses.append(LLMResponse(text="日报写好了"))
        form = {
            "name": "日报",
            "workspace": str(self.directories.workspace),
            "prompt": "写日报",
            "approval_mode": "full_auto",
            "schedule_kind": "daily",
            "daily_time": "18:00",
            "weekdays": [0, 1, 2, 3, 4],
        }
        created = await (await self._send_json("POST", "/api/automations", form)).json()
        automation_id = created["automations"][0]["id"]
        rejected = await self._send_json("POST", "/api/automations", dict(form, approval_mode="ask"))

        run = await (await self._send_json("POST", f"/api/automations/{automation_id}/run", {})).json()
        web_session = self.app[MANAGER_KEY].sessions[run["session_id"]]
        for _ in range(100):
            if not web_session.running:
                break
            await asyncio.sleep(0.05)
        automations = (await self._get_json("/api/automations"))["automations"]
        snapshot = await self._get_json(f"/api/sessions/{run['session_id']}")

        self.assertEqual(created["automations"][0]["schedule_text"], "工作日 18:00")
        self.assertEqual(rejected.status, 400)
        self.assertEqual(automations[0]["last_status"], "done")
        self.assertEqual(snapshot["title"], "日报")
        self.assertEqual(snapshot["items"][-1]["text"], "日报写好了")


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

    def test_start_workspace_goes_first_in_the_list(self) -> None:
        other = self.directories.home_dir / "other"
        app_config = AppConfig(workspaces=[str(other), str(self.directories.workspace)])

        server._put_workspace_first(app_config, self.settings)

        self.assertEqual(app_config.workspaces[0], str(self.directories.workspace.resolve()))
        self.assertEqual(len(app_config.workspaces), 2)


if __name__ == "__main__":
    unittest.main()
