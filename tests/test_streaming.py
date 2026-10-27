"""bash 流式输出：工具回调、事件顺序、取消和 trace 过滤。"""

import asyncio
import contextlib
import io
import json
import unittest

from pocketagent.cli import ConsoleRenderer, Style
from pocketagent.events import Event, EventType
from pocketagent.llm import LLMResponse
from pocketagent.tools import ToolContext
from pocketagent.tools.base import current_tool_output
from pocketagent.tools.shell import BashTool
from tests.helpers import TemporaryDirectories, build_harness, tool_call


class BashStreamingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.context = ToolContext(workspace=self.directories.workspace, shell_timeout_seconds=10)

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_output_callback_receives_chunks_before_the_command_ends(self) -> None:
        chunks: list[str] = []

        async def sink(text: str) -> None:
            chunks.append(text)

        token = current_tool_output.set(sink)
        try:
            result = await BashTool().run(
                {"command": "printf 'first\\n'; sleep 0.3; printf 'second\\n'"}, self.context
            )
        finally:
            current_tool_output.reset(token)

        self.assertIn("first", "".join(chunks))
        self.assertIn("first", result.content)
        self.assertIn("second", result.content)

    async def test_delta_event_comes_before_tool_result(self) -> None:
        command = "printf 'first\\n'; sleep 0.3; printf 'second\\n'"
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("call_1", "bash", json.dumps({"command": command}))]),
                LLMResponse(text="跑完了"),
            ],
            sandbox_enabled=False,
        )

        await harness.agent.run_turn("跑一下")

        events = harness.recorder.events
        delta_positions = [
            index for index, event in enumerate(events) if event.type == EventType.TOOL_OUTPUT_DELTA
        ]
        result_position = next(
            index for index, event in enumerate(events) if event.type == EventType.TOOL_RESULT
        )
        self.assertTrue(delta_positions)
        self.assertLess(min(delta_positions), result_position)
        streamed = "".join(
            event.data["text"] for event in harness.recorder.of_type(EventType.TOOL_OUTPUT_DELTA)
        )
        self.assertIn("first", streamed)
        tool_result = harness.recorder.of_type(EventType.TOOL_RESULT)[0]
        self.assertIn("second", tool_result.data["content"])

        trace_path = harness.settings.traces_dir / f"{harness.agent.session.id}.jsonl"
        self.assertNotIn('"type": "tool_output_delta"', trace_path.read_text(encoding="utf-8"))

    async def test_cancel_kills_the_running_command(self) -> None:
        task = asyncio.create_task(BashTool().run({"command": "sleep 30"}, self.context))
        await asyncio.sleep(0.2)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_console_renderer_prints_streamed_output_only_once(self) -> None:
        renderer = ConsoleRenderer(Style(enabled=False))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            renderer(Event(EventType.TOOL_CALL, {"call_id": "c1", "name": "bash", "summary": "echo"}))
            renderer(
                Event(
                    EventType.TOOL_OUTPUT_DELTA,
                    {"call_id": "c1", "name": "bash", "text": "first\n"},
                )
            )
            renderer(
                Event(
                    EventType.TOOL_RESULT,
                    {
                        "call_id": "c1",
                        "name": "bash",
                        "content": "first\n[exit code 0]",
                        "is_error": False,
                        "denied": False,
                    },
                )
            )

        printed = output.getvalue()
        self.assertEqual(printed.count("first"), 1)
        self.assertIn("[exit code 0]", printed)
