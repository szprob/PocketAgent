import asyncio
import base64
import json
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

try:
    from aiohttp import FormData, web
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("没装 aiohttp，跳过 web 测试")

from pocketagent.config import AppConfig, Settings, config_path
from pocketagent.core.agent import INTERRUPTED_TOOL_RESULT
from pocketagent.knowledge.memory import project_memory_dir
from pocketagent.llm import LLMResponse, ScriptedProvider, Usage
from pocketagent.safety.permissions import ApprovalMode
from pocketagent.server import app as server
from pocketagent.server.app import MANAGER_KEY, create_app
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


class WebTestBase(AioHTTPTestCase):
    """搭服务和小工具；具体用例在下面的子类里。"""

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


class WorktreeWebTest(WebTestBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        workspace = self.directories.workspace
        subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
        (workspace / "tracked.txt").write_text("from-head\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(workspace), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
            check=True,
        )

    async def test_create_and_browse_worktree_session(self) -> None:
        await self._send_json("PUT", "/api/config", {"worktree_foreground": True})
        response = await self._send_json("POST", "/api/sessions", {"workspace": str(self.directories.workspace)})
        created = await response.json()

        self.assertEqual(response.status, 200)
        snapshot = await self._get_json(f"/api/sessions/{created['id']}")
        self.assertEqual(snapshot["workspace"], created["workspace"])
        self.assertEqual(snapshot["worktree"]["origin"], str(self.directories.workspace))
        files = await self._get_json(f"/api/files?workspace={created['workspace']}")
        self.assertEqual([entry["path"] for entry in files["entries"]], ["tracked.txt"])

    async def test_worktree_defaults_off_and_scratch_never_isolates(self) -> None:
        session_id = await self._create_session(workspace=str(self.directories.workspace))
        snapshot = await self._get_json(f"/api/sessions/{session_id}")
        scratch_id = await self._create_session(ephemeral=True)
        scratch = await self._get_json(f"/api/sessions/{scratch_id}")

        self.assertIsNone(snapshot["worktree"])
        self.assertIsNone(snapshot["worktree_fallback"])
        self.assertIsNone(scratch["worktree"])
        self.assertIsNone(scratch["worktree_fallback"])


class WebServerTest(WebTestBase):
    # ---- 鉴权、基本信息 ----

    async def test_token_is_required_and_remembered_by_cookie(self) -> None:
        without_token = await self.client.get("/api/info")
        with_token = await self.client.get("/", params=AUTH)
        static_file_by_cookie = await self.client.get("/static/js/main.js")

        self.assertEqual(without_token.status, 401)
        self.assertEqual(with_token.status, 200)
        self.assertIn("PocketAgent", await with_token.text())
        self.assertEqual(static_file_by_cookie.status, 200)

    async def test_static_files_ask_browser_to_revalidate(self) -> None:
        response = await self.client.get("/static/js/core/api.js", params=AUTH)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-cache")

    async def test_info_lists_three_web_modes_and_models(self) -> None:
        info = await self._get_json("/api/info")
        missing = await self.client.get("/api/sessions/nope", params=AUTH)

        self.assertEqual(info["modes"], ["ask", "ai_auto", "full_auto"])
        self.assertEqual([model["id"] for model in info["models"]], ["ds", "qwen"])
        self.assertTrue(info["models"][0]["supports_images"])
        self.assertEqual(info["busy_input_mode"], "interject")
        # 外观和字号前端一启动就要套上
        self.assertEqual((info["appearance"], info["font_scale"]), ("system", 1.0))
        # 工具调用默认收进折叠
        self.assertTrue(info["fold_tool_calls"])
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
            waiting = await self._get_json("/api/sessions")
            self.assertIn("新建 a.txt", approval["preview"])
            # 卡在审批上时，任务列表要说「在等你确认」，不是「在跑」
            self.assertEqual(waiting["sessions"][0]["waiting"], "approval")
            await socket_client.send_json({"type": "approval", "request_id": approval["request_id"], "answer": "allow_once"})
            turn_end = await self._receive_until(socket_client, is_event("turn_end"))
            await self._receive_until(socket_client, is_idle_state)

        self.assertEqual(turn_end["event"]["final_text"], "写好了")
        self.assertEqual((self.directories.workspace / "a.txt").read_text(encoding="utf-8"), "x")
        history = await self._get_json(f"/api/sessions/{session_id}")
        self.assertEqual([item["kind"] for item in history["items"]], ["user", "tool", "assistant"])
        self.assertEqual(history["items"][1]["result"], "已新建 a.txt（1 行）")
        self.assertEqual(history["title"], "写个文件")
        tasks = await self._get_json("/api/sessions")
        self.assertEqual(tasks["sessions"][0]["title"], "写个文件")
        # 第一次调用没报 usage，按估算记；第二次 107
        self.assertGreater(history["usage"]["total"], 107)

    async def test_several_writes_come_as_one_card_with_per_item_answers(self) -> None:
        self.responses.extend(
            [
                LLMResponse(
                    tool_calls=[
                        tool_call("call_1", "write_file", '{"path": "a.txt", "content": "A"}'),
                        tool_call("call_2", "write_file", '{"path": "b.txt", "content": "B"}'),
                    ]
                ),
            ]
        )
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "写两个文件"})
            approval = await self._receive_until(socket_client, is_type("approval_request"))
            await socket_client.send_json(
                {"type": "approval", "request_id": approval["request_id"], "answers": ["allow_once", "deny"]}
            )
            turn_end = await self._receive_until(socket_client, is_event("turn_end"))

        self.assertEqual([item["tool_name"] for item in approval["items"]], ["write_file", "write_file"])
        self.assertIn("新建 b.txt", approval["items"][1]["preview"])
        self.assertEqual(turn_end["event"]["stop_reason"], "denied")
        self.assertTrue((self.directories.workspace / "a.txt").exists())
        self.assertFalse((self.directories.workspace / "b.txt").exists())

    async def test_todo_write_updates_plan_panel_and_persists(self) -> None:
        arguments = (
            '{"todos": ['
            '{"content": "调研口径", "status": "completed"},'
            '{"content": "写 runbook", "status": "in_progress", "active_form": "正在写 runbook"}'
            "]}"
        )
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("call_1", "todo_write", arguments)]),
                LLMResponse(text="计划列好了", usage=Usage(prompt_tokens=10, completion_tokens=5)),
            ]
        )
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "列个计划"})
            todos_updated = await self._receive_until(socket_client, is_event("todos_updated"))
            await self._receive_until(socket_client, is_event("turn_end"))
            await self._receive_until(socket_client, is_idle_state)
            # 重连拿快照：todos 要跟着会话恢复
            async with self._connect(session_id) as again:
                snapshot = await again.receive_json()
                self.assertEqual(snapshot["todos"], todos_updated["event"]["todos"])

        self.assertEqual(todos_updated["event"]["todos"][1]["status"], "in_progress")
        self.assertEqual(todos_updated["event"]["todos"][1]["active_form"], "正在写 runbook")
        history = await self._get_json(f"/api/sessions/{session_id}")
        self.assertEqual(history["items"][1]["name"], "todo_write")

    async def test_rewind_removes_the_message_and_everything_after(self) -> None:
        self.responses.extend(
            [
                LLMResponse(text="第一答", usage=Usage(prompt_tokens=10, completion_tokens=2)),
                LLMResponse(text="第二答", usage=Usage(prompt_tokens=10, completion_tokens=2)),
            ]
        )
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            for text in ("第一问", "第二问"):
                await socket_client.send_json({"type": "user_message", "text": text})
                await self._receive_until(socket_client, is_event("turn_end"))
                await self._receive_until(socket_client, is_idle_state)

            snapshot = await self._get_json(f"/api/sessions/{session_id}")
            users = [item for item in snapshot["items"] if item["kind"] == "user"]
            self.assertTrue(all(item["message_id"] for item in users))

            response = await self._send_json(
                "POST", f"/api/sessions/{session_id}/rewind", {"message_id": users[1]["message_id"]}
            )
            payload = await response.json()
            # 新快照也推给其它标签页 / 桌面窗口
            broadcast = await self._receive_until(socket_client, is_type("snapshot"))

            again = await self._send_json(
                "POST", f"/api/sessions/{session_id}/rewind", {"message_id": users[1]["message_id"]}
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["message"]["text"], "第二问")
        self.assertEqual([item["kind"] for item in payload["snapshot"]["items"]], ["user", "assistant"])
        self.assertEqual(broadcast["items"], payload["snapshot"]["items"])
        # 这句话已经不在了，再恢复一次是冲突
        self.assertEqual(again.status, 409)
        stored = self.app[MANAGER_KEY].store.load(session_id)
        self.assertEqual([message.get("content") for message in stored.messages if message.get("role") == "user"], ["第一问"])
        self.assertEqual(stored.turns, 1)
        self.assertEqual(stored.title, None)

    async def test_rewind_is_refused_while_the_agent_is_running(self) -> None:
        gated = GatedProvider([LLMResponse(text="答")])
        self.provider_override = gated
        session_id = await self._create_session()

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "先说一句"})
            started = await self._receive_until(socket_client, is_event("turn_start"))
            response = await self._send_json(
                "POST", f"/api/sessions/{session_id}/rewind", {"message_id": started["event"]["message_id"]}
            )
            gated.gate.set()
            await self._receive_until(socket_client, is_event("turn_end"))

        self.assertEqual(response.status, 409)
        self.assertIn("还在运行", (await response.json())["error"])

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
            # 等第一次调模型真的开始了再插话（每轮开始前要先拍检查点）
            for _ in range(500):
                if gated.call_count >= 1:
                    break
                await asyncio.sleep(0.01)
            await socket_client.send_json({"type": "user_message", "text": "顺便看看 README"})
            await self._receive_until(socket_client, is_event("interjection_queued"))
            gated.gate.set()
            applied = await self._receive_until(socket_client, is_event("interjection_applied"))
            # 插话交给模型后服务端马上推一次 state，「待发」里已经没有它了（各前端不用自己删）
            state_after = await self._receive_until(socket_client, lambda message: message["type"] == "state")
            await self._receive_until(socket_client, is_event("turn_end"))

        second_call = gated.received_messages[1]
        self.assertEqual(state_after["interjections"], [])
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

    async def test_shadow_git_sees_bash_changes_and_reverts_them(self) -> None:
        (self.directories.workspace / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (self.directories.workspace / "old.txt").write_text("old\n", encoding="utf-8")
        self.responses.extend(
            [
                LLMResponse(
                    tool_calls=[
                        tool_call("c1", "edit_file", '{"path": "app.py", "old_string": "v1", "new_string": "v2"}'),
                        # 以前的变更跟踪看不见 bash 的改动；影子 git 拍整个工作区，都看得见
                        tool_call("c2", "bash", '{"command": "echo hi > from_bash.txt && rm old.txt"}'),
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

        payload = await self._get_json(f"/api/sessions/{session_id}/changes")
        by_path = {change["path"]: change for change in payload["changes"]}
        diff = await self.client.get(f"/api/sessions/{session_id}/changes/diff", params={**AUTH, "path": "app.py"})
        await self._send_json("POST", f"/api/sessions/{session_id}/changes/revert", {"path": "app.py"})
        await self._send_json("POST", f"/api/sessions/{session_id}/changes/revert", {"path": "from_bash.txt"})
        baseline = payload["checkpoints"][0]["commit"]
        restored = await (await self._send_json("POST", f"/api/sessions/{session_id}/checkpoints/restore", {"commit": baseline})).json()

        self.assertTrue(payload["enabled"])
        self.assertEqual(
            {path: change["status"] for path, change in by_path.items()},
            {"app.py": "modified", "from_bash.txt": "added", "old.txt": "deleted"},
        )
        self.assertIn("+print('v2')", (await diff.json())["diff"])
        self.assertEqual((self.directories.workspace / "app.py").read_text(encoding="utf-8"), "print('v1')\n")
        self.assertFalse((self.directories.workspace / "from_bash.txt").exists())
        # 整体恢复把 bash 删掉的 old.txt 也找回来了，恢复前还自动拍了一张
        self.assertEqual(restored["restored"], ["old.txt"])
        self.assertEqual((self.directories.workspace / "old.txt").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(restored["changes"], [])
        self.assertEqual([checkpoint["kind"] for checkpoint in restored["checkpoints"]][-1], "safety")

    async def test_turn_changes_only_cover_the_current_turn(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("c1", "write_file", '{"path": "first.svg", "content": "<svg/>"}')]),
                LLMResponse(text="第一轮好了"),
                LLMResponse(tool_calls=[tool_call("c2", "write_file", '{"path": "second.svg", "content": "<svg/>"}')]),
                LLMResponse(text="第二轮好了"),
            ]
        )
        session_id = await self._create_session(mode="full_auto")
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            for text in ("第一轮", "第二轮"):
                await socket_client.send_json({"type": "user_message", "text": text})
                await self._receive_until(socket_client, is_event("turn_end"))
                await self._receive_until(socket_client, is_idle_state)

        turn = await self._get_json(f"/api/sessions/{session_id}/changes/turn")
        everything = await self._get_json(f"/api/sessions/{session_id}/changes")

        self.assertEqual([change["path"] for change in turn["changes"]], ["second.svg"])
        self.assertEqual([change["path"] for change in everything["changes"]], ["first.svg", "second.svg"])

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

    async def test_default_workspace_can_be_configured(self) -> None:
        other_workspace = self.directories.home_dir / "other"
        other_workspace.mkdir(parents=True)
        await self._send_json("POST", "/api/workspaces", {"path": str(self.directories.workspace)})
        await self._send_json("POST", "/api/workspaces", {"path": str(other_workspace)})

        not_listed = await self._send_json("POST", "/api/workspaces/default", {"path": "/no/such/dir"})
        saved = await self._send_json("POST", "/api/workspaces/default", {"path": str(other_workspace)})
        info = await self._get_json("/api/info")
        stored = AppConfig.load(config_path(self.directories.home_dir))
        # 不带工作区建任务，落在配置的默认工作区里
        session_id = await self._create_session()
        snapshot = await self._get_json(f"/api/sessions/{session_id}")
        await self._send_json("DELETE", "/api/workspaces", {"path": str(other_workspace)})
        after_remove = await self._get_json("/api/info")

        self.assertEqual(not_listed.status, 400)
        self.assertEqual(saved.status, 200)
        self.assertEqual(info["default_workspace"], str(other_workspace.resolve()))
        self.assertEqual(stored.default_workspace, str(other_workspace.resolve()))
        self.assertEqual(snapshot["workspace"], str(other_workspace.resolve()))
        # 默认的那个被移出列表后，回落到剩下的第一个
        self.assertEqual(after_remove["default_workspace"], str(self.directories.workspace.resolve()))

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


class WebScratchTaskTest(WebTestBase):
    """不指定工作区的任务：服务端在 ~/.pocketagent/scratch 下建一个临时目录，工具照常能用。"""

    async def test_new_task_without_a_workspace_runs_in_a_scratch_directory(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("call_1", "write_file", '{"path": "note.txt", "content": "hi"}')]),
                LLMResponse(text="写好了"),
            ]
        )
        session_id = await self._create_session(ephemeral=True, mode="full_auto")
        snapshot = await self._get_json(f"/api/sessions/{session_id}")
        workspace = Path(snapshot["workspace"])

        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "写个文件"})
            await self._receive_until(socket_client, is_event("turn_end"))
            await self._receive_until(socket_client, is_idle_state)

        self.assertTrue(snapshot["ephemeral"])
        self.assertEqual(workspace.parent, self.directories.home_dir / "scratch")
        self.assertTrue(workspace.is_dir())
        self.assertEqual((workspace / "note.txt").read_text(encoding="utf-8"), "hi")
        tasks = await self._get_json("/api/sessions")
        self.assertTrue(tasks["sessions"][0]["ephemeral"])

    async def test_session_file_raw_works_in_the_scratch_directory(self) -> None:
        session_id = await self._create_session(ephemeral=True)
        workspace = Path((await self._get_json(f"/api/sessions/{session_id}"))["workspace"])
        (workspace / "chart.png").write_bytes(TINY_PNG)

        image = await self.client.get(f"/api/sessions/{session_id}/file/raw", params={**AUTH, "path": "chart.png"})

        self.assertEqual(image.status, 200)
        self.assertEqual(await image.read(), TINY_PNG)

    async def test_reopening_a_task_whose_scratch_directory_was_cleaned(self) -> None:
        self.responses.append(LLMResponse(text="收到"))
        session_id = await self._create_session(ephemeral=True, mode="full_auto")
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "你好"})
            await self._receive_until(socket_client, is_event("turn_end"))
            await self._receive_until(socket_client, is_idle_state)
        workspace = Path((await self._get_json(f"/api/sessions/{session_id}"))["workspace"])

        # 用户把 scratch 清了、服务也重启过：重开任务时要把目录重新建出来，不能满屏「目录不存在」
        shutil.rmtree(workspace)
        self.app[MANAGER_KEY].sessions.clear()
        reopened = await self._get_json(f"/api/sessions/{session_id}")

        self.assertTrue(workspace.is_dir())
        self.assertTrue(reopened["ephemeral"])
        self.assertEqual(reopened["workspace"], str(workspace))


