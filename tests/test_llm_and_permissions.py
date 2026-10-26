import unittest
from types import SimpleNamespace

from pocketagent.llm import LLMResponse, build_assistant_message
from pocketagent.llm.openai_compat import StreamAccumulator, _consume_delta
from pocketagent.permissions import ApprovalMode, Decision, decide_by_mode
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
