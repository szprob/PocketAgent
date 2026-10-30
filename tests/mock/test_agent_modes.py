"""工作模式：Agent 默认、Ask 只回答、计划 / 设计提交方案等批准后再动手。"""

import json
import unittest
from typing import Any

try:
    from aiohttp import web  # noqa: F401
except ImportError:
    web = None

from pocketagent.core.events import EventType
from pocketagent.core.session import SessionStore
from pocketagent.interaction.modes import AgentMode, PlanDecision, PlanSwitchRequest
from pocketagent.llm import LLMResponse
from pocketagent.safety.permissions import AllowAllApprover
from tests.helpers import TemporaryDirectories, build_harness, tool_call

if web is not None:
    from tests.mock.test_web import WebTestBase, is_event, is_idle_state, is_type


class ScriptedModeApprover(AllowAllApprover):
    """测试用：剧本决定要不要切计划模式、批不批准方案，并记下问了什么。"""

    def __init__(self, switches: list[bool], decisions: list[PlanDecision]) -> None:
        self.switches = list(switches)
        self.decisions = list(decisions)
        self.switch_requests: list[PlanSwitchRequest] = []
        self.plan_requests: list[Any] = []

    async def request_plan_switch(self, request: PlanSwitchRequest) -> bool:
        self.switch_requests.append(request)
        return self.switches.pop(0)

    async def request_plan_approval(self, request) -> PlanDecision:
        self.plan_requests.append(request)
        return self.decisions.pop(0)


def suggest_call(call_id: str = "plan_switch", reason: str = "要改好几个文件，先理一下") -> Any:
    return tool_call(call_id, "suggest_plan_mode", json.dumps({"reason": reason}, ensure_ascii=False))


def submit_call(call_id: str = "plan_submit", plan: str = "# 计划\n\n1. 改 a.txt\n2. 跑测试") -> Any:
    return tool_call(call_id, "submit_plan", json.dumps({"plan": plan}, ensure_ascii=False))


def write_call(call_id: str = "write", path: str = "a.txt", content: str = "done") -> Any:
    return tool_call(call_id, "write_file", json.dumps({"path": path, "content": content}, ensure_ascii=False))


class AgentModeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_default_mode_is_agent(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="好的")], approver=AllowAllApprover())

        await harness.agent.run_turn("你好")

        self.assertIs(harness.agent.agent_mode, AgentMode.AGENT)
        self.assertEqual(harness.agent.session.agent_mode, "agent")
        prompt = harness.provider.received_messages[0][0]["content"]
        self.assertIn("关于计划模式", prompt)
        self.assertNotIn("现在处于【Ask 模式】", prompt)
        self.assertNotIn("现在处于【设计模式】", prompt)

    async def test_suggest_switch_then_approve_and_execute(self) -> None:
        approver = ScriptedModeApprover(switches=[True], decisions=[PlanDecision(approve=True)])
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[suggest_call()]),
                LLMResponse(tool_calls=[tool_call("read", "read_file", '{"path": "hello.txt"}')]),
                LLMResponse(tool_calls=[submit_call()]),
                LLMResponse(tool_calls=[write_call()]),
                LLMResponse(text="写好了"),
            ],
            approver=approver,
        )
        (self.directories.workspace / "hello.txt").write_text("hi\n", encoding="utf-8")

        final_text = await harness.agent.run_turn("把 a.txt 改一下，顺便调整调用方")

        self.assertEqual(final_text, "写好了")
        self.assertEqual(approver.switch_requests[0].reason, "要改好几个文件，先理一下")
        self.assertEqual(approver.plan_requests[0].mode, "plan")
        self.assertEqual((self.directories.workspace / "a.txt").read_text(encoding="utf-8"), "done")
        modes = [event.data["mode"] for event in harness.recorder.of_type(EventType.AGENT_MODE)]
        self.assertEqual(modes, ["plan", "agent"])
        # 计划模式期间发给模型的系统提示是计划模式那一套，批准后恢复 Agent 模式
        plan_prompt = harness.provider.received_messages[1][0]["content"]
        after_prompt = harness.provider.received_messages[4][0]["content"]
        self.assertIn("现在处于【计划模式】", plan_prompt)
        self.assertNotIn("现在处于【计划模式】", after_prompt)
        saved = SessionStore(self.directories.home_dir / "sessions").load(harness.agent.session.id)
        self.assertEqual(saved.agent_mode, "agent")
        self.assertEqual(saved.plan_status, "approved")
        self.assertEqual(saved.plan_kind, "plan")
        self.assertIn("改 a.txt", saved.plan)

    async def test_plan_mode_denies_writes_until_the_plan_is_approved(self) -> None:
        approver = ScriptedModeApprover(switches=[True], decisions=[])
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[suggest_call()]),
                LLMResponse(tool_calls=[write_call(content="x")]),
            ],
            approver=approver,
        )

        await harness.agent.run_turn("帮我改一下")

        self.assertEqual(harness.agent.context.turn.stop_reason, "denied")
        self.assertFalse((self.directories.workspace / "a.txt").exists())
        tool_messages = [message for message in harness.agent.context.messages if message["role"] == "tool"]
        self.assertIn("计划模式", tool_messages[-1]["content"])
        self.assertTrue(tool_messages[-1]["_denied"])
        self.assertEqual([event.data["mode"] for event in harness.recorder.of_type(EventType.AGENT_MODE)], ["plan"])

    async def test_user_declines_the_switch_and_work_continues_normally(self) -> None:
        approver = ScriptedModeApprover(switches=[False], decisions=[])
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[suggest_call()]),
                LLMResponse(tool_calls=[write_call(content="x")]),
                LLMResponse(text="改好了"),
            ],
            approver=approver,
        )

        final_text = await harness.agent.run_turn("改一下 a.txt")

        self.assertEqual(final_text, "改好了")
        self.assertEqual((self.directories.workspace / "a.txt").read_text(encoding="utf-8"), "x")
        self.assertEqual(harness.recorder.of_type(EventType.AGENT_MODE), [])
        tool_message = next(message for message in harness.agent.context.messages if message["role"] == "tool")
        self.assertIn("直接开始", tool_message["content"])

    async def test_feedback_keeps_plan_mode_until_the_next_submission_is_approved(self) -> None:
        approver = ScriptedModeApprover(
            switches=[], decisions=[PlanDecision(approve=False, feedback="补一下怎么验证"), PlanDecision(approve=True)]
        )
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[submit_call()]),
                LLMResponse(tool_calls=[submit_call(call_id="plan_submit_2", plan="# 计划 v2\n\n1. 改 a.txt\n2. 跑 pytest")]),
                LLMResponse(text="开始"),
            ],
            approver=approver,
        )
        await harness.agent.set_agent_mode(AgentMode.PLAN)

        final_text = await harness.agent.run_turn("把计划补完整")

        self.assertEqual(final_text, "开始")
        self.assertIs(harness.agent.agent_mode, AgentMode.AGENT)
        self.assertEqual(harness.agent.session.plan_status, "approved")
        self.assertIn("v2", harness.agent.session.plan)
        first_result = next(message for message in harness.agent.context.messages if message["role"] == "tool")
        self.assertIn("补一下怎么验证", first_result["content"])
        modes = [event.data["mode"] for event in harness.recorder.of_type(EventType.AGENT_MODE)]
        self.assertEqual(modes, ["plan", "agent"])

    async def test_submit_plan_is_rejected_outside_proposal_modes(self) -> None:
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[submit_call()]), LLMResponse(text="好")],
            approver=AllowAllApprover(),
        )

        await harness.agent.run_turn("随便做点")

        tool_message = next(message for message in harness.agent.context.messages if message["role"] == "tool")
        self.assertTrue(tool_message["_is_error"])
        self.assertIn("Agent 模式", tool_message["content"])

    async def test_ask_mode_denies_writes(self) -> None:
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[write_call()])],
            approver=AllowAllApprover(),
        )
        await harness.agent.set_agent_mode(AgentMode.ASK)

        await harness.agent.run_turn("顺便把这个文件改了")

        self.assertEqual(harness.agent.context.turn.stop_reason, "denied")
        self.assertFalse((self.directories.workspace / "a.txt").exists())
        tool_message = next(message for message in harness.agent.context.messages if message["role"] == "tool")
        self.assertIn("Ask 模式", tool_message["content"])
        prompt = harness.provider.received_messages[0][0]["content"]
        self.assertIn("现在处于【Ask 模式】", prompt)

    async def test_ask_mode_can_read_and_answer(self) -> None:
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("read", "read_file", '{"path": "hello.txt"}')]),
                LLMResponse(text="这段代码是这么工作的……"),
            ],
            approver=AllowAllApprover(),
        )
        (self.directories.workspace / "hello.txt").write_text("hi\n", encoding="utf-8")
        await harness.agent.set_agent_mode(AgentMode.ASK)

        final_text = await harness.agent.run_turn("解释一下这个文件")

        self.assertIn("这么工作", final_text)

    async def test_design_mode_produces_a_design_and_then_switches_to_agent(self) -> None:
        design = "# 登录改造设计\n\n## 备选方案\n- A：Session\n- B：JWT"
        approver = ScriptedModeApprover(switches=[], decisions=[PlanDecision(approve=True)])
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[submit_call(plan=design)]),
                LLMResponse(text="开始实现"),
            ],
            approver=approver,
        )
        await harness.agent.set_agent_mode(AgentMode.DESIGN)

        final_text = await harness.agent.run_turn("设计一下登录改造")

        self.assertEqual(final_text, "开始实现")
        self.assertEqual(approver.plan_requests[0].mode, "design")
        self.assertIs(harness.agent.agent_mode, AgentMode.AGENT)
        self.assertEqual(harness.agent.session.plan_kind, "design")
        self.assertEqual(harness.agent.session.plan_status, "approved")
        tool_message = next(message for message in harness.agent.context.messages if message["role"] == "tool")
        self.assertIn("设计方案", tool_message["content"])
        prompt = harness.provider.received_messages[0][0]["content"]
        self.assertIn("现在处于【设计模式】", prompt)


