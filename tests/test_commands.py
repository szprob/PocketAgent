"""斜杠命令：解析、各条命令的输出和动作，web 和命令行共用一套。"""

import unittest
from pathlib import Path

from pocketagent.commands import (
    CommandContext,
    command_specs_for,
    handle_command_line,
    help_text,
    parse_command,
)
from pocketagent.config import AppConfig
from pocketagent.llm import LLMResponse
from pocketagent.permissions import ApprovalMode
from pocketagent.skills import Skill
from tests.helpers import TemporaryDirectories, build_harness


class ParseCommandTest(unittest.TestCase):
    def test_command_with_and_without_argument(self) -> None:
        parsed = parse_command("/mode ask")
        self.assertEqual((parsed.name, parsed.argument), ("mode", "ask"))
        self.assertEqual(parse_command("/help").argument, "")
        self.assertEqual(parse_command("/undo all").argument, "all")

    def test_paths_and_plain_text_are_not_commands(self) -> None:
        self.assertIsNone(parse_command("/usr/bin/python 是什么"))
        self.assertIsNone(parse_command("/tmp/x"))
        self.assertIsNone(parse_command("看看 /help 文档"))
        self.assertIsNone(parse_command(""))


class CommandHelpTest(unittest.TestCase):
    def test_web_hides_cli_only_commands(self) -> None:
        web_names = {spec.name for spec in command_specs_for("web")}
        cli_names = {spec.name for spec in command_specs_for("cli")}
        self.assertNotIn("exit", web_names)
        self.assertIn("exit", cli_names)
        self.assertIn("help", web_names)

    def test_help_lists_commands_and_explains_one(self) -> None:
        directories = TemporaryDirectories()
        try:
            harness = build_harness(directories, [LLMResponse(text="好")])
            context = CommandContext(settings=harness.settings, agent=harness.agent, surface="cli")
            listing = help_text(context)
            self.assertIn("/compact", listing)
            self.assertIn("/技能名", listing)
            detail = help_text(context, "mode")
            self.assertIn("用法：/mode", detail)
            self.assertIn("审批模式", detail)
        finally:
            directories.cleanup()


class CommandExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.harness = build_harness(self.directories, [LLMResponse(text="好")], mode=ApprovalMode.ASK)
        self.app_config = AppConfig.from_dict(
            {
                "models": [
                    {"id": "ds", "name": "DS", "model": "deepseek-v4.1-flash", "api_key": "sk-x"},
                    {"id": "qwen", "name": "Qwen", "model": "Qwen3.8-27B"},
                ],
                "default_model": "ds",
            }
        )
        self.context = CommandContext(
            settings=self.harness.settings,
            agent=self.harness.agent,
            models=self.app_config.models,
            surface="cli",
        )
        # 用上配置里的第一个模型，/model 列表里的「当前」标记才有依据
        self.harness.settings.use_profile(self.app_config.models[0])
        # /sessions、/resume 读的是磁盘上的会话，先把当前会话存了
        self.harness.agent.session_store.save(self.harness.agent.session)

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def run_command(self, text: str):
        result = await handle_command_line(text, self.context)
        self.assertIsNotNone(result, text)
        return result

    async def test_unknown_command_is_an_error_but_skill_names_pass_through(self) -> None:
        result = await self.run_command("/不存在的命令")
        self.assertEqual(result.kind, "error")
        self.assertIn("不认识的命令", result.text)
        # 技能名不在这套命令里：返回 None，调用方照常发给 agent
        self.harness.agent.skills = [Skill(name="my-skill", description="演示", path=Path("SKILL.md"), source="用户")]
        self.assertIsNone(await handle_command_line("/my-skill 做点事", self.context))

    async def test_mode_lists_and_switches(self) -> None:
        listing = await self.run_command("/mode")
        self.assertIn("ask", listing.text)
        switched = await self.run_command("/mode full_auto")
        self.assertEqual((switched.action, switched.argument), ("switch_mode", "full_auto"))
        bad = await self.run_command("/mode 没有这个")
        self.assertEqual(bad.kind, "error")

    async def test_model_lists_and_switches(self) -> None:
        listing = await self.run_command("/model")
        self.assertIn("DS", listing.text)
        self.assertIn("（当前）", listing.text)
        switched = await self.run_command("/model Qwen")
        self.assertEqual((switched.action, switched.argument), ("switch_model", "qwen"))
        bad = await self.run_command("/model nope")
        self.assertEqual(bad.kind, "error")

    async def test_status_shows_model_directory_and_session(self) -> None:
        result = await self.run_command("/status")
        self.assertIn("deepseek-v4.1-flash", result.text)
        self.assertIn(str(self.directories.workspace), result.text)
        self.assertIn(self.harness.agent.session.id, result.text)

    async def test_new_compact_and_exit_actions(self) -> None:
        self.assertEqual((await self.run_command("/new")).action, "new_session")
        self.assertEqual((await self.run_command("/compact")).action, "compact")
        self.assertEqual((await self.run_command("/quit")).action, "exit")

    async def test_exit_is_not_a_web_command(self) -> None:
        self.context.surface = "web"
        result = await self.run_command("/exit")
        self.assertEqual(result.kind, "warning")
        self.assertIsNone(result.action)

    async def test_sessions_lists_current_session(self) -> None:
        result = await self.run_command("/sessions")
        self.assertIn(self.harness.agent.session.id, result.text)
        self.assertIn("（当前）", result.text)

    async def test_resume_needs_an_existing_id(self) -> None:
        missing = await self.run_command("/resume no-such-id")
        self.assertEqual(missing.kind, "error")
        found = await self.run_command(f"/resume {self.harness.agent.session.id}")
        self.assertEqual((found.action, found.argument), ("open_session", self.harness.agent.session.id))

    async def test_usage_reports_session_tokens(self) -> None:
        await self.harness.agent.run_turn("hi")
        result = await self.run_command("/usage")
        self.assertIn("本会话", result.text)

    async def test_memory_lists_and_reads(self) -> None:
        empty = await self.run_command("/memory")
        self.assertIn("还没有项目记忆", empty.text)
        memory_dir = self.harness.settings.memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "约定.md").write_text(
            "---\nname: 约定\ndescription: 团队约定\n---\n测试用 stdlib unittest\n", encoding="utf-8"
        )
        listing = await self.run_command("/memory")
        self.assertIn("约定.md", listing.text)
        detail = await self.run_command("/memory 约定.md")
        self.assertIn("stdlib unittest", detail.text)

    async def test_undo_lists_changes_and_reverts_one_file(self) -> None:
        workspace = self.directories.workspace
        (workspace / "a.txt").write_text("v1\n", encoding="utf-8")
        await self.harness.agent.checkpoints.create("任务开始", "baseline")
        (workspace / "a.txt").write_text("v2\n", encoding="utf-8")
        (workspace / "b.txt").write_text("new\n", encoding="utf-8")

        listing = await self.run_command("/undo")
        self.assertIn("a.txt", listing.text)
        self.assertIn("b.txt", listing.text)

        undone = await self.run_command("/undo b.txt")
        self.assertIn("已撤销 b.txt", undone.text)
        self.assertFalse((workspace / "b.txt").exists())
        self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "v2\n")

    async def test_no_argument_commands_reject_extra_words(self) -> None:
        result = await self.run_command("/compact 这些")
        self.assertIn("不带参数", result.text)
        self.assertEqual(result.kind, "error")


class NeedsSessionTest(unittest.IsolatedAsyncioTestCase):
    """新任务页（没有会话）用不了要会话的命令，其余照常。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.harness = build_harness(self.directories, [LLMResponse(text="好")])
        self.context = CommandContext(
            settings=self.harness.settings, agent=None, models=[], surface="web"
        )

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_session_only_commands_say_to_open_a_task_first(self) -> None:
        for text in ("/compact", "/usage", "/undo"):
            result = await handle_command_line(text, self.context)
            self.assertEqual(result.kind, "warning", text)
            self.assertIn("先开个任务", result.text)

    async def test_help_and_status_still_work(self) -> None:
        result = await handle_command_line("/help", self.context)
        self.assertIn("/help", result.text)
        status = await handle_command_line("/status", self.context)
        self.assertNotIn("会话", status.text)
        self.assertEqual(status.kind, "info")

    async def test_new_on_the_new_task_page_explains_itself(self) -> None:
        result = await handle_command_line("/new", self.context)
        self.assertIn("新任务页", result.text)
        self.assertIsNone(result.action)
