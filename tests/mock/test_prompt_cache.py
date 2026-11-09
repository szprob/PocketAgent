"""前缀缓存：两次压缩之间，发给模型的系统提示、工具定义和已有的历史只追加、不改。

中途改一个字，接口的前缀缓存就从改动处往后全部失效，整段历史按原价重算。所以模式切换、日期变了
追加一条提示；工具增删先换注册表，模型看到的定义等下一次压缩；压缩时一起重建。
"""

import unittest
from unittest import mock

from pocketagent.core.session import Session
from pocketagent.interaction.modes import AgentMode
from pocketagent.llm import LLMResponse, without_earlier_reasoning
from pocketagent.server.history import history_items
from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolRegistry, ToolResult
from tests.helpers import TemporaryDirectories, build_harness, tool_call


class EchoTool(Tool):
    name = "echo"
    description = "原样返回"
    kind = ToolKind.READ
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, arguments, context: ToolContext) -> ToolResult:
        return ToolResult(str(arguments.get("text", "")))


class ShoutTool(EchoTool):
    name = "shout"
    description = "大声返回"


def tool_names(schemas: list[dict]) -> list[str]:
    return [schema["function"]["name"] for schema in schemas]


class PromptCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_mode_switch_appends_a_notice_instead_of_rewriting_the_system_prompt(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="第一轮"), LLMResponse(text="好，只调研")])
        await harness.agent.run_turn("你好")
        system_before = harness.agent.context.messages[0]["content"]

        await harness.agent.set_agent_mode(AgentMode.PLAN)
        await harness.agent.run_turn("先看看再说")

        second_request = harness.provider.received_messages[1]
        self.assertEqual(second_request[0]["content"], system_before)
        self.assertEqual(harness.provider.received_messages[0][0]["content"], system_before)
        notice = second_request[-1]
        self.assertEqual(notice["role"], "user")
        self.assertIn("现在处于【计划模式】", notice["content"])
        # 只给模型看：界面重放历史时不显示
        texts = [item.get("text") for item in history_items(harness.agent.context.messages, harness.agent.context.tools)]
        self.assertNotIn(notice["content"], texts)
        # 权限门禁照常立刻按新模式拦
        self.assertEqual(harness.agent.context.permissions.agent_mode, AgentMode.PLAN)

    async def test_before_the_first_reply_the_system_prompt_is_simply_rebuilt(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="好")])

        await harness.agent.set_agent_mode(AgentMode.PLAN)
        await harness.agent.run_turn("先看看")

        request = harness.provider.received_messages[0]
        self.assertIn("现在处于【计划模式】", request[0]["content"])
        self.assertFalse(any(message.get("content", "").startswith("（工作模式切换了") for message in request[1:]))

    async def test_tool_changes_wait_for_the_next_compaction(self) -> None:
        harness = build_harness(
            self.directories,
            [LLMResponse(text="第一轮"), LLMResponse(text="第二轮"), LLMResponse(text="这是摘要"), LLMResponse(text="第三轮")],
            tools=ToolRegistry([EchoTool()]),
        )
        await harness.agent.run_turn("一")
        system_before = harness.agent.context.messages[0]["content"]

        # 改设置后工具注册表换了：能执行的以注册表为准，模型看到的定义先不变
        harness.agent.context.tools = ToolRegistry([EchoTool(), ShoutTool()])
        harness.agent._rebuild_unless_cached()
        await harness.agent.run_turn("二")

        self.assertEqual(tool_names(harness.provider.received_tools[1]), ["echo"])
        self.assertEqual(harness.agent.context.messages[0]["content"], system_before)

        await harness.agent.compact()
        await harness.agent.run_turn("三")

        self.assertEqual(tool_names(harness.provider.received_tools[-1]), ["echo", "shout"])
        self.assertIn("shout", harness.agent.context.messages[0]["content"])

    async def test_a_new_day_adds_a_date_notice_and_keeps_the_system_prompt(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="一"), LLMResponse(text="二")])
        await harness.agent.run_turn("今天几号")
        system_before = harness.agent.context.messages[0]["content"]

        with mock.patch("pocketagent.core.agent._today", return_value="2099-01-02"):
            await harness.agent.run_turn("那现在呢")

        request = harness.provider.received_messages[1]
        self.assertEqual(request[0]["content"], system_before)
        self.assertIn("今天是 2099-01-02", request[-1]["content"])

    async def test_a_resumed_session_keeps_its_stored_system_prompt(self) -> None:
        stored = Session(
            id="resumed",
            workspace=str(self.directories.workspace),
            messages=[
                {"role": "system", "content": "旧的系统提示\n- 今天：2000-01-01"},
                {"role": "user", "content": "之前的问题"},
                {"role": "assistant", "content": "之前的回答"},
            ],
        )
        harness = build_harness(self.directories, [LLMResponse(text="接着来")], session=stored)

        await harness.agent.run_turn("继续")

        request = harness.provider.received_messages[0]
        self.assertEqual(request[0]["content"], "旧的系统提示\n- 今天：2000-01-01")
        # 存下来的提示里日期已经过了，开头补一条
        self.assertTrue(any("系统提示里写的日期已经过时" in str(message.get("content")) for message in request))

    async def test_tool_results_and_screenshots_stay_in_history_until_compaction(self) -> None:
        """旧截图也不在中途删：删了前缀就变了。"""
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("c1", "echo", '{"text": "a"}')]),
                LLMResponse(tool_calls=[tool_call("c2", "echo", '{"text": "b"}')]),
                LLMResponse(text="完"),
            ],
            tools=ToolRegistry([EchoTool()]),
        )

        await harness.agent.run_turn("跑两次")

        first, second, third = harness.provider.received_messages
        self.assertEqual(second[: len(first)], first)
        self.assertEqual(third[: len(second)], second)


class NoticesDoNotStartANewTurnTest(unittest.TestCase):
    def test_reasoning_before_an_inserted_notice_is_still_this_turn(self) -> None:
        messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": None, "reasoning_content": "这一轮的思考", "tool_calls": []},
            {"role": "user", "content": "（工作模式切换了…）", "_kind": "context"},
        ]

        self.assertEqual(without_earlier_reasoning(messages)[1]["reasoning_content"], "这一轮的思考")
