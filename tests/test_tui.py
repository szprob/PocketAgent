"""Textual TUI：流式对话、斜杠命令、审批 / 提问桥接和中断。"""

import asyncio
import json
import unittest

from pocketagent.core.agent import Agent
from pocketagent.core.events import Event, EventType
from pocketagent.llm import LLMResponse
from pocketagent.safety.permissions import ApprovalMode
from tests.helpers import TemporaryDirectories, build_harness, tool_call

try:
    from pocketagent.cli.tui import ApprovalScreen, PocketAgentTui, QuestionScreen
    from textual.widgets import Input, SelectionList, TextArea

    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False


async def wait_until(pilot, predicate, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.03)
    raise AssertionError("等待 TUI 状态超时")


class BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, messages, tools, listener):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@unittest.skipUnless(TEXTUAL_AVAILABLE, "需要可选依赖 textual")
class PocketAgentTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    def make_app(self, responses, **kwargs):
        harness = build_harness(self.directories, responses, **kwargs)
        app = PocketAgentTui(harness.settings, agent=harness.agent)
        return app, harness

    async def test_enter_runs_a_turn_and_streams_text_without_markup(self) -> None:
        app, harness = self.make_app([LLMResponse(text="你好，[这不是链接]")])
        async with app.run_test(size=(100, 32)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.text = "打个招呼"
            prompt.focus()
            await pilot.press("enter")
            await wait_until(pilot, lambda: len(app.query(".assistant")) > 0)
            await wait_until(pilot, lambda: not app.busy)
            await pilot.pause()

            assistant = app.query(".assistant").last()
            self.assertEqual(assistant.content.plain, "你好，[这不是链接]")
            self.assertEqual(prompt.text, "")
            self.assertFalse(app.busy)
            self.assertEqual(harness.agent.session.first_user_text(), "打个招呼")

    async def test_first_prompt_and_multiline_keys(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="已收到")])
        app = PocketAgentTui(harness.settings, agent=harness.agent, first_prompt="第一句话")
        async with app.run_test() as pilot:
            await wait_until(pilot, lambda: len(app.query(".assistant")) > 0)
            await wait_until(pilot, lambda: not app.busy)
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            prompt.text = "第一行"
            prompt.move_cursor((0, len("第一行")))
            await pilot.press("shift+enter")
            await pilot.press("ctrl+j")
            prompt.insert("第二行")

            self.assertEqual(prompt.text, "第一行\n\n第二行")
            self.assertEqual(harness.agent.session.first_user_text(), "第一句话")

    async def test_slash_command_uses_the_shared_controller(self) -> None:
        app, harness = self.make_app([], mode=ApprovalMode.ASK)
        async with app.run_test():
            await app.submit_text("/mode full_auto")
            await pilot_pause(app)

            self.assertEqual(harness.agent.context.permissions.mode, ApprovalMode.FULL_AUTO)
            self.assertIn("full_auto", app.query(".info").last().content.plain)

    async def test_approval_dialog_allows_the_tool_and_the_turn_finishes(self) -> None:
        responses = [
            LLMResponse(
                tool_calls=[
                    tool_call("write-1", "write_file", json.dumps({"path": "note.txt", "content": "approved"}))
                ]
            ),
            LLMResponse(text="写好了"),
        ]
        app, harness = self.make_app(responses, mode=ApprovalMode.ASK)
        harness.agent.context.permissions.approver = app.approver
        async with app.run_test(size=(110, 36)) as pilot:
            await app.submit_text("写一个文件")
            await wait_until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.pause()
            await pilot.press("y")
            await wait_until(pilot, lambda: not app.busy)
            await pilot.pause()

            self.assertEqual((self.directories.workspace / "note.txt").read_text(encoding="utf-8"), "approved")
            self.assertFalse(app.busy)
            self.assertNotIsInstance(app.screen, ApprovalScreen)

    async def test_question_dialog_returns_selected_option_and_other_text(self) -> None:
        arguments = {
            "questions": [
                {
                    "question": "选哪个数据库？",
                    "options": [{"label": "SQLite"}, {"label": "PostgreSQL"}],
                }
            ]
        }
        responses = [
            LLMResponse(tool_calls=[tool_call("ask-1", "ask_user", json.dumps(arguments, ensure_ascii=False))]),
            LLMResponse(text="收到"),
        ]
        app, harness = self.make_app(responses)
        harness.agent.context.tool_context.ask_user = app.approver.ask_questions
        async with app.run_test(size=(100, 34)) as pilot:
            await app.submit_text("帮我选一下")
            await wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            await pilot.pause()
            app.screen.query_one("#question-0", SelectionList).select(1)
            app.screen.query_one("#other-0", Input).value = "先只本机用"
            await pilot.click("#submit-answers")
            await wait_until(pilot, lambda: not app.busy)
            await pilot.pause()

            tool_message = next(message for message in harness.agent.context.messages if message["role"] == "tool")
            self.assertIn("PostgreSQL", tool_message["content"])
            self.assertIn("先只本机用", tool_message["content"])

    async def test_ctrl_c_cancels_only_the_current_turn(self) -> None:
        provider = BlockingProvider()
        app, harness = self.make_app([], provider=provider)
        async with app.run_test() as pilot:
            await app.submit_text("等一下")
            await wait_until(pilot, provider.started.is_set)
            app.action_interrupt()
            await wait_until(pilot, lambda: not app.busy)
            await pilot.pause()

            self.assertTrue(provider.cancelled)
            self.assertFalse(app.busy)
            self.assertIsInstance(app.agent, Agent)
            self.assertEqual(app.agent.session.first_user_text(), "等一下")


@unittest.skipUnless(TEXTUAL_AVAILABLE, "需要可选依赖 textual")
class TuiRenderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_tool_output_is_not_duplicated_in_the_result(self) -> None:
        directories = TemporaryDirectories()
        self.addCleanup(directories.cleanup)
        harness = build_harness(directories, [])
        app = PocketAgentTui(harness.settings, agent=harness.agent)
        async with app.run_test():
            app.render_event(Event(EventType.TOOL_CALL, {"call_id": "c1", "name": "bash", "summary": "printf"}))
            app.render_event(Event(EventType.TOOL_OUTPUT_DELTA, {"call_id": "c1", "text": "first\n"}))
            app.render_event(
                Event(
                    EventType.TOOL_RESULT,
                    {"call_id": "c1", "content": "first\n[exit code 0]", "is_error": False, "denied": False},
                )
            )
            content = app.tool_displays["c1"].widget.content.plain
            self.assertEqual(content.count("first"), 1)
            self.assertIn("[exit code 0]", content)


async def pilot_pause(app) -> None:
    """直接调用 submit_text 的测试没有 pilot，也让已排队的控件更新完成。"""
    await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
