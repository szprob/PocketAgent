import unittest
from types import SimpleNamespace

from pocketagent.llm import LLMResponse, build_assistant_message, without_earlier_reasoning
from pocketagent.llm.openai_compat import StreamAccumulator, _consume_delta
from pocketagent.safety.permissions import ApprovalMode, Decision, decide_by_mode
from pocketagent.tools import ToolKind


def tool_delta(index: int, call_id: str | None = None, name: str | None = None, arguments: str | None = None):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def content_delta(content: str | None = None, reasoning: str | None = None, tool_calls: list | None = None):
    return SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)


class RecordingListener:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []

    async def on_text(self, text: str) -> None:
        self.text.append(text)

    async def on_reasoning(self, text: str) -> None:
        self.reasoning.append(text)


class StreamAccumulatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_merges_interleaved_tool_call_fragments(self) -> None:
        accumulator = StreamAccumulator()
        listener = RecordingListener()
        deltas = [
            content_delta(reasoning="想一想"),
            content_delta(content="我来看看"),
            content_delta(tool_calls=[tool_delta(0, "call_a", "read_file", '{"pa')]),
            content_delta(tool_calls=[tool_delta(1, "call_b", "grep", '{"pattern": "x"}')]),
            content_delta(tool_calls=[tool_delta(0, arguments='th": "a.py"}')]),
        ]
        for delta in deltas:
            await _consume_delta(delta, accumulator, listener)

        response = accumulator.build_response()

        self.assertEqual(response.text, "我来看看")
        self.assertEqual(response.reasoning, "想一想")
        self.assertEqual(listener.text, ["我来看看"])
        self.assertEqual([call.id for call in response.tool_calls], ["call_a", "call_b"])
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(response.tool_calls[1].arguments, {"pattern": "x"})

    async def test_openrouter_reasoning_details_are_stitched_back_by_index(self) -> None:
        """OpenRouter 把一段思考按 index 拆成很多片下发（实测 nemotron / dots 这样），签名在最后一片；拼回去才能原样传回。"""
        accumulator = StreamAccumulator()
        pieces = [
            [{"type": "reasoning.text", "text": "先查", "format": "anthropic-claude-v1", "index": 0}],
            [{"type": "reasoning.text", "text": "日期", "signature": None, "index": 0}],
            [{"type": "reasoning.text", "text": "", "signature": "sig-1", "index": 0}],
            [{"type": "reasoning.encrypted", "data": "ENC", "format": "anthropic-claude-v1", "index": 1}],
        ]
        for details in pieces:
            await _consume_delta(SimpleNamespace(content=None, reasoning="x", tool_calls=None, reasoning_details=details), accumulator, RecordingListener())

        response = accumulator.build_response()

        self.assertEqual(
            response.reasoning_details,
            [
                {"type": "reasoning.text", "text": "先查日期", "format": "anthropic-claude-v1", "index": 0, "signature": "sig-1"},
                {"type": "reasoning.encrypted", "data": "ENC", "format": "anthropic-claude-v1", "index": 1},
            ],
        )
        message = build_assistant_message(response)
        self.assertEqual(message["reasoning_details"], response.reasoning_details)

    async def test_gemini_signature_on_a_tool_call_is_kept(self) -> None:
        accumulator = StreamAccumulator()
        signature = {"google": {"thought_signature": "g-sig"}}
        first = SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="f", arguments="{}"), extra_content=signature)

        await _consume_delta(content_delta(tool_calls=[first]), accumulator, RecordingListener())

        message = build_assistant_message(accumulator.build_response())
        self.assertEqual(message["tool_calls"][0]["extra_content"], signature)

    def test_invalid_json_arguments_are_flagged_not_raised(self) -> None:
        accumulator = StreamAccumulator()
        accumulator.add_tool_call_delta(tool_delta(0, None, "bash", '{"command": "ls'))

        call = accumulator.build_response().tool_calls[0]

        self.assertEqual(call.id, "call_0")
        self.assertEqual(call.arguments, {})
        self.assertIn("不是合法 JSON", call.arguments_error)

    def test_assistant_message_with_only_tool_calls_has_null_content(self) -> None:
        accumulator = StreamAccumulator()
        accumulator.add_tool_call_delta(tool_delta(0, "call_a", "list_dir", ""))

        message = build_assistant_message(accumulator.build_response())

        self.assertIsNone(message["content"])
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(build_assistant_message(LLMResponse(text=""))["content"], "")

    def test_without_earlier_reasoning_keeps_only_the_current_turn(self) -> None:
        messages = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "答", "reasoning_content": "旧的"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": None, "reasoning_content": "新的", "tool_calls": []},
            {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        ]

        cleaned = without_earlier_reasoning(messages)

        self.assertNotIn("reasoning_content", cleaned[1])
        self.assertEqual(cleaned[3]["reasoning_content"], "新的")
        self.assertEqual(messages[1]["reasoning_content"], "旧的")

    def test_assistant_message_keeps_reasoning_only_when_there_is_some(self) -> None:
        with_reasoning = build_assistant_message(LLMResponse(text="好", reasoning="先想清楚"))
        without = build_assistant_message(LLMResponse(text="好"))

        self.assertEqual(with_reasoning["reasoning_content"], "先想清楚")
        self.assertNotIn("reasoning_content", without)


class PermissionModeTest(unittest.TestCase):
    def test_decision_table(self) -> None:
        expected = {
            (ApprovalMode.READ_ONLY, ToolKind.READ): Decision.ALLOW,
            (ApprovalMode.READ_ONLY, ToolKind.EDIT): Decision.DENY,
            (ApprovalMode.READ_ONLY, ToolKind.EXECUTE): Decision.DENY,
            (ApprovalMode.ASK, ToolKind.READ): Decision.ALLOW,
            (ApprovalMode.ASK, ToolKind.EDIT): Decision.ASK,
            (ApprovalMode.ASK, ToolKind.EXECUTE): Decision.ASK,
            (ApprovalMode.AUTO_EDIT, ToolKind.EDIT): Decision.ALLOW,
            (ApprovalMode.AUTO_EDIT, ToolKind.EXECUTE): Decision.ASK,
            (ApprovalMode.FULL_AUTO, ToolKind.EXECUTE): Decision.ALLOW,
        }
        for (mode, kind), decision in expected.items():
            with self.subTest(mode=mode, kind=kind):
                self.assertEqual(decide_by_mode(mode, kind), decision)


if __name__ == "__main__":
    unittest.main()
