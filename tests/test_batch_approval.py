"""一批工具调用的审批：要问人的合成一张卡（每项单独选），AI 审批并发，审完按顺序执行。"""

import asyncio
import json
import unittest

from pocketagent.core.events import EventType
from pocketagent.core.nodes import split_into_stages
from pocketagent.llm import LLMResponse
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalMode
from tests.helpers import TemporaryDirectories, build_harness, tool_call

ALLOW = ApprovalAnswer.ALLOW_ONCE
DENY = ApprovalAnswer.DENY
REVIEW_ALLOW = '{"allow": true, "reason": "项目内的改动"}'


def write(call_id: str, path: str, content: str):
    return tool_call(call_id, "write_file", json.dumps({"path": path, "content": content}))


def edit(call_id: str, path: str, old: str, new: str):
    return tool_call(call_id, "edit_file", json.dumps({"path": path, "old_string": old, "new_string": new}))


class MergingApprover:
    """支持合并的通道：每次问记一轮（一轮里几项），按剧本逐轮回答。"""

    def __init__(self, rounds: list[list[ApprovalAnswer]]) -> None:
        self.answers = list(rounds)
        self.rounds: list[list] = []

    async def request_approval(self, request):
        self.rounds.append([request])
        return self.answers.pop(0)[0]

    async def request_approvals(self, requests):
        self.rounds.append(list(requests))
        return self.answers.pop(0)

    async def ask_questions(self, questions):
        return None


class OneByOneApprover:
    """不支持合并的通道（比如命令行）：只会一项一项问。"""

    def __init__(self, answers: list[ApprovalAnswer]) -> None:
        self.answers = list(answers)
        self.requests: list = []

    async def request_approval(self, request):
        self.requests.append(request)
        return self.answers.pop(0)

    async def ask_questions(self, questions):
        return None


class ReviewCountingProvider:
    """主对话按剧本走；AI 审批的调用（只有一条 user 消息）记下同时在审的有几个。"""

    def __init__(self, main_script: list[LLMResponse]) -> None:
        self.main_script = list(main_script)
        self.reviews_in_flight = 0
        self.max_reviews_in_flight = 0

    async def complete(self, messages, tools, listener):
        if messages[0]["role"] == "user":
            self.reviews_in_flight += 1
            self.max_reviews_in_flight = max(self.max_reviews_in_flight, self.reviews_in_flight)
            await asyncio.sleep(0.05)
            self.reviews_in_flight -= 1
            return LLMResponse(text=REVIEW_ALLOW)
        return self.main_script.pop(0)


class BatchApprovalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace

    def tearDown(self) -> None:
        self.directories.cleanup()

    def tool_results(self, harness) -> dict[str, str]:
        return {m["tool_call_id"]: m["content"] for m in harness.agent.context.messages if m["role"] == "tool"}

    async def test_three_writes_become_one_card_and_each_can_be_denied(self) -> None:
        approver = MergingApprover([[ALLOW, DENY, ALLOW]])
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[write("c1", "a.txt", "A"), write("c2", "b.txt", "B"), write("c3", "c.txt", "C")])],
            mode=ApprovalMode.ASK,
            approver=approver,
        )

        await harness.agent.run_turn("写三个文件")

        self.assertEqual([len(each) for each in approver.rounds], [3])
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "A")
        self.assertFalse((self.workspace / "b.txt").exists())
        self.assertEqual((self.workspace / "c.txt").read_text(encoding="utf-8"), "C")
        self.assertIn("用户拒绝了这次调用", self.tool_results(harness)["c2"])
        self.assertEqual(harness.recorder.of_type(EventType.TURN_END)[0].data["stop_reason"], "denied")

    async def test_channels_without_merging_are_asked_one_by_one_before_anything_runs(self) -> None:
        approver = OneByOneApprover([ALLOW, ALLOW])
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[write("c1", "a.txt", "A"), write("c2", "b.txt", "B")]), LLMResponse(text="好了")],
            mode=ApprovalMode.ASK,
            approver=approver,
        )

        await harness.agent.run_turn("写两个文件")

        self.assertEqual([request.tool_name for request in approver.requests], ["write_file", "write_file"])
        self.assertTrue((self.workspace / "a.txt").exists())
        self.assertTrue((self.workspace / "b.txt").exists())

    async def test_second_edit_of_the_same_file_is_reviewed_after_the_first_runs(self) -> None:
        (self.workspace / "a.txt").write_text("x\n", encoding="utf-8")
        approver = MergingApprover([[ALLOW], [ALLOW]])
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[edit("c1", "a.txt", "x", "y"), edit("c2", "a.txt", "y", "z")]), LLMResponse(text="好了")],
            mode=ApprovalMode.ASK,
            approver=approver,
        )

        await harness.agent.run_turn("改两次")

        self.assertEqual([len(each) for each in approver.rounds], [1, 1])
        second_preview = approver.rounds[1][0].preview
        self.assertIn("-y", second_preview)
        self.assertIn("+z", second_preview)
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "z\n")

    async def test_a_denied_stage_skips_the_later_stages_without_asking(self) -> None:
        (self.workspace / "a.txt").write_text("x\n", encoding="utf-8")
        approver = MergingApprover([[DENY]])
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[edit("c1", "a.txt", "x", "y"), edit("c2", "a.txt", "y", "z")])],
            mode=ApprovalMode.ASK,
            approver=approver,
        )

        await harness.agent.run_turn("改两次")

        self.assertEqual(len(approver.rounds), 1)
        self.assertIn("前面的调用被用户拒绝了", self.tool_results(harness)["c2"])
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "x\n")

    async def test_ai_reviews_of_one_stage_run_concurrently(self) -> None:
        provider = ReviewCountingProvider(
            [LLMResponse(tool_calls=[write("c1", "a.txt", "A"), write("c2", "b.txt", "B")]), LLMResponse(text="好了")]
        )
        approver = MergingApprover([])
        harness = build_harness(self.directories, [], mode=ApprovalMode.AI_AUTO, approver=approver, provider=provider)

        await harness.agent.run_turn("写两个文件")

        self.assertEqual(provider.max_reviews_in_flight, 2)
        self.assertEqual(approver.rounds, [])
        self.assertTrue((self.workspace / "a.txt").exists())
        self.assertTrue((self.workspace / "b.txt").exists())

    def test_calls_after_a_command_are_previewed_in_a_new_stage(self) -> None:
        harness = build_harness(self.directories, [])
        calls = [
            write("c1", "a.txt", "A"),
            write("c2", "b.txt", "B"),
            tool_call("c3", "bash", '{"command": "touch c.txt"}'),
            write("c4", "c.txt", "C"),
            write("c5", "a.txt", "AA"),
        ]

        stages = split_into_stages(calls, harness.agent.context)

        self.assertEqual([[call.id for call in stage] for stage in stages], [["c1", "c2", "c3"], ["c4", "c5"]])


if __name__ == "__main__":
    unittest.main()
