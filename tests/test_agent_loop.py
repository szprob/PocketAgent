import asyncio
import unittest
from typing import Any

from pocketagent.core.agent import INTERRUPTED_TOOL_RESULT
from pocketagent.core.compaction import MERGE_SUMMARY_INSTRUCTION, SUMMARY_PREFIX
from pocketagent.core.events import EventType
from pocketagent.core.nodes import apply_finished_compaction, start_background_compaction
from pocketagent.core.session import SessionStore
from pocketagent.llm import ContextOverflowError, LLMResponse, ToolCall
from pocketagent.safety.permissions import ApprovalMode, DenyAllApprover
from pocketagent.tools import Tool, ToolContext, ToolKind, ToolResult, default_tool_registry
from tests.helpers import RoutingProvider, TemporaryDirectories, build_harness, roles, tool_call


class SlowTool(Tool):
    """执行时一直等，用来测中断。"""

    name = "slow"
    description = "一直等"
    kind = ToolKind.EXECUTE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        self.started.set()
        await asyncio.sleep(60)
        return ToolResult("不该走到这里")


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_tool_call_then_final_answer(self) -> None:
        (self.directories.workspace / "hello.txt").write_text("hi there\n", encoding="utf-8")
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("call_1", "read_file", '{"path": "hello.txt"}')]),
                LLMResponse(text="文件里写的是 hi there"),
            ],
        )

        final_text = await harness.agent.run_turn("hello.txt 里写了什么")

        self.assertEqual(final_text, "文件里写的是 hi there")
        messages = harness.agent.context.messages
        self.assertEqual(roles(messages), ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[3]["tool_call_id"], "call_1")
        self.assertIn("hi there", messages[3]["content"])
        # 第二次调模型时已经带上了工具结果
        self.assertEqual(roles(harness.provider.received_messages[1])[-1], "tool")
        turn_end = harness.recorder.of_type(EventType.TURN_END)[0]
        self.assertEqual(turn_end.data["stop_reason"], "done")
        session_file = SessionStore(harness.settings.sessions_dir).path_for(harness.agent.session.id)
        self.assertTrue(session_file.is_file())
        trace_file = harness.settings.traces_dir / f"{harness.agent.session.id}.jsonl"
        self.assertIn('"type": "tool_result"', trace_file.read_text(encoding="utf-8"))

    async def test_denied_call_stops_turn_and_skips_the_rest(self) -> None:
        harness = build_harness(
            self.directories,
            [
                LLMResponse(
                    tool_calls=[
                        tool_call("call_1", "bash", '{"command": "touch created_by_bash"}'),
                        tool_call("call_2", "write_file", '{"path": "new.txt", "content": "x"}'),
                    ]
                ),
            ],
            mode=ApprovalMode.ASK,
            approver=DenyAllApprover(),
        )

        await harness.agent.run_turn("建两个文件")

        self.assertEqual(harness.agent.context.turn.stop_reason, "denied")
        self.assertEqual(len(harness.provider.received_messages), 1)
        self.assertFalse((self.directories.workspace / "created_by_bash").exists())
        self.assertFalse((self.directories.workspace / "new.txt").exists())
        tool_messages = [message for message in harness.agent.context.messages if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])

    async def test_read_only_mode_denies_without_asking(self) -> None:
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[tool_call("call_1", "write_file", '{"path": "a.txt", "content": "x"}')])],
            mode=ApprovalMode.READ_ONLY,
        )

        await harness.agent.run_turn("写个文件")

        self.assertEqual(harness.agent.context.turn.stop_reason, "denied")
        self.assertIn("read_only", harness.agent.context.messages[-1]["content"])

    async def test_auto_edit_allows_edits_without_asking(self) -> None:
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("call_1", "write_file", '{"path": "a.txt", "content": "x"}')]),
                LLMResponse(text="写好了"),
            ],
            mode=ApprovalMode.AUTO_EDIT,
            approver=DenyAllApprover(),
        )

        await harness.agent.run_turn("写个文件")

        self.assertEqual((self.directories.workspace / "a.txt").read_text(encoding="utf-8"), "x")
        self.assertEqual(harness.agent.context.turn.stop_reason, "done")

    async def test_bad_calls_are_reported_back_to_model(self) -> None:
        broken_call = ToolCall(
            id="call_2", name="read_file", arguments={}, raw_arguments="{oops", arguments_error="参数不是合法 JSON"
        )
        harness = build_harness(
            self.directories,
            [
                LLMResponse(
                    tool_calls=[
                        tool_call("call_1", "fly", "{}"),
                        broken_call,
                        tool_call("call_3", "read_file", "{}"),
                    ]
                ),
                LLMResponse(text="好的，我换个方法"),
            ],
        )

        await harness.agent.run_turn("试试")

        tool_contents = [message["content"] for message in harness.agent.context.messages if message["role"] == "tool"]
        self.assertIn("没有叫 fly 的工具", tool_contents[0])
        self.assertIn("参数不是合法 JSON", tool_contents[1])
        self.assertIn("缺少必填参数：path", tool_contents[2])
        self.assertEqual(harness.agent.context.turn.final_text, "好的，我换个方法")

    async def test_step_limit_stops_the_turn(self) -> None:
        looping_response = LLMResponse(tool_calls=[tool_call("call_1", "list_dir", "{}")])
        harness = build_harness(self.directories, [looping_response, looping_response, looping_response], max_steps=2)

        await harness.agent.run_turn("一直列目录")

        self.assertEqual(harness.agent.context.turn.stop_reason, "step_limit")
        self.assertEqual(len(harness.provider.received_messages), 2)
        self.assertEqual(len(harness.recorder.of_type(EventType.STEP_LIMIT)), 1)

    def _long_history(self) -> list[dict[str, Any]]:
        """一段大约一万 token 的旧对话，用来把上下文撑满。"""
        return [
            {"role": "user", "content": "第一件事"},
            {"role": "assistant", "content": "很长的回复" * 6000},
            {"role": "user", "content": "第二件事"},
            {"role": "assistant", "content": "好的"},
        ]

    async def test_background_compaction_does_not_block_the_turn(self) -> None:
        provider = RoutingProvider(
            [
                LLMResponse(tool_calls=[tool_call("c1", "list_dir", "{}")]),
                LLMResponse(tool_calls=[tool_call("c2", "list_dir", "{}")]),
                LLMResponse(text="完成"),
            ]
        )
        provider.summary_gate.clear()
        # 阈值极低、窗口极大：第一次工具调用之后就开始后台压缩，但永远到不了必须等的程度
        harness = build_harness(
            self.directories, [], provider=provider, compact_threshold=0.00001, context_window_tokens=10_000_000
        )

        await harness.agent.run_turn("列两次目录")

        self.assertEqual(len(provider.chat_calls), 3)
        self.assertEqual(provider.summary_calls, 1)
        self.assertEqual(len(harness.recorder.of_type(EventType.COMPACTION_STARTED)), 1)
        self.assertEqual(harness.recorder.of_type(EventType.COMPACTION), [])

        provider.summary_gate.set()
        for _ in range(50):
            if harness.recorder.of_type(EventType.COMPACTION):
                break
            await asyncio.sleep(0.01)

        messages = harness.agent.context.messages
        compaction = harness.recorder.of_type(EventType.COMPACTION)[0]
        self.assertTrue(compaction.data["background"])
        self.assertTrue(messages[1]["content"].startswith(SUMMARY_PREFIX))
        # 快照是第一次工具结果之后拍的，之后的第二次调用和最后的回答原样保留
        self.assertEqual(messages[-1]["content"], "完成")
        self.assertIn("c2", [call["id"] for message in messages for call in message.get("tool_calls") or []])
        saved = SessionStore(harness.settings.sessions_dir).load(harness.agent.session.id)
        self.assertTrue(saved.messages[1]["content"].startswith(SUMMARY_PREFIX))

    async def test_near_the_limit_the_turn_waits_for_the_background_summary(self) -> None:
        provider = RoutingProvider([LLMResponse(text="接着做完了")])
        provider.summary_gate.clear()
        harness = build_harness(self.directories, [], provider=provider, context_window_tokens=8000)
        harness.agent.context.messages.extend(self._long_history())
        await harness.agent.start_compaction()

        turn = asyncio.create_task(harness.agent.run_turn("继续"))
        # 等到这一轮真的停下来等压缩了，再放行摘要
        for _ in range(500):
            if harness.recorder.of_type(EventType.COMPACTION_WAITING):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(provider.chat_calls, [])
        provider.summary_gate.set()
        await turn

        self.assertEqual(len(harness.recorder.of_type(EventType.COMPACTION_WAITING)), 1)
        messages = harness.agent.context.messages
        self.assertTrue(messages[1]["content"].startswith(SUMMARY_PREFIX))
        # 末尾的原文（第二轮对话）保留，没进摘要
        self.assertEqual(
            roles(messages), ["system", "user", "user", "assistant", "user", "assistant"]
        )
        self.assertEqual(provider.summary_calls, 1)

    async def test_second_compaction_merges_into_the_previous_summary(self) -> None:
        provider = RoutingProvider([])
        provider.summary_gate.set()
        harness = build_harness(self.directories, [], provider=provider)
        context = harness.agent.context
        context.messages.extend(self._long_history())

        await start_background_compaction(context, reason="manual")
        await asyncio.wait([context.pending_compaction.task])
        self.assertTrue(await apply_finished_compaction(context))
        first_summary = context.messages[1]["content"]
        self.assertTrue(first_summary.startswith(SUMMARY_PREFIX))

        # 第二次压缩走「合并」：旧摘要原样放在请求里，不再从头重压
        context.messages.extend(self._long_history())
        await start_background_compaction(context, reason="manual")
        await asyncio.wait([context.pending_compaction.task])
        self.assertTrue(await apply_finished_compaction(context))

        self.assertEqual(provider.summary_calls, 2)
        request = provider.summary_requests[1]
        self.assertEqual(request[-1]["content"], MERGE_SUMMARY_INSTRUCTION)
        self.assertEqual(request[1]["content"], first_summary)

    async def test_stale_background_summary_is_discarded(self) -> None:
        provider = RoutingProvider([])
        provider.summary_gate.clear()
        harness = build_harness(self.directories, [], provider=provider)
        context = harness.agent.context
        context.messages.extend(self._long_history())
        await harness.agent.start_compaction()
        # 期间历史被别的压缩换掉了
        context.history_version += 1
        before = list(context.messages)
        provider.summary_gate.set()
        await asyncio.wait([context.pending_compaction.task])

        self.assertFalse(await apply_finished_compaction(context))
        self.assertEqual(context.messages, before)

    async def test_context_overflow_error_compacts_and_retries(self) -> None:
        provider = RoutingProvider([ContextOverflowError("maximum context length exceeded"), LLMResponse(text="好了")])
        harness = build_harness(self.directories, [], provider=provider)
        harness.agent.context.messages.extend(self._long_history())

        final_text = await harness.agent.run_turn("继续")

        compaction = harness.recorder.of_type(EventType.COMPACTION)[0]
        self.assertEqual(compaction.data["reason"], "overflow")
        self.assertEqual(final_text, "好了")
        self.assertEqual(len(provider.chat_calls), 2)
        self.assertLess(len(provider.chat_calls[1]), len(provider.chat_calls[0]))

    async def test_auto_compact_off_never_compacts_on_its_own(self) -> None:
        provider = RoutingProvider([LLMResponse(text="好")])
        harness = build_harness(self.directories, [], provider=provider, context_window_tokens=8000, auto_compact=False)
        harness.agent.context.messages.extend(self._long_history())

        await harness.agent.run_turn("继续")

        self.assertEqual(provider.summary_calls, 0)
        self.assertEqual(harness.recorder.of_type(EventType.COMPACTION_STARTED), [])

    async def test_cancel_mid_tool_closes_dangling_tool_calls(self) -> None:
        slow_tool = SlowTool()
        tools = default_tool_registry()
        tools.register(slow_tool)
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[tool_call("call_1", "slow", "{}")])],
            tools=tools,
        )

        turn_task = asyncio.create_task(harness.agent.run_turn("跑个慢的"))
        await asyncio.wait_for(slow_tool.started.wait(), timeout=5)
        turn_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn_task

        last_message = harness.agent.context.messages[-1]
        self.assertEqual(last_message["role"], "tool")
        self.assertEqual(last_message["tool_call_id"], "call_1")
        self.assertEqual(last_message["content"], INTERRUPTED_TOOL_RESULT)
        saved = SessionStore(harness.settings.sessions_dir).load(harness.agent.session.id)
        self.assertEqual(saved.messages[-1]["content"], INTERRUPTED_TOOL_RESULT)

    async def test_resumed_session_keeps_history_and_single_system_prompt(self) -> None:
        first = build_harness(self.directories, [LLMResponse(text="第一轮")])
        await first.agent.run_turn("你好")
        stored_session = SessionStore(first.settings.sessions_dir).load(first.agent.session.id)

        second = build_harness(self.directories, [LLMResponse(text="第二轮")], session=stored_session)
        await second.agent.run_turn("还记得吗")

        self.assertEqual(roles(second.agent.context.messages), ["system", "user", "assistant", "user", "assistant"])


if __name__ == "__main__":
    unittest.main()
