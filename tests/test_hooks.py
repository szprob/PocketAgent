"""钩子：配置校验、工具名匹配、九个时机的行为。"""

import json
import unittest
from pathlib import Path

from pocketagent.config import AppConfig, ConfigError, Settings
from pocketagent.core.events import EventBus, EventType
from pocketagent.interaction.modes import PlanApprovalRequest, PlanDecision, PlanSwitchRequest
from pocketagent.llm import LLMResponse
from pocketagent.safety.hooks import HookDefinition, HookRunner, NotifyingApprover
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalMode, DenyAllApprover
from pocketagent.safety.processes import bash_executable
from tests.helpers import TemporaryDirectories, build_harness, tool_call
from tests.test_subagents import TwoLevelProvider, spawn

HAS_BASH = bash_executable() is not None


class RecordingApprover:
    """记下每次审批请求；一律允许一次。"""

    def __init__(self) -> None:
        self.requests = []

    async def request_approval(self, request):
        self.requests.append(request)
        return ApprovalAnswer.ALLOW_ONCE

    async def ask_questions(self, questions):
        return None


def write_script(directory: Path, name: str, body: str) -> str:
    """写一个可执行的钩子脚本，返回可以直接填进配置的命令。"""
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return f'"{path}"'


class HookDefinitionTest(unittest.TestCase):
    def test_defaults_and_round_trip(self) -> None:
        hook = HookDefinition.from_dict({"event": "pre_tool_use", "command": "echo hi"})

        self.assertEqual(hook.matcher, "")
        self.assertEqual(hook.timeout_seconds, 30)
        self.assertTrue(hook.enabled)
        self.assertEqual(HookDefinition.from_dict(hook.to_dict()), hook)

    def test_rejects_unknown_event(self) -> None:
        with self.assertRaises(ValueError) as raised:
            HookDefinition.from_dict({"event": "before_everything", "command": "echo hi"})

        self.assertIn("user_prompt_submit", str(raised.exception))

    def test_rejects_empty_command(self) -> None:
        with self.assertRaises(ValueError):
            HookDefinition.from_dict({"event": "turn_end", "command": "  "})

    def test_rejects_bad_timeout(self) -> None:
        for timeout in ("abc", 0, 10000):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    HookDefinition.from_dict({"event": "turn_end", "command": "true", "timeout_seconds": timeout})

    def test_matcher_accepts_wildcards_and_lists(self) -> None:
        hook = HookDefinition(event="pre_tool_use", command="true", matcher="edit_*|write_file")

        self.assertTrue(hook.matches("write_file"))
        self.assertTrue(hook.matches("edit_file"))
        self.assertFalse(hook.matches("bash"))
        self.assertTrue(HookDefinition(event="pre_tool_use", command="true").matches("bash"))
        self.assertTrue(HookDefinition(event="pre_tool_use", command="true", matcher="mcp__*").matches("mcp__docs__read"))

    def test_matcher_does_not_apply_to_prompt_and_turn_events(self) -> None:
        hook = HookDefinition(event="user_prompt_submit", command="true", matcher="bash")

        self.assertTrue(hook.matches(""))

    def test_config_validation_turns_into_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"hooks": [{"event": "nope", "command": "true"}]})
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"hooks": "not-a-list"})

    def test_config_keeps_hooks_in_both_dicts(self) -> None:
        payload = {"hooks": [{"event": "post_tool_use", "matcher": "bash", "command": "echo done"}]}
        config = AppConfig.from_dict(payload)

        self.assertEqual(config.to_dict()["hooks"][0]["command"], "echo done")
        self.assertEqual(config.public_dict()["hooks"][0]["event"], "post_tool_use")


@unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
class HookRunnerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.runner = self.build_runner([], session_id="s1")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def build_runner(self, hooks: list[HookDefinition], session_id: str = "s1") -> HookRunner:
        settings = Settings(workspace=self.workspace, home_dir=self.directories.home_dir, hooks=hooks)
        return HookRunner(settings, session_id, EventBus())

    async def test_exit_code_two_blocks_with_stderr_as_reason(self) -> None:
        command = write_script(self.workspace, "block.sh", 'echo "不许删这个目录" >&2\nexit 2\n')
        runner = self.build_runner([HookDefinition(event="pre_tool_use", command=command)])

        outcome = await runner.run("pre_tool_use", {"tool_name": "bash"}, tool_name="bash")

        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.reason, "不许删这个目录")
        self.assertEqual(outcome.problems, [])

    async def test_json_decision_blocks_and_collects_context(self) -> None:
        command = write_script(
            self.workspace,
            "json.sh",
            "cat > /dev/null\n"
            "printf '%s' '{\"decision\": \"block\", \"reason\": \"先跑测试\", \"additional_context\": \"改了 3 个文件\"}'\n",
        )
        runner = self.build_runner([HookDefinition(event="post_tool_use", command=command)])

        outcome = await runner.run("post_tool_use", {"tool_name": "bash"}, tool_name="bash")

        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.reason, "先跑测试")
        self.assertEqual(outcome.additional_context, "改了 3 个文件")

    async def test_context_from_several_hooks_is_joined(self) -> None:
        first = write_script(self.workspace, "a.sh", "cat > /dev/null\nprintf '%s' '{\"additional_context\": \"第一条\"}'\n")
        second = write_script(self.workspace, "b.sh", "cat > /dev/null\nprintf '%s' '{\"additional_context\": \"第二条\"}'\n")
        runner = self.build_runner(
            [HookDefinition(event="turn_end", command=first), HookDefinition(event="turn_end", command=second)]
        )

        outcome = await runner.run("turn_end", {})

        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.additional_context, "第一条\n第二条")

    async def test_failing_hook_is_reported_but_does_not_block(self) -> None:
        command = write_script(self.workspace, "broken.sh", 'echo "配置读不到" >&2\nexit 1\n')
        runner = self.build_runner([HookDefinition(event="pre_tool_use", command=command)])

        outcome = await runner.run("pre_tool_use", {"tool_name": "bash"}, tool_name="bash")

        self.assertFalse(outcome.blocked)
        self.assertEqual(len(outcome.problems), 1)
        self.assertIn("退出码 1", outcome.problems[0])
        self.assertIn("配置读不到", outcome.problems[0])

    async def test_timeout_kills_the_hook(self) -> None:
        command = write_script(self.workspace, "slow.sh", "sleep 5\n")
        runner = self.build_runner([HookDefinition(event="pre_tool_use", command=command, timeout_seconds=1)])

        outcome = await runner.run("pre_tool_use", {"tool_name": "bash"}, tool_name="bash")

        self.assertFalse(outcome.blocked)
        self.assertIn("超过 1 秒", outcome.problems[0])

    async def test_matcher_skips_other_tools(self) -> None:
        command = write_script(self.workspace, "block.sh", "exit 2\n")
        runner = self.build_runner([HookDefinition(event="pre_tool_use", command=command, matcher="write_file")])

        outcome = await runner.run("pre_tool_use", {"tool_name": "bash"}, tool_name="bash")

        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.problems, [])

    async def test_disabled_hook_does_not_run(self) -> None:
        command = write_script(self.workspace, "block.sh", "exit 2\n")
        runner = self.build_runner(
            [HookDefinition(event="pre_tool_use", command=command, enabled=False)]
        )

        outcome = await runner.run("pre_tool_use", {"tool_name": "bash"}, tool_name="bash")

        self.assertFalse(outcome.blocked)

    async def test_payload_environment_and_working_directory(self) -> None:
        payload_path = self.workspace / "payload.json"
        environment_path = self.workspace / "env.txt"
        directory_path = self.workspace / "cwd.txt"
        command = write_script(
            self.workspace,
            "record.sh",
            f'cat > "{payload_path}"\n'
            f'printf "%s\\n%s\\n%s\\n" "$POCKETAGENT_EVENT" "$POCKETAGENT_TOOL_NAME" "$POCKETAGENT_SESSION_ID" '
            f'> "{environment_path}"\n'
            f'pwd > "{directory_path}"\n',
        )
        runner = self.build_runner([HookDefinition(event="pre_tool_use", command=command)], session_id="session-9")

        await runner.run(
            "pre_tool_use",
            {"tool_name": "bash", "tool_input": {"command": "ls"}, "tool_call_id": "call_1"},
            tool_name="bash",
        )

        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["event"], "pre_tool_use")
        self.assertEqual(payload["session_id"], "session-9")
        self.assertEqual(payload["tool_input"], {"command": "ls"})
        self.assertEqual(payload["workspace"], str(self.workspace))
        self.assertEqual(
            environment_path.read_text(encoding="utf-8").splitlines(),
            ["pre_tool_use", "bash", "session-9"],
        )
        self.assertEqual(Path(directory_path.read_text(encoding="utf-8").strip()).resolve(), self.workspace.resolve())


@unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
class HookAgentTest(unittest.IsolatedAsyncioTestCase):
    """钩子接进主循环之后：拦住、补充上下文、收尾。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_pre_tool_use_block_stops_the_call_but_not_the_turn(self) -> None:
        command = write_script(self.workspace, "block.sh", 'echo "这个目录不许动" >&2\nexit 2\n')
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[tool_call("call_1", "bash", '{"command": "touch blocked.txt"}')]), LLMResponse(text="好的，我不动它")],
            hooks=[HookDefinition(event="pre_tool_use", command=command)],
        )

        final_text = await harness.agent.run_turn("删掉那个目录")

        self.assertEqual(final_text, "好的，我不动它")
        self.assertFalse((self.workspace / "blocked.txt").exists())
        tool_message = harness.agent.context.messages[3]
        self.assertEqual(tool_message["role"], "tool")
        self.assertTrue(tool_message["_is_error"])
        self.assertIn("钩子阻止了这次调用", tool_message["content"])
        self.assertIn("这个目录不许动", tool_message["content"])
        hook_events = harness.recorder.of_type(EventType.HOOK)
        self.assertEqual(len(hook_events), 1)
        self.assertTrue(hook_events[0].data["blocked"])

    async def test_post_tool_use_context_reaches_the_model(self) -> None:
        (self.workspace / "hello.txt").write_text("hi\n", encoding="utf-8")
        command = write_script(
            self.workspace,
            "note.sh",
            "cat > /dev/null\nprintf '%s' '{\"additional_context\": \"这个文件还有 3 个 lint 错误\"}'\n",
        )
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[tool_call("call_1", "read_file", '{"path": "hello.txt"}')]), LLMResponse(text="看到了")],
            hooks=[HookDefinition(event="post_tool_use", command=command)],
        )

        await harness.agent.run_turn("看看这个文件")

        tool_message = harness.agent.context.messages[3]
        self.assertIn("这个文件还有 3 个 lint 错误", tool_message["content"])
        # 下一次调用模型时工具结果里已经带着钩子补充的话
        self.assertIn("lint 错误", json.dumps(harness.provider.received_messages[1], ensure_ascii=False))

    async def test_user_prompt_submit_adds_context(self) -> None:
        command = write_script(
            self.workspace,
            "context.sh",
            "cat > /dev/null\nprintf '%s' '{\"additional_context\": \"当前分支是 feature/hooks\"}'\n",
        )
        harness = build_harness(
            self.directories,
            [LLMResponse(text="知道了")],
            hooks=[HookDefinition(event="user_prompt_submit", command=command)],
        )

        await harness.agent.run_turn("接着做")

        messages = harness.agent.context.messages
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "接着做")
        self.assertEqual(messages[2]["_kind"], "notice")
        self.assertIn("当前分支是 feature/hooks", messages[2]["content"])
        self.assertIn("feature/hooks", json.dumps(harness.provider.received_messages[0], ensure_ascii=False))

    async def test_user_prompt_submit_can_block_the_turn(self) -> None:
        command = write_script(self.workspace, "block.sh", 'echo "这句话里带密钥" >&2\nexit 2\n')
        harness = build_harness(
            self.directories,
            [LLMResponse(text="不该调用模型")],
            hooks=[HookDefinition(event="user_prompt_submit", command=command)],
        )

        final_text = await harness.agent.run_turn("sk-1234567890")

        self.assertEqual(final_text, "")
        self.assertEqual(harness.provider.received_messages, [])
        self.assertEqual(harness.agent.context.turn.stop_reason, "hook_blocked")
        notices = [message for message in harness.agent.context.messages if message.get("_kind") == "notice"]
        self.assertIn("这句话里带密钥", notices[0]["content"])
        self.assertEqual(harness.recorder.of_type(EventType.TURN_END)[0].data["stop_reason"], "hook_blocked")

    async def test_turn_end_receives_stop_reason(self) -> None:
        record_path = self.workspace / "turn-end.txt"
        command = write_script(
            self.workspace,
            "record.sh",
            f'{{ printf "%s\\n" "$POCKETAGENT_EVENT"; cat; }} > "{record_path}"\n',
        )
        harness = build_harness(
            self.directories,
            [LLMResponse(text="做完了")],
            hooks=[HookDefinition(event="turn_end", command=command)],
        )

        await harness.agent.run_turn("随便做点什么")

        recorded = record_path.read_text(encoding="utf-8")
        self.assertTrue(recorded.startswith("turn_end\n"))
        payload = json.loads(recorded.split("\n", 1)[1])
        self.assertEqual(payload["stop_reason"], "done")
        self.assertEqual(payload["final_text"], "做完了")

    async def test_denied_call_does_not_run_pre_hook(self) -> None:
        command = write_script(self.workspace, "mark.sh", f'echo ran >> "{self.workspace / "mark.txt"}"\n')
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[tool_call("call_1", "bash", '{"command": "echo hi"}')])],
            mode=ApprovalMode.ASK,
            approver=DenyAllApprover(),
            hooks=[HookDefinition(event="pre_tool_use", command=command)],
        )

        await harness.agent.run_turn("跑一下")

        self.assertFalse((self.workspace / "mark.txt").exists())
        self.assertEqual(harness.recorder.of_type(EventType.HOOK), [])

    async def test_session_start_runs_once_and_adds_context(self) -> None:
        command = write_script(
            self.workspace,
            "start.sh",
            "cat > /dev/null\nprintf '%s' '{\"additional_context\": \"今天是周五\"}'\n",
        )
        harness = build_harness(
            self.directories,
            [LLMResponse(text="第一轮"), LLMResponse(text="第二轮")],
            hooks=[HookDefinition(event="session_start", command=command)],
        )

        await harness.agent.run_turn("第一句")
        await harness.agent.run_turn("第二句")

        # 只有第一轮跑，续跑 / 第二轮都不算新任务
        self.assertEqual(len(harness.recorder.of_type(EventType.HOOK)), 1)
        messages = harness.agent.context.messages
        self.assertEqual(messages[2]["_kind"], "notice")
        self.assertIn("今天是周五", messages[2]["content"])
        self.assertIn("今天是周五", json.dumps(harness.provider.received_messages[0], ensure_ascii=False))

    async def test_session_end_runs_on_close_only_once(self) -> None:
        record_path = self.workspace / "session-end.json"
        command = write_script(self.workspace, "end.sh", f'cat > "{record_path}"\n')
        harness = build_harness(
            self.directories,
            [LLMResponse(text="做完了")],
            hooks=[HookDefinition(event="session_end", command=command)],
        )

        await harness.agent.run_turn("做点事")
        await harness.agent.close()
        await harness.agent.close()

        payload = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["turns"], 1)
        self.assertEqual(len(harness.recorder.of_type(EventType.HOOK)), 1)

    async def test_notification_fires_before_approval(self) -> None:
        record_path = self.workspace / "notification.json"
        command = write_script(self.workspace, "notify.sh", f'cat > "{record_path}"\n')
        approver = RecordingApprover()
        harness = build_harness(
            self.directories,
            [LLMResponse(tool_calls=[tool_call("call_1", "bash", '{"command": "echo hi"}')]), LLMResponse(text="好了")],
            mode=ApprovalMode.ASK,
            approver=approver,
            hooks=[HookDefinition(event="notification", command=command)],
        )

        await harness.agent.run_turn("跑一下")

        payload = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "approval")
        self.assertEqual(payload["tools"], ["bash"])
        self.assertEqual(len(approver.requests), 1)

    async def test_pre_compact_runs_before_summary(self) -> None:
        record_path = self.workspace / "pre-compact.json"
        command = write_script(self.workspace, "compact.sh", f'cat > "{record_path}"\n')
        harness = build_harness(
            self.directories,
            [LLMResponse(text="第一轮"), LLMResponse(text="这是摘要")],
            hooks=[HookDefinition(event="pre_compact", command=command)],
        )

        await harness.agent.run_turn("说句话")
        await harness.agent.compact()

        payload = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["reason"], "manual")
        self.assertGreater(payload["tokens_before"], 0)
        self.assertEqual(len(harness.recorder.of_type(EventType.HOOK)), 1)

    async def test_subagent_stop_can_add_to_the_result(self) -> None:
        command = write_script(
            self.workspace,
            "subagent.sh",
            "cat > /dev/null\nprintf '%s' '{\"additional_context\": \"记得跑测试\"}'\n",
        )
        provider = TwoLevelProvider(
            [LLMResponse(tool_calls=[spawn("call_1", "调研", "看看")]), LLMResponse(text="收到")],
            {"调研": [LLMResponse(text="子 agent 的结论")]},
        )
        harness = build_harness(
            self.directories,
            [],
            provider=provider,
            hooks=[HookDefinition(event="subagent_stop", command=command)],
        )

        await harness.agent.run_turn("派个活")

        tool_messages = [message for message in harness.agent.context.messages if message.get("role") == "tool"]
        self.assertIn("子 agent 的结论", tool_messages[0]["content"])
        self.assertIn("记得跑测试", tool_messages[0]["content"])


class NotifyingApproverTest(unittest.IsolatedAsyncioTestCase):
    """审批通道包一层之后，它上面的其他能力不能被藏起来（计划模式就靠 request_plan_approval）。"""

    def build(self, inner):
        settings = Settings(workspace=Path("."), home_dir=Path("."))
        return NotifyingApprover(inner, HookRunner(settings, "s1", EventBus()))

    async def test_plan_requests_are_forwarded(self) -> None:
        class PlanApprover(RecordingApprover):
            def __init__(self) -> None:
                super().__init__()
                self.plans = []

            async def request_plan_approval(self, request):
                self.plans.append(request)
                return PlanDecision(approve=True)

        inner = PlanApprover()
        wrapper = self.build(inner)

        decision = await wrapper.request_plan_approval(PlanApprovalRequest(plan="先这样"))
        # 内层没实现「要不要切计划模式」时按没人能回答处理
        switched = await wrapper.request_plan_switch(PlanSwitchRequest(reason="值得先规划"))

        self.assertTrue(decision.approve)
        self.assertEqual(len(inner.plans), 1)
        self.assertFalse(switched)

    async def test_other_approver_attributes_are_forwarded(self) -> None:
        inner = RecordingApprover()
        inner.label = "extra"
        wrapper = self.build(inner)

        self.assertEqual(wrapper.label, "extra")