if web is not None:

    class WebAgentModeTest(WebTestBase):
        async def test_default_mode_is_agent_and_can_be_switched(self) -> None:
            session_id = await self._create_session()
            default_snapshot = await self._get_json(f"/api/sessions/{session_id}")

            async with self._connect(session_id) as socket_client:
                connected = await socket_client.receive_json()
                await socket_client.send_json({"type": "set_agent_mode", "mode": "ask"})
                ask_state = await self._receive_until(
                    socket_client, lambda message: message["type"] == "state" and message.get("agent_mode") == "ask"
                )
                await socket_client.send_json({"type": "set_agent_mode", "mode": "design"})
                design_state = await self._receive_until(
                    socket_client, lambda message: message["type"] == "state" and message.get("agent_mode") == "design"
                )

            self.assertEqual(default_snapshot["agent_mode"], "agent")
            self.assertEqual(connected["agent_mode"], "agent")
            self.assertEqual(ask_state["agent_mode"], "ask")
            self.assertEqual(design_state["agent_mode"], "design")

        async def test_create_session_in_design_mode(self) -> None:
            session_id = await self._create_session(agent_mode="design")
            snapshot = await self._get_json(f"/api/sessions/{session_id}")
            self.assertEqual(snapshot["agent_mode"], "design")

        async def test_plan_approval_over_websocket(self) -> None:
            plan_text = "# 计划\n\n1. 改 a.txt\n2. 跑测试"
            self.responses.extend(
                [
                    LLMResponse(tool_calls=[submit_call(plan=plan_text)]),
                    LLMResponse(text="按计划开始"),
                ]
            )
            session_id = await self._create_session(agent_mode="plan")

            async with self._connect(session_id) as socket_client:
                await socket_client.receive_json()
                await socket_client.send_json({"type": "user_message", "text": "把 a.txt 改一下"})
                request = await self._receive_until(socket_client, is_type("plan_request"))
                waiting = await self._get_json("/api/sessions")
                waiting_snapshot = await self._get_json(f"/api/sessions/{session_id}")
                self.assertEqual(request["kind"], "approval")
                self.assertEqual(request["mode"], "plan")
                self.assertEqual(request["plan"], plan_text)
                self.assertEqual(waiting["sessions"][0]["waiting"], "plan")
                self.assertEqual(waiting_snapshot["pending_plans"][0]["request_id"], request["request_id"])
                await socket_client.send_json(
                    {"type": "plan_answer", "request_id": request["request_id"], "approve": True}
                )
                resolved = await self._receive_until(socket_client, is_type("plan_resolved"))
                turn_end = await self._receive_until(socket_client, is_event("turn_end"))
                await self._receive_until(socket_client, is_idle_state)

            saved = await self._get_json(f"/api/sessions/{session_id}")
            self.assertTrue(resolved["approved"])
            self.assertEqual(turn_end["event"]["final_text"], "按计划开始")
            self.assertEqual(saved["agent_mode"], "agent")
            self.assertEqual(saved["plan"], plan_text)
            self.assertEqual(saved["plan_status"], "approved")
            self.assertEqual(saved["plan_kind"], "plan")
            self.assertEqual(saved["pending_plans"], [])

        async def test_design_approval_over_websocket(self) -> None:
            design = "# 设计方案\n\n- 拆成三个模块"
            self.responses.extend(
                [
                    LLMResponse(tool_calls=[submit_call(plan=design)]),
                    LLMResponse(text="按设计实现"),
                ]
            )
            session_id = await self._create_session(agent_mode="design")

            async with self._connect(session_id) as socket_client:
                await socket_client.receive_json()
                await socket_client.send_json({"type": "user_message", "text": "设计一下这个模块"})
                request = await self._receive_until(socket_client, is_type("plan_request"))
                self.assertEqual(request["kind"], "approval")
                self.assertEqual(request["mode"], "design")
                await socket_client.send_json(
                    {"type": "plan_answer", "request_id": request["request_id"], "approve": True}
                )
                await self._receive_until(socket_client, is_type("plan_resolved"))
                await self._receive_until(socket_client, is_event("turn_end"))
                await self._receive_until(socket_client, is_idle_state)

            saved = await self._get_json(f"/api/sessions/{session_id}")
            self.assertEqual(saved["agent_mode"], "agent")
            self.assertEqual(saved["plan_kind"], "design")
            self.assertEqual(saved["plan_status"], "approved")

        async def test_switch_suggestion_puts_the_session_into_plan_mode(self) -> None:
            self.responses.extend(
                [
                    LLMResponse(tool_calls=[suggest_call()]),
                    LLMResponse(text="我先看看再写计划"),
                ]
            )
            session_id = await self._create_session()

            async with self._connect(session_id) as socket_client:
                await socket_client.receive_json()
                await socket_client.send_json({"type": "user_message", "text": "帮我重构一下这个模块"})
                request = await self._receive_until(socket_client, is_type("plan_request"))
                self.assertEqual(request["kind"], "switch")
                self.assertIn("改好几个文件", request["reason"])
                await socket_client.send_json(
                    {"type": "plan_answer", "request_id": request["request_id"], "approve": True}
                )
                resolved = await self._receive_until(socket_client, is_type("plan_resolved"))
                await self._receive_until(
                    socket_client, lambda message: message["type"] == "state" and message.get("agent_mode") == "plan"
                )
                await self._receive_until(socket_client, is_event("turn_end"))
                await self._receive_until(socket_client, is_idle_state)

            snapshot = await self._get_json(f"/api/sessions/{session_id}")
            self.assertTrue(resolved["approved"])
            self.assertEqual(snapshot["agent_mode"], "plan")

        async def test_plan_answer_with_feedback_keeps_planning(self) -> None:
            self.responses.extend(
                [
                    LLMResponse(tool_calls=[submit_call()]),
                    LLMResponse(text="好的，我再想想"),
                ]
            )
            session_id = await self._create_session(agent_mode="plan")

            async with self._connect(session_id) as socket_client:
                await socket_client.receive_json()
                await socket_client.send_json({"type": "user_message", "text": "出个计划"})
                request = await self._receive_until(socket_client, is_type("plan_request"))
                await socket_client.send_json(
                    {
                        "type": "plan_answer",
                        "request_id": request["request_id"],
                        "approve": False,
                        "feedback": "补上回滚方案",
                    }
                )
                resolved = await self._receive_until(socket_client, is_type("plan_resolved"))
                await self._receive_until(socket_client, is_event("turn_end"))
                await self._receive_until(socket_client, is_idle_state)

            snapshot = await self._get_json(f"/api/sessions/{session_id}")
            self.assertFalse(resolved["approved"])
            self.assertEqual(resolved["feedback"], "补上回滚方案")
            self.assertEqual(snapshot["agent_mode"], "plan")
            self.assertEqual(snapshot["plan_status"], "pending")

        async def test_mode_commands_switch_the_session(self) -> None:
            session_id = await self._create_session()

            async def run(command: str) -> dict:
                response = await self._send_json("POST", "/api/command", {"text": command, "session_id": session_id})
                return await response.json()

            async def current_mode() -> str:
                return (await self._get_json(f"/api/sessions/{session_id}"))["agent_mode"]

            plan_on = await run("/plan on")
            plan_mode = await current_mode()
            ask = await run("/ask")
            ask_mode = await current_mode()
            design = await run("/design")
            design_mode = await current_mode()
            agent = await run("/agent")
            agent_mode = await current_mode()
            plan_off = await run("/plan off")
            off_mode = await current_mode()

            self.assertEqual(plan_on["action"], "switch_agent_mode")
            self.assertEqual(plan_mode, "plan")
            self.assertEqual(ask["argument"], "ask")
            self.assertEqual(ask_mode, "ask")
            self.assertEqual(design["argument"], "design")
            self.assertEqual(design_mode, "design")
            self.assertEqual(agent["argument"], "agent")
            self.assertEqual(agent_mode, "agent")
            self.assertEqual(plan_off["action"], "switch_agent_mode")
            self.assertEqual(off_mode, "agent")

        async def test_new_task_page_mode_commands_only_report_intent(self) -> None:
            ask = await self._send_json("POST", "/api/command", {"text": "/ask"})
            payload = await ask.json()
            self.assertTrue(payload["handled"])
            self.assertEqual(payload["action"], "switch_agent_mode")
            self.assertEqual(payload["argument"], "ask")
