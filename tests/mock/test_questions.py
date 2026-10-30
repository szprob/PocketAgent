"""ask_user：向用户提问。"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pocketagent.cli import ConsoleApprover, Style
from pocketagent.interaction.questions import (
    NO_ONE_TO_ASK,
    QuestionAnswer,
    QuestionError,
    format_answers,
    parse_questions,
)
from pocketagent.llm import LLMResponse
from pocketagent.tools import ToolContext
from pocketagent.tools.ask import AskUserTool
from tests.helpers import TemporaryDirectories, build_harness, tool_call

QUESTIONS = {
    "questions": [
        {
            "question": "数据库用哪个？",
            "header": "数据库选型这个标签实在太长了吧",
            "options": [
                {"label": "PostgreSQL（推荐）", "description": "功能全"},
                {"label": "SQLite", "description": "零部署"},
                {"label": "其他"},
            ],
        },
        {
            "question": "要哪些测试？",
            "header": "测试",
            "multi_select": True,
            "options": [{"label": "单元测试"}, {"label": "集成测试"}, {"label": "端到端"}],
        },
    ]
}


class AnsweringApprover:
    """测试用：问题按剧本回答，记下问了什么。"""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def request_approval(self, request):
        raise AssertionError("这个测试里不该有审批")

    async def ask_questions(self, questions):
        self.asked.append(questions)
        return self.answers


class ParseQuestionsTest(unittest.TestCase):
    def test_normalizes_and_drops_the_models_own_other_option(self) -> None:
        questions = parse_questions(QUESTIONS)

        self.assertEqual([option.label for option in questions[0].options], ["PostgreSQL（推荐）", "SQLite"])
        self.assertEqual(questions[0].header, "数据库选型这个标签实在太")
        self.assertTrue(questions[1].multi_select)

    def test_rejects_bad_shapes(self) -> None:
        bad_inputs = [
            {},
            {"questions": []},
            {"questions": [{"question": f"问题{i}？", "options": ["a", "b"]} for i in range(5)]},
            {"questions": [{"question": "只有一个选项？", "options": ["a", "其他"]}]},
            {"questions": [{"question": "选项太多？", "options": ["a", "b", "c", "d", "e"]}]},
            {"questions": [{"question": "重名？", "options": ["a", "a"]}]},
            {"questions": [{"question": "一样？", "options": ["a", "b"]}, {"question": "一样？", "options": ["a", "b"]}]},
            {"questions": [{"options": ["a", "b"]}]},
        ]
        for arguments in bad_inputs:
            with self.subTest(arguments=json.dumps(arguments, ensure_ascii=False)[:60]):
                with self.assertRaises(QuestionError):
                    parse_questions(arguments)

    def test_format_answers(self) -> None:
        questions = parse_questions(QUESTIONS)

        answered = format_answers(
            questions,
            [QuestionAnswer(selected=["SQLite"]), QuestionAnswer(selected=["单元测试", "端到端"], other="还要压测")],
        )
        partial = format_answers(questions, [QuestionAnswer(other="用 MySQL"), QuestionAnswer()])
        skipped = format_answers(questions, None)

        self.assertIn("1. 〔数据库选型这个标签实在太〕数据库用哪个？\n   → SQLite", answered)
        self.assertIn("→ 单元测试、端到端；用户自己写的：还要压测", answered)
        self.assertIn("→ 用户自己写的：用 MySQL", partial)
        self.assertIn("这个没回答", partial)
        self.assertIn("跳过", skipped)


class AskUserToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_without_anyone_to_ask_the_model_is_told_to_decide(self) -> None:
        result = await AskUserTool().run(QUESTIONS, ToolContext(workspace=self.directories.workspace))

        self.assertEqual(result.content, NO_ONE_TO_ASK)
        self.assertFalse(result.is_error)

    async def test_bad_questions_go_back_to_the_model(self) -> None:
        result = await AskUserTool().run({"questions": []}, ToolContext(workspace=self.directories.workspace))

        self.assertTrue(result.is_error)

    async def test_agent_asks_and_gets_the_answers_without_approval(self) -> None:
        approver = AnsweringApprover([QuestionAnswer(selected=["SQLite"]), QuestionAnswer(other="都要")])
        responses = [
            LLMResponse(tool_calls=[tool_call("c1", "ask_user", json.dumps(QUESTIONS, ensure_ascii=False))]),
            LLMResponse(text="好的，用 SQLite"),
        ]
        harness = build_harness(self.directories, responses, approver=approver)

        await harness.agent.run_turn("帮我搭个后端")

        tool_message = next(m for m in harness.agent.context.messages if m["role"] == "tool")
        self.assertEqual(len(approver.asked), 1)
        self.assertIn("→ SQLite", tool_message["content"])
        self.assertIn("用户自己写的：都要", tool_message["content"])
        self.assertIn("ask_user", harness.agent.context.messages[0]["content"])


class ConsoleQuestionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_numbers_other_text_and_blank_to_skip(self) -> None:
        questions = parse_questions(QUESTIONS)
        typed = iter(["9", "1,2", "2", "0,1", "压测也要"])
        approver = ConsoleApprover(Style(enabled=False))

        with mock.patch("builtins.input", side_effect=lambda prompt="": next(typed)), redirect_stdout(io.StringIO()) as output:
            answers = await approver.ask_questions(questions)

        self.assertEqual(answers[0], QuestionAnswer(selected=["SQLite"]))
        self.assertEqual(answers[1], QuestionAnswer(selected=["单元测试"], other="压测也要"))
        self.assertIn("这一题只能选一个", output.getvalue())
        self.assertIn("0. 其他（自己写）", output.getvalue())

    async def test_blank_skips_a_question(self) -> None:
        questions = parse_questions(QUESTIONS)
        typed = iter(["", "3"])

        with mock.patch("builtins.input", side_effect=lambda prompt="": next(typed)), redirect_stdout(io.StringIO()):
            answers = await ConsoleApprover(Style(enabled=False)).ask_questions(questions)

        self.assertTrue(answers[0].is_empty())
        self.assertEqual(answers[1].selected, ["端到端"])


if __name__ == "__main__":
    unittest.main()
