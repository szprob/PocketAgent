"""子 agent：主 agent 用 subagent 工具派出去，自己的上下文，做完交回结论。"""

import asyncio
import json
import unittest

from pocketagent.core.events import EventType
from pocketagent.core.subagents import subagent_tools
from pocketagent.llm import LLMResponse
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalMode
from pocketagent.tools import ToolContext, default_tool_registry
from pocketagent.tools.subagent import SubagentTool
from tests.helpers import TemporaryDirectories, build_harness, tool_call

SUBAGENT_MARK = "# 你现在是子 agent"


class TwoLevelProvider:
    """主 agent 和子 agent 各走各的剧本：系统提示里有子 agent 那段就是子 agent。
    子 agent 的剧本按任务标题分开，可以用 gate 卡住，看几个子 agent 是不是同时在跑。"""

    def __init__(self, parent_script, child_scripts) -> None:
        self.parent_script = list(parent_script)
        self.child_scripts = {title: list(script) for title, script in child_scripts.items()}
        self.child_requests: dict[str, list] = {}
        self.running_children: set[str] = set()
        self.max_parallel = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def complete(self, messages, tools, listener):
        await asyncio.sleep(0)
        system = messages[0]["content"]
        if SUBAGENT_MARK not in system:
            response = self.parent_script.pop(0)
        else:
            title = system.split("标题是「", 1)[1].split("」", 1)[0]
            self.child_requests.setdefault(title, []).append({"messages": messages, "tools": [tool["function"]["name"] for tool in tools]})
            self.running_children.add(title)
            self.max_parallel = max(self.max_parallel, len(self.running_children))
            await self.gate.wait()
            response = self.child_scripts[title].pop(0)
            if not self.child_scripts[title]:
                self.running_children.discard(title)
        if isinstance(response, Exception):
            raise response
        if response.text:
            await listener.on_text(response.text)
        return response


def spawn(call_id: str, description: str, prompt: str, kind: str = "explore"):
    arguments = json.dumps({"description": description, "prompt": prompt, "type": kind}, ensure_ascii=False)
    return tool_call(call_id, "subagent", arguments)


class RecordingApprover:
    def __init__(self, answer=ApprovalAnswer.ALLOW_ONCE) -> None:
        self.answer = answer
        self.requests = []

    async def request_approval(self, request):
        self.requests.append(request)
        return self.answer

    async def ask_questions(self, questions):
        raise AssertionError("子 agent 不该问用户")


class SubagentToolsTest(unittest.TestCase):
    def test_explore_gets_read_only_tools_and_nobody_gets_ask_or_spawn(self) -> None:
        registry = default_tool_registry(sandboxed=True)

        explore = subagent_tools(registry, "explore").names()
        general = subagent_tools(registry, "general").names()

        self.assertIn("read_file", explore)
        self.assertNotIn("bash", explore)
        self.assertNotIn("write_file", explore)
        self.assertIn("bash", general)
        for names in (explore, general):
            self.assertNotIn("ask_user", names)
            self.assertNotIn("subagent", names)


class SubagentRunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        (self.directories.workspace / "notes.txt").write_text("答案是 42\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_child_runs_with_its_own_context_and_hands_back_a_conclusion(self) -> None:
        provider = TwoLevelProvider(
            [LLMResponse(tool_calls=[spawn("p1", "找答案", "读 notes.txt，告诉我答案是多少")]), LLMResponse(text="答案是 42")],
            {"找答案": [LLMResponse(tool_calls=[tool_call("c1", "read_file", '{"path": "notes.txt"}')]), LLMResponse(text="notes.txt 第 1 行：答案是 42")]},
        )
        harness = build_harness(self.directories, [], provider=provider)

        await harness.agent.run_turn("答案是多少？")

        child = provider.child_requests["找答案"][0]
        self.assertEqual(child["messages"][1], {"role": "user", "content": "读 notes.txt，告诉我答案是多少"})
        self.assertIn(SUBAGENT_MARK, child["messages"][0]["content"])
        self.assertNotIn("ask_user", child["tools"])
        self.assertNotIn("bash", child["tools"])
        tool_message = next(m for m in harness.agent.context.messages if m["role"] == "tool")
        self.assertIn("子 agent「找答案」做完了（1 次工具调用", tool_message["content"])
        self.assertIn("notes.txt 第 1 行：答案是 42", tool_message["content"])
        # 子 agent 读过的文件内容不进主对话
        self.assertFalse(any("答案是 42\n" in str(m.get("content")) for m in harness.agent.context.messages if m["role"] == "tool" and "子 agent" not in m["content"]))
        progress = harness.recorder.of_type(EventType.SUBAGENT_PROGRESS)
        self.assertEqual([(event.data["call_id"], event.data["name"]) for event in progress], [("p1", "read_file")])

    async def test_explore_subagents_in_one_step_run_in_parallel(self) -> None:
        provider = TwoLevelProvider(
            [
                LLMResponse(tool_calls=[spawn("p1", "查前端", "看前端"), spawn("p2", "查后端", "看后端")]),
                LLMResponse(text="都查完了"),
            ],
            {"查前端": [LLMResponse(text="前端没问题")], "查后端": [LLMResponse(text="后端没问题")]},
        )
        provider.gate.clear()
        harness = build_harness(self.directories, [], provider=provider)

        turn = asyncio.create_task(harness.agent.run_turn("前后端都查一下"))
        while provider.max_parallel < 2 and not turn.done():
            await asyncio.sleep(0.01)
        provider.gate.set()
        await turn

        self.assertEqual(provider.max_parallel, 2)
        results = [m["content"] for m in harness.agent.context.messages if m["role"] == "tool"]
        self.assertTrue(any("前端没问题" in text for text in results))
        self.assertTrue(any("后端没问题" in text for text in results))

    async def test_general_subagent_edits_go_through_labeled_approval(self) -> None:
        approver = RecordingApprover()
        provider = TwoLevelProvider(
            [LLMResponse(tool_calls=[spawn("p1", "写文件", "新建 out.txt 写 hi", kind="general")]), LLMResponse(text="好了")],
            {"写文件": [LLMResponse(tool_calls=[tool_call("c1", "write_file", '{"path": "out.txt", "content": "hi"}')]), LLMResponse(text="写好了 out.txt")]},
        )
        harness = build_harness(self.directories, [], provider=provider, mode=ApprovalMode.ASK, approver=approver)

        await harness.agent.run_turn("让子 agent 写个文件")

        self.assertEqual((self.directories.workspace / "out.txt").read_text(encoding="utf-8"), "hi")
        self.assertEqual(len(approver.requests), 1)
        self.assertTrue(approver.requests[0].summary.startswith("〔子 agent：写文件〕"))

    async def test_child_errors_come_back_as_a_result_not_a_crash(self) -> None:
        provider = TwoLevelProvider(
            [LLMResponse(tool_calls=[spawn("p1", "会出错", "随便做点事")]), LLMResponse(text="子 agent 出错了，我自己来")],
            {"会出错": [ValueError("模型挂了")]},
        )
        harness = build_harness(self.directories, [], provider=provider)

        final = await harness.agent.run_turn("试试")

        tool_message = next(m for m in harness.agent.context.messages if m["role"] == "tool")
        self.assertIn("出错停下了", tool_message["content"])
        self.assertIn("模型挂了", tool_message["content"])
        self.assertEqual(final, "子 agent 出错了，我自己来")

    async def test_stopping_the_main_task_stops_the_child(self) -> None:
        provider = TwoLevelProvider([LLMResponse(tool_calls=[spawn("p1", "很慢", "慢慢查")])], {"很慢": [LLMResponse(text="不会到这")]})
        provider.gate.clear()
        harness = build_harness(self.directories, [], provider=provider)

        turn = asyncio.create_task(harness.agent.run_turn("查"))
        while not provider.running_children:
            await asyncio.sleep(0.01)
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn

        tool_message = next(m for m in harness.agent.context.messages if m["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "p1")

    async def test_subagent_cannot_spawn_another(self) -> None:
        result = await SubagentTool().run(
            {"description": "套娃", "prompt": "再派一个"}, ToolContext(workspace=self.directories.workspace)
        )

        self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