class WebRound3Test(WebTestBase):
    """模型选项、右侧栏概要、文件浏览、联网设置。"""

    async def test_patch_model_options_applies_to_open_sessions(self) -> None:
        session_id = await self._create_session()
        response = await self._send_json("PATCH", "/api/models/ds", {"reasoning_effort": "none", "context_window_tokens": 64000})
        invalid = await self._send_json("PATCH", "/api/models/ds", {"reasoning_effort": "xhigh"})
        info = await self._get_json("/api/info")
        snapshot = await self._get_json(f"/api/sessions/{session_id}")

        self.assertEqual(response.status, 200)
        self.assertEqual(invalid.status, 400)
        model = next(item for item in info["models"] if item["id"] == "ds")
        self.assertEqual((model["reasoning_effort"], model["context_window_tokens"]), ("none", 64000))
        self.assertEqual(self.provider_settings[-1].reasoning_effort, "none")
        self.assertEqual(snapshot["context_window"], 64000)

    async def test_overview_has_usage_and_latest_reply(self) -> None:
        self.responses.append(LLMResponse(text="最新的回复", usage=Usage(prompt_tokens=100, completion_tokens=5, cached_tokens=60)))
        session_id = await self._create_session()
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "你好"})
            await self._receive_until(socket_client, is_event("turn_end"))

        overview = await self._get_json(f"/api/sessions/{session_id}/overview")

        self.assertEqual(overview["latest_reply"], "最新的回复")
        self.assertEqual(overview["turns"], 1)
        self.assertEqual(overview["tool_calls"], 0)
        self.assertEqual(overview["usage"]["cached"], 60)
        self.assertEqual(overview["usage"]["cache_hit_rate"], 0.6)

    async def test_file_tree_preview_and_folder_browsing(self) -> None:
        workspace = self.directories.workspace
        (workspace / "src").mkdir()
        (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (workspace / "logo.png").write_bytes(TINY_PNG)
        (workspace / ".git").mkdir()
        unregistered = await self.client.get("/api/files", params={**AUTH, "workspace": str(workspace)})
        await self._send_json("POST", "/api/workspaces", {"path": str(workspace)})
        query = {**AUTH, "workspace": str(workspace.resolve())}

        root = await (await self.client.get("/api/files", params=query)).json()
        nested = await (await self.client.get("/api/files", params={**query, "path": "src"})).json()
        preview = await (await self.client.get("/api/file", params={**query, "path": "src/app.py"})).json()
        image = await (await self.client.get("/api/file", params={**query, "path": "logo.png"})).json()
        raw = await self.client.get("/api/file/raw", params={**query, "path": "logo.png"})
        escape = await self.client.get("/api/file", params={**query, "path": "../outside.txt"})
        browse = await (await self.client.get("/api/browse", params={**AUTH, "path": str(workspace)})).json()

        self.assertEqual(unregistered.status, 400)
        self.assertEqual([(entry["name"], entry["is_dir"]) for entry in root["entries"]], [("src", True), ("logo.png", False)])
        self.assertEqual(nested["entries"][0]["path"], "src/app.py")
        # Windows 上写文件会把 \n 落成 \r\n，预览原样返回
        self.assertEqual((preview["kind"], preview["content"].replace("\r\n", "\n")), ("text", "print('hi')\n"))
        self.assertEqual(image["kind"], "image")
        self.assertEqual(await raw.read(), TINY_PNG)
        self.assertEqual(escape.status, 400)
        self.assertEqual([item["name"] for item in browse["directories"]], ["src"])

    async def test_folder_browsing_lists_windows_drives_at_home(self) -> None:
        if sys.platform != "win32":
            self.skipTest("只在 Windows 上列盘符")
        browse = await (await self.client.get("/api/browse", params={**AUTH, "path": str(Path.home())})).json()
        drive_names = [
            item["name"]
            for item in browse["directories"]
            if len(item["name"]) == 3 and item["name"][1:] == ":\\"
        ]

        self.assertTrue(drive_names)
        # 只填盘符也要落到盘符根目录，不能落到该盘的当前目录
        drive_only = await (
            await self.client.get("/api/browse", params={**AUTH, "path": drive_names[0][:2]})
        ).json()
        self.assertEqual(drive_only["path"], drive_names[0])

    async def test_session_file_raw_serves_images_without_workspace_registration(self) -> None:
        session_id = await self._create_session()
        (self.directories.workspace / "chart.png").write_bytes(TINY_PNG)

        image = await self.client.get(f"/api/sessions/{session_id}/file/raw", params={**AUTH, "path": "chart.png"})
        escape = await self.client.get(f"/api/sessions/{session_id}/file/raw", params={**AUTH, "path": "../outside.png"})
        missing = await self.client.get(f"/api/sessions/{session_id}/file/raw", params={**AUTH, "path": "nope.png"})

        self.assertEqual(image.status, 200)
        self.assertEqual(await image.read(), TINY_PNG)
        self.assertEqual(escape.status, 400)
        self.assertEqual(missing.status, 400)

    async def test_web_search_key_is_kept_when_left_blank(self) -> None:
        await self._send_json("PUT", "/api/config", {"web_search_api_key": "search-secret-key-1"})
        await self._send_json("PUT", "/api/config", {"web_search_api_key": "", "web_search_engine": "search_pro"})
        saved = json.loads((self.directories.home_dir / "config.json").read_text(encoding="utf-8"))
        config = await self._get_json("/api/config")

        self.assertEqual(saved["web_search_api_key"], "search-secret-key-1")
        self.assertEqual(saved["web_search_engine"], "search_pro")
        self.assertNotIn("search-secret-key-1", json.dumps(config))
        self.assertEqual(config["web_search_key_hint"], "search…ey-1")

    async def test_web_search_defaults_to_bing_without_a_key(self) -> None:
        config = await self._get_json("/api/config")
        info = await self._get_json("/api/info")

        self.assertEqual(config["web_search_provider"], "bing")
        # 必应不要密钥：一个智谱模型都没配，搜索也是能用的
        self.assertTrue(info["web_search_enabled"])


class WebAccessTest(WebTestBase):
    """工作目录外的访问、技能、沙箱设置。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # 不读本机的 ~/.claude/skills
        patcher = mock.patch("pocketagent.knowledge.skills.USER_SKILL_DIRS", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_outside_read_asks_once_then_remembers_the_directory(self) -> None:
        elsewhere = self.directories.workspace.parent / "elsewhere"
        elsewhere.mkdir(parents=True)
        (elsewhere / "a.txt").write_text("A\n", encoding="utf-8")
        (elsewhere / "b.txt").write_text("B\n", encoding="utf-8")
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("c1", "read_file", json.dumps({"path": str(elsewhere / "a.txt")}))]),
                LLMResponse(tool_calls=[tool_call("c2", "read_file", json.dumps({"path": str(elsewhere / "b.txt")}))]),
                LLMResponse(text="都读了"),
            ]
        )
        session_id = await self._create_session(mode="ask")
        async with self._connect(session_id) as socket_client:
            await socket_client.receive_json()
            await socket_client.send_json({"type": "user_message", "text": "读那两个文件"})
            approval = await self._receive_until(socket_client, is_type("approval_request"))
            await socket_client.send_json({"type": "approval", "request_id": approval["request_id"], "answer": "allow_always"})
            await self._receive_until(socket_client, is_event("turn_end"))

        snapshot = await self._get_json(f"/api/sessions/{session_id}")
        tool_results = [item for item in snapshot["items"] if item["kind"] == "tool"]

        self.assertTrue(any(str(elsewhere / "a.txt") in note for note in approval["notes"]))
        self.assertIn(str(elsewhere), approval["always_label"])
        self.assertEqual(len(tool_results), 2)
        self.assertFalse(any(item["is_error"] for item in tool_results))

    async def test_skills_api_and_settings_round_trip(self) -> None:
        skill_file = self.directories.workspace / ".claude" / "skills" / "deploy" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: deploy\ndescription: 部署用\n---\n步骤", encoding="utf-8")
        session_id = await self._create_session()
        web_session = self.app[MANAGER_KEY].sessions[session_id]
        workspace = str(self.directories.workspace.resolve())

        listed = await (await self.client.get("/api/skills", params={**AUTH, "workspace": workspace})).json()
        without_workspace = await self._get_json("/api/skills")
        tools_before = web_session.agent.context.tools.names()
        trusted = self.directories.workspace.parent / "pocket-data"
        # PocketAgent 自己的目录是密钥目录：配进免问目录后审批不问，但不进沙箱的可写范围
        secret_home = self.directories.home_dir.resolve()
        await self._send_json(
            "PUT",
            "/api/config",
            {
                "disabled_skills": ["deploy"],
                "skill_dirs": ["~/my-skills"],
                "sandbox_network": True,
                "trusted_dirs": [str(trusted), str(secret_home)],
            },
        )
        after = await (await self.client.get("/api/skills", params={**AUTH, "workspace": workspace})).json()
        config = await self._get_json("/api/config")

        self.assertEqual([(skill["name"], skill["source"], skill["enabled"]) for skill in listed["skills"]], [("deploy", "项目", True)])
        self.assertEqual(without_workspace["skills"], [])
        self.assertIn("activate_skill", tools_before)
        self.assertEqual(after["skills"][0]["enabled"], False)
        self.assertNotIn("activate_skill", web_session.agent.context.tools.names())
        self.assertEqual(config["disabled_skills"], ["deploy"])
        self.assertEqual(config["skill_dirs"], [str(Path("~/my-skills").expanduser().resolve())])
        self.assertEqual(config["trusted_dirs"], [str(trusted.resolve()), str(secret_home)])
        self.assertEqual(web_session.agent.context.tool_context.trusted_dirs, [trusted.resolve(), secret_home])
        self.assertTrue(config["sandbox_network"])
        if web_session.agent.context.tool_context.sandbox is not None:
            self.assertTrue(web_session.agent.context.tool_context.sandbox.allow_network)
            self.assertEqual(web_session.agent.context.tool_context.sandbox.writable_roots, [trusted.resolve()])
            self.assertIn(trusted.resolve(), web_session.agent.context.tool_context.sandbox.writable_roots)


class WebSkillsMemoryTest(WebTestBase):
    """设置里的技能查看、项目记忆、模型可见性。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch("pocketagent.knowledge.skills.USER_SKILL_DIRS", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_skill_content_only_for_discovered_skills(self) -> None:
        skill_file = self.directories.home_dir / "skills" / "deploy" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: deploy\ndescription: 部署用\n---\n步骤一", encoding="utf-8")
        (self.directories.home_dir / "config.json").write_text("{}", encoding="utf-8")

        listed = await self._get_json("/api/skills")
        content = await (await self.client.get("/api/skills/content", params={**AUTH, "path": str(skill_file)})).json()
        refused = await self.client.get("/api/skills/content", params={**AUTH, "path": str(self.directories.home_dir / "config.json")})

        self.assertEqual(listed["skills"][0]["warnings"], [])
        self.assertIn("步骤一", content["content"])
        self.assertEqual(refused.status, 404)

    async def test_memory_overview_view_and_delete(self) -> None:
        workspace = str(self.directories.workspace.resolve())
        await self._send_json("POST", "/api/workspaces", {"path": workspace})
        memory_dir = project_memory_dir(self.directories.home_dir, self.directories.workspace)
        memory_dir.mkdir(parents=True)
        (memory_dir / "a.md").write_text("---\nname: a\ndescription: 第一条\ntype: user\n---\n正文A", encoding="utf-8")
        (memory_dir / "MEMORY.md").write_text("- [A](a.md) — 第一条\n", encoding="utf-8")
        query = {**AUTH, "workspace": workspace}

        overview = await (await self.client.get("/api/memory", params=query)).json()
        content = await (await self.client.get("/api/memory/file", params={**query, "file": "a.md"})).json()
        escape = await self.client.get("/api/memory/file", params={**query, "file": "../../config.json"})
        unregistered = await self.client.get("/api/memory", params={**AUTH, "workspace": "/tmp"})
        after = await (await self.client.delete("/api/memory/file", params={**query, "file": "a.md"})).json()

        self.assertEqual([(m["name"], m["type"]) for m in overview["memories"]], [("a", "user")])
        self.assertIn("正文A", content["content"])
        self.assertEqual(escape.status, 400)
        self.assertEqual(unregistered.status, 400)
        self.assertEqual(after["memories"], [])
        self.assertEqual(after["index"], "")

    async def test_model_visibility_round_trip(self) -> None:
        config = await self._get_json("/api/config")
        models = config["models"]
        for model in models:
            model["visible"] = model["id"] != "qwen"
        await self._send_json("PUT", "/api/config", {"models": models, "memory_enabled": False})

        info = await self._get_json("/api/info")
        saved = json.loads((self.directories.home_dir / "config.json").read_text(encoding="utf-8"))

        self.assertEqual({model["id"]: model["visible"] for model in info["models"]}, {"ds": True, "qwen": False})
        self.assertFalse(saved["memory_enabled"])


class WebQuestionsTest(WebTestBase):
    """ask_user：问题推到浏览器，选完提交，模型拿到回答；刷新页面问题还在；可以跳过。"""

    QUESTIONS = {
        "questions": [
            {"question": "用哪个数据库？", "header": "数据库", "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}]},
            {"question": "要哪些测试？", "multi_select": True, "options": [{"label": "单元"}, {"label": "集成"}]},
        ]
    }

    async def _run_until_question(self, socket_client, session_id):
        await socket_client.receive_json()
        await socket_client.send_json({"type": "user_message", "text": "搭个后端"})
        return await self._receive_until(socket_client, is_type("question_request"))

    async def test_answers_reach_the_model(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("c1", "ask_user", json.dumps(self.QUESTIONS, ensure_ascii=False))]),
                LLMResponse(text="收到"),
            ]
        )
        session_id = await self._create_session(mode="ask")
        async with self._connect(session_id) as socket_client:
            question = await self._run_until_question(socket_client, session_id)
            snapshot = await self._get_json(f"/api/sessions/{session_id}")
            answers = [{"selected": ["SQLite"]}, {"selected": ["单元", "集成"], "other": "还要压测"}]
            await socket_client.send_json({"type": "question_answer", "request_id": question["request_id"], "answers": answers})
            await self._receive_until(socket_client, is_type("question_resolved"))
            await self._receive_until(socket_client, is_event("turn_end"))

        sent = self.scripted_providers[-1].received_messages[-1]
        tool_message = next(message for message in sent if message["role"] == "tool")
        self.assertEqual([item["header"] for item in question["questions"]], ["数据库", ""])
        self.assertEqual(snapshot["pending_questions"][0]["request_id"], question["request_id"])
        self.assertIn("→ SQLite", tool_message["content"])
        self.assertIn("单元、集成；用户自己写的：还要压测", tool_message["content"])

    async def test_session_list_says_it_is_waiting_for_the_answer(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("c1", "ask_user", json.dumps(self.QUESTIONS, ensure_ascii=False))]),
                LLMResponse(text="收到"),
            ]
        )
        session_id = await self._create_session(mode="ask")
        async with self._connect(session_id) as socket_client:
            question = await self._run_until_question(socket_client, session_id)
            waiting = await self._get_json("/api/sessions")
            await socket_client.send_json(
                {"type": "question_answer", "request_id": question["request_id"], "answers": None}
            )
            await self._receive_until(socket_client, is_type("question_resolved"))
            answered = await self._get_json("/api/sessions")

        # 问了还没答：侧栏要显示「等你回答」的角标，而不是运行中的呼吸灯
        self.assertEqual(waiting["sessions"][0]["waiting"], "question")
        self.assertIsNone(answered["sessions"][0]["waiting"])

    async def test_skipping_tells_the_model_to_decide(self) -> None:
        self.responses.extend(
            [
                LLMResponse(tool_calls=[tool_call("c1", "ask_user", json.dumps(self.QUESTIONS, ensure_ascii=False))]),
                LLMResponse(text="我自己定了"),
            ]
        )
        session_id = await self._create_session(mode="ask")
        async with self._connect(session_id) as socket_client:
            question = await self._run_until_question(socket_client, session_id)
            await socket_client.send_json({"type": "question_answer", "request_id": question["request_id"], "answers": None})
            await self._receive_until(socket_client, is_event("turn_end"))

        sent = self.scripted_providers[-1].received_messages[-1]
        tool_message = next(message for message in sent if message["role"] == "tool")
        self.assertIn("跳过", tool_message["content"])


