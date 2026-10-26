"""技能（Agent Skills 格式）和项目记忆。"""

import unittest
from pathlib import Path
from unittest import mock

from pocketagent.compaction import PRESERVED_SKILLS_HEADER
from pocketagent.frontmatter import split_frontmatter
from pocketagent.llm import LLMResponse
from pocketagent.memory import delete_memory, list_memories, memory_prompt_section, project_memory_dir
from pocketagent.permissions import ApprovalAnswer, ApprovalMode, Decision, DenyAllApprover, PermissionGate
from pocketagent.skills import (
    SkillDirectory,
    discover_skills,
    find_skill_command,
    skill_directories,
    skills_prompt_section,
)
from pocketagent.tools import ToolContext
from pocketagent.tools.files import ReadFileTool, WriteFileTool
from pocketagent.tools.skill import ActivateSkillTool
from pocketagent.web.history import history_items
from tests.helpers import RoutingProvider, TemporaryDirectories, build_harness, tool_call

SKILL_TEXT = """---
name: "deploy-thing"
description: >
  部署服务用。当用户说
  '上线'、"发布" 时触发。
argument-hint: <service>
---

# 部署

1. 跑 scripts/deploy.sh
"""


class RecordingApprover:
    def __init__(self) -> None:
        self.requests = []

    async def request_approval(self, request) -> ApprovalAnswer:
        self.requests.append(request)
        return ApprovalAnswer.ALLOW_ONCE


class SkillTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.user_dir = self.directories.home_dir / "skills"
        # 不读本机的 ~/.agents/skills、~/.claude/skills
        patcher = mock.patch("pocketagent.skills.USER_SKILL_DIRS", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.directories.cleanup()

    def write_skill(self, root: Path, folder: str, text: str) -> Path:
        skill_file = root / folder / "SKILL.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(text, encoding="utf-8")
        return skill_file

    def user_skills(self):
        return discover_skills([SkillDirectory(self.user_dir, "用户")]).skills


class SkillFormatTest(SkillTestBase):
    def test_frontmatter_variants(self) -> None:
        metadata, body = split_frontmatter(SKILL_TEXT)
        single, _ = split_frontmatter("---\nname: 'it''s'\ndescription: \"say \\\"hi\\\"\"\n---\nbody")
        colon, _ = split_frontmatter("---\nname: pdf\ndescription: Use this skill when: the user asks about PDFs\n---\n")

        self.assertEqual(metadata["name"], "deploy-thing")
        self.assertEqual(metadata["description"], "部署服务用。当用户说 '上线'、\"发布\" 时触发。")
        self.assertTrue(body.lstrip().startswith("# 部署"))
        self.assertEqual(single, {"name": "it's", "description": 'say "hi"'})
        self.assertEqual(colon["description"], "Use this skill when: the user asks about PDFs")
        self.assertEqual(split_frontmatter("no frontmatter")[0], {})

    def test_spec_violations_are_warnings_but_missing_description_skips(self) -> None:
        self.write_skill(self.user_dir, "deploy", SKILL_TEXT)
        self.write_skill(self.user_dir, "Bad_Name", "---\nname: Bad_Name\ndescription: 能用\n---\n")
        self.write_skill(self.user_dir, "empty", "---\nname: empty\n---\n正文")

        catalog = discover_skills([SkillDirectory(self.user_dir, "用户")])
        by_name = {skill.name: skill for skill in catalog.skills}

        self.assertEqual(sorted(by_name), ["Bad_Name", "deploy-thing"])
        self.assertTrue(any("和目录名" in warning for warning in by_name["deploy-thing"].warnings))
        self.assertTrue(any("小写字母" in warning for warning in by_name["Bad_Name"].warnings))
        self.assertTrue(any("没有 description" in problem for problem in catalog.problems))

    def test_standard_directories_and_project_overrides_user(self) -> None:
        agents_dir = self.workspace / ".agents" / "skills"
        self.write_skill(agents_dir, "deploy-thing", SKILL_TEXT.replace("部署服务用", "项目版"))
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        self.write_skill(self.user_dir, "group/inner", "---\nname: inner\ndescription: 嵌套的\n---\n")
        directories = skill_directories(self.workspace, self.directories.home_dir, [])

        catalog = discover_skills(directories)

        self.assertIn(agents_dir, [directory.path for directory in directories])
        self.assertEqual([skill.name for skill in catalog.skills], ["deploy-thing"])
        self.assertTrue(catalog.skills[0].description.startswith("项目版"))
        self.assertEqual(catalog.skills[0].source, "项目")
        self.assertTrue(any("重名" in problem for problem in catalog.problems))

    def test_symlinked_directories_are_scanned_once(self) -> None:
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        alias = self.directories.workspace.parent / "alias-skills"
        alias.symlink_to(self.user_dir)

        directories = skill_directories(None, self.directories.home_dir, [str(alias)])

        self.assertEqual([directory.path for directory in directories], [self.user_dir])

    def test_catalog_is_available_skills_xml_with_location(self) -> None:
        skill_file = self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        self.write_skill(self.user_dir, "long", "---\nname: long\ndescription: " + "长<" * 800 + "\n---\n")

        section = skills_prompt_section(self.user_skills())

        self.assertIn("<available_skills>", section)
        self.assertIn("<name>deploy-thing</name>", section)
        self.assertIn(f"<location>{skill_file}</location>", section)
        self.assertIn("长&lt;", section)
        self.assertNotIn("长<", section)
        self.assertIn("…</description>", section)

    def test_skill_command_parsing(self) -> None:
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        skills = self.user_skills()

        self.assertEqual(find_skill_command("/deploy-thing 上线 api 服务", skills)[1], "上线 api 服务")
        self.assertIsNotNone(find_skill_command("/deploy-thing", skills))
        self.assertIsNone(find_skill_command("/unknown 做点事", skills))
        self.assertIsNone(find_skill_command("帮我 /deploy-thing", skills))


class SkillActivationTest(SkillTestBase):
    async def test_activate_skill_wraps_body_and_lists_resources(self) -> None:
        skill_file = self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        (skill_file.parent / "scripts").mkdir()
        (skill_file.parent / "scripts" / "deploy.sh").write_text("echo deploy", encoding="utf-8")
        tool = ActivateSkillTool(self.user_skills())
        context = ToolContext(workspace=self.workspace)

        loaded = await tool.run({"name": "deploy-thing"}, context)
        missing = await tool.run({"name": "nope"}, context)

        self.assertTrue(loaded.content.startswith('<skill_content name="deploy-thing">'))
        self.assertTrue(loaded.content.endswith("</skill_content>"))
        self.assertIn("# 部署", loaded.content)
        self.assertIn("<file>scripts/deploy.sh</file>", loaded.content)
        self.assertIn(f"技能目录：{skill_file.parent}", loaded.content)
        self.assertNotIn("argument-hint", loaded.content)
        self.assertEqual(tool.parameters["properties"]["name"]["enum"], ["deploy-thing"])
        self.assertTrue(missing.is_error)

    async def test_second_activation_is_deduplicated(self) -> None:
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        tool = ActivateSkillTool(self.user_skills())
        messages = []
        context = ToolContext(workspace=self.workspace, history=lambda: messages)

        first = await tool.run({"name": "deploy-thing"}, context)
        messages.append({"role": "tool", "tool_call_id": "c1", "content": first.content})
        second = await tool.run({"name": "deploy-thing"}, context)

        self.assertIn("已经在上面的对话里了", second.content)

    async def test_agent_lists_skills_and_reads_their_files_without_asking(self) -> None:
        skill_file = self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        approver = RecordingApprover()
        responses = [
            LLMResponse(
                tool_calls=[
                    tool_call("c1", "activate_skill", '{"name": "deploy-thing"}'),
                    tool_call("c2", "read_file", f'{{"path": "{skill_file}"}}'),
                ]
            ),
            LLMResponse(text="好了"),
        ]
        harness = build_harness(
            self.directories, responses, mode=ApprovalMode.ASK, approver=approver, skills_enabled=True
        )

        await harness.agent.run_turn("上线")

        system_prompt = harness.agent.context.messages[0]["content"]
        self.assertIn("<name>deploy-thing</name>", system_prompt)
        self.assertIn("activate_skill", harness.agent.context.tools.names())
        self.assertEqual(approver.requests, [])

    async def test_slash_command_injects_the_skill_but_shows_only_the_user_text(self) -> None:
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        harness = build_harness(self.directories, [LLMResponse(text="开始部署")], skills_enabled=True)

        await harness.agent.run_turn("/deploy-thing 上线 api")

        user_message = harness.agent.context.messages[1]
        sent = harness.provider.received_messages[0][1]
        items = history_items(harness.agent.context.messages, harness.agent.context.tools)
        self.assertEqual(items[0]["text"], "/deploy-thing 上线 api")
        self.assertIn('<skill_content name="deploy-thing">', sent["content"][1]["text"])
        self.assertEqual(user_message["content"][0]["text"], "/deploy-thing 上线 api")

    async def test_compaction_keeps_loaded_skills_verbatim(self) -> None:
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        provider = RoutingProvider(
            [
                LLMResponse(tool_calls=[tool_call("c1", "activate_skill", '{"name": "deploy-thing"}')]),
                LLMResponse(text="照着技能做完了"),
            ],
            summary_text="做了部署",
        )
        harness = build_harness(self.directories, [], provider=provider, skills_enabled=True)
        await harness.agent.run_turn("上线")

        await harness.agent.compact()

        summary = harness.agent.context.messages[1]["content"]
        items = history_items(harness.agent.context.messages, harness.agent.context.tools)
        self.assertIn("做了部署", summary)
        self.assertIn(PRESERVED_SKILLS_HEADER.strip(), summary)
        self.assertIn('<skill_content name="deploy-thing">', summary)
        self.assertIn("# 部署", summary)
        self.assertEqual(items[0]["kind"], "summary")
        self.assertNotIn("skill_content", items[0]["text"])

    async def test_disabled_skills_are_hidden(self) -> None:
        self.write_skill(self.user_dir, "deploy-thing", SKILL_TEXT)
        harness = build_harness(self.directories, [], skills_enabled=True, disabled_skills=["deploy-thing"])

        self.assertNotIn("deploy-thing", harness.agent.context.messages[0]["content"])
        self.assertNotIn("activate_skill", harness.agent.context.tools.names())


MEMORY_FILE = """---
name: prefers-uv
description: 用户装包一律用 uv，不用 pip
type: feedback
---

装 Python 包用 uv pip install。
**Why:** 环境是 uv 管的。
"""


class ProjectMemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.memory_dir = project_memory_dir(self.directories.home_dir, self.workspace)

    def tearDown(self) -> None:
        self.directories.cleanup()

    def write_memory(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "prefers-uv.md").write_text(MEMORY_FILE, encoding="utf-8")
        (self.memory_dir / "MEMORY.md").write_text(
            "- [装包用 uv](prefers-uv.md) — 别用 pip\n- [别的](other.md) — 另一条\n", encoding="utf-8"
        )

    def test_directory_name_follows_claude_code(self) -> None:
        memory_dir = project_memory_dir(Path("/home"), Path("/Users/me/projects/my.app"))

        self.assertEqual(memory_dir, Path("/home/projects/-Users-me-projects-my-app/memory"))

    def test_prompt_section_has_rules_and_index(self) -> None:
        self.write_memory()

        section = memory_prompt_section(self.memory_dir)
        empty = memory_prompt_section(self.directories.home_dir / "nothing")

        self.assertIn(str(self.memory_dir), section)
        self.assertIn("- [装包用 uv](prefers-uv.md)", section)
        self.assertIn("feedback", section)
        self.assertIn("还是空的", empty)

    def test_list_and_delete_keep_the_index_in_sync(self) -> None:
        self.write_memory()

        listed = list_memories(self.memory_dir)
        delete_memory(self.memory_dir, "prefers-uv.md")

        self.assertEqual([(m.name, m.type) for m in listed], [("prefers-uv", "feedback")])
        self.assertFalse((self.memory_dir / "prefers-uv.md").exists())
        self.assertEqual((self.memory_dir / "MEMORY.md").read_text(encoding="utf-8"), "- [别的](other.md) — 另一条\n")
        with self.assertRaises(ValueError):
            delete_memory(self.memory_dir, "../config.json")

    def test_memory_writes_need_no_approval_except_in_read_only(self) -> None:
        context = ToolContext(
            workspace=self.workspace, memory_dir=self.memory_dir, secret_dirs=[self.directories.home_dir]
        )
        write = WriteFileTool().access({"path": str(self.memory_dir / "a.md"), "content": "x"}, context)
        read = ReadFileTool().access({"path": str(self.memory_dir / "a.md")}, context)
        mixed = context.path_access([self.memory_dir / "a.md", self.workspace.parent / "x.txt"], write=True)

        self.assertTrue(write.memory_only)
        self.assertEqual(write.outside_paths, [])
        self.assertEqual(read.outside_paths, [])
        self.assertFalse(mixed.memory_only)
        for mode, expected in [
            (ApprovalMode.ASK, Decision.ALLOW),
            (ApprovalMode.AI_AUTO, Decision.ALLOW),
            (ApprovalMode.READ_ONLY, Decision.DENY),
        ]:
            with self.subTest(mode=mode.value):
                self.assertEqual(PermissionGate(mode, DenyAllApprover()).decide(WriteFileTool(), write), expected)

    async def test_agent_sees_memory_and_writes_it_without_asking(self) -> None:
        self.write_memory()
        approver = RecordingApprover()
        new_memory = self.memory_dir / "likes-tests.md"
        responses = [
            LLMResponse(
                tool_calls=[
                    tool_call(
                        "c1",
                        "write_file",
                        f'{{"path": "{new_memory}", "content": "---\\nname: likes-tests\\ndescription: 改完跑测试\\ntype: feedback\\n---\\n"}}',
                    )
                ]
            ),
            LLMResponse(text="记下了"),
        ]
        harness = build_harness(self.directories, responses, mode=ApprovalMode.ASK, approver=approver)

        await harness.agent.run_turn("记住：改完代码要跑测试")

        self.assertIn("- [装包用 uv](prefers-uv.md)", harness.agent.context.messages[0]["content"])
        self.assertTrue(new_memory.is_file())
        self.assertEqual(approver.requests, [])

    async def test_memory_can_be_turned_off(self) -> None:
        harness = build_harness(self.directories, [], memory_enabled=False)

        self.assertNotIn("项目记忆", harness.agent.context.messages[0]["content"])
        self.assertIsNone(harness.agent.context.tool_context.memory_dir)


if __name__ == "__main__":
    unittest.main()
