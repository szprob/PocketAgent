"""系统提示词拼装：运行环境、工具清单、沙箱说明、四种模式、技能 / 记忆、AGENTS.md 和 CLAUDE.md。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from pocketagent.core.prompts import build_system_prompt, load_instruction_files
from pocketagent.interaction.modes import AgentMode
from pocketagent.knowledge.skills import Skill
from pocketagent.safety.sandbox import SandboxPolicy
from pocketagent.tools import ToolRegistry
from pocketagent.tools.files import ReadFileTool
from pocketagent.tools.shell import BashTool
from pocketagent.tools.skill import ActivateSkillTool


class SystemPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()

    def build(self, tools=None, **overrides) -> str:
        return build_system_prompt(
            workspace=self.workspace,
            home_dir=self.home,
            tools=tools or ToolRegistry([BashTool()]),
            **overrides,
        )

    def test_environment_tools_and_git_status(self) -> None:
        prompt = self.build()

        self.assertIn(f"工作目录：{self.workspace}", prompt)
        self.assertIn("是否 git 仓库：否", prompt)
        self.assertIn(f"今天：{date.today().isoformat()}", prompt)
        self.assertIn("可用工具：bash", prompt)
        self.assertIn("bash 命令不在沙箱里", prompt)

    def test_git_repository_worktree_and_ephemeral_notes(self) -> None:
        (self.workspace / ".git").mkdir()
        worktree = {"origin": "/src/origin", "branch": "pocketagent/task", "base_commit": "abc123"}

        prompt = self.build(ephemeral=True, worktree=worktree)

        self.assertIn("是否 git 仓库：是", prompt)
        self.assertIn("这是临时任务", prompt)
        self.assertIn("这是独立 worktree", prompt)
        self.assertIn("/src/origin", prompt)
        self.assertIn("pocketagent/task", prompt)
        self.assertIn("abc123", prompt)

    def test_sandbox_section_only_when_bash_is_available(self) -> None:
        sandboxed = self.build(sandbox=SandboxPolicy(workspace=self.workspace))
        self.assertIn("bash 命令在沙箱里执行", sandboxed)
        self.assertIn("不能联网", sandboxed)

        without_bash = self.build(tools=ToolRegistry([ReadFileTool()]), sandbox=SandboxPolicy(workspace=self.workspace))
        self.assertNotIn("bash 命令在沙箱里执行", without_bash)
        self.assertIn("可用工具：read_file", without_bash)

    def test_each_agent_mode_has_its_own_prompt(self) -> None:
        markers = {
            AgentMode.AGENT: "关于计划模式",
            AgentMode.ASK: "【Ask 模式】",
            AgentMode.PLAN: "【计划模式】",
            AgentMode.DESIGN: "【设计模式】",
        }
        for mode, marker in markers.items():
            with self.subTest(mode=mode):
                self.assertIn(marker, self.build(agent_mode=mode))

    def test_instruction_files_are_loaded_home_first(self) -> None:
        (self.home / "AGENTS.md").write_text("用户级规则", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("项目级规则", encoding="utf-8")
        (self.workspace / "CLAUDE.md").write_text("兼容规则", encoding="utf-8")

        files = load_instruction_files(self.workspace, self.home)
        self.assertEqual(
            [file.path for file in files],
            [self.home / "AGENTS.md", self.workspace / "AGENTS.md", self.workspace / "CLAUDE.md"],
        )
        prompt = self.build()
        self.assertLess(prompt.index("用户级规则"), prompt.index("项目级规则"))
        self.assertLess(prompt.index("项目级规则"), prompt.index("兼容规则"))

    def test_skills_and_memory_sections(self) -> None:
        skill_path = self.home / "skills" / "deploy" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("---\nname: deploy\ndescription: 部署服务\n---\n", encoding="utf-8")
        skill = Skill(name="deploy", description="部署服务", path=skill_path, source="用户")
        memory_dir = self.home / "projects" / "demo" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text("- [部署约定](deploy.md) — 用蓝绿发布\n", encoding="utf-8")

        prompt = self.build(
            tools=ToolRegistry([BashTool(), ActivateSkillTool([skill])]),
            skills=[skill],
            memory_dir=memory_dir,
        )

        self.assertIn("<name>deploy</name>", prompt)
        self.assertIn("部署服务", prompt)
        self.assertIn("项目记忆", prompt)
        self.assertIn("用蓝绿发布", prompt)