class WebCommandTest(WebTestBase):
    """/api/command：web 的斜杠命令入口，和命令行共用一套。"""

    async def _run_command(self, text: str, **payload) -> dict[str, Any]:
        response = await self._send_json("POST", "/api/command", {"text": text, **payload})
        return await response.json()

    async def test_help_status_and_unknown_command(self) -> None:
        session_id = await self._create_session()
        help_result = await self._run_command("/help", session_id=session_id)
        status = await self._run_command("/status", session_id=session_id)
        unknown = await self._run_command("/没有这条", session_id=session_id)
        exit_result = await self._run_command("/exit", session_id=session_id)

        self.assertTrue(help_result["handled"])
        self.assertIn("/compact", help_result["text"])
        self.assertIn("模型", status["text"])
        self.assertEqual(unknown["kind"], "error")
        # web 上没有退出命令
        self.assertEqual(exit_result["kind"], "warning")

    async def test_switch_mode_and_model_apply_to_the_task(self) -> None:
        session_id = await self._create_session()
        mode_result = await self._run_command("/mode full_auto", session_id=session_id)
        model_result = await self._run_command("/model qwen", session_id=session_id)
        snapshot = await self._get_json(f"/api/sessions/{session_id}")

        self.assertEqual(mode_result["action"], "switch_mode")
        self.assertEqual(snapshot["mode"], "full_auto")
        self.assertEqual(model_result["action"], "switch_model")
        self.assertEqual(snapshot["model_id"], "qwen")

    async def test_skill_commands_and_plain_messages_are_not_handled(self) -> None:
        session_id = await self._create_session()
        plain = await self._run_command("写个文件", session_id=session_id)
        slash_path = await self._run_command("/usr/bin/python 是什么", session_id=session_id)

        self.assertFalse(plain["handled"])
        self.assertFalse(slash_path["handled"])

    async def test_new_task_page_commands_without_a_session(self) -> None:
        help_result = await self._run_command("/help")
        compact = await self._run_command("/compact")
        sessions = await self._run_command("/sessions", workspace=str(self.directories.workspace))

        self.assertTrue(help_result["handled"])
        self.assertEqual(compact["kind"], "warning")
        self.assertIn("先开个任务", compact["text"])
        self.assertIn("还没有会话", sessions["text"])


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

    def test_explicit_workspace_goes_first_in_the_list(self) -> None:
        other = self.directories.home_dir / "other"
        app_config = AppConfig(workspaces=[str(other), str(self.directories.workspace)])

        server._register_launch_workspace(app_config, self.settings, explicit=True)

        self.assertEqual(app_config.workspaces[0], str(self.directories.workspace.resolve()))
        self.assertEqual(len(app_config.workspaces), 2)

    def test_launch_directory_does_not_come_back_after_removal(self) -> None:
        """没用 -C 指定时不动已有的列表：用户在界面里移除的启动目录，下次启动不会又被加回来。"""
        other = str(self.directories.home_dir / "other")
        app_config = AppConfig(workspaces=[other], default_workspace=other)

        server._register_launch_workspace(app_config, self.settings, explicit=False)

        self.assertEqual(app_config.workspaces, [other])
        self.assertEqual(app_config.default_workspace, other)

    def test_launch_directory_is_added_on_first_run(self) -> None:
        app_config = AppConfig()

        server._register_launch_workspace(app_config, self.settings, explicit=False)

        workspace = str(self.directories.workspace.resolve())
        self.assertEqual(app_config.workspaces, [workspace])
        self.assertEqual(app_config.default_workspace, workspace)


if __name__ == "__main__":
    unittest.main()
