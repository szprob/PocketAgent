"""沙箱、工作目录外的访问。技能和记忆在 test_skills_memory.py。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pocketagent.llm import LLMResponse
from pocketagent.permissions import (
    ApprovalAnswer,
    ApprovalMode,
    ApprovalRequest,
    Decision,
    DenyAllApprover,
    PermissionGate,
)
from pocketagent.sandbox import (
    SandboxPolicy,
    available_sandbox,
    bubblewrap_arguments,
    denial_hint,
    seatbelt_profile,
    wrap_command,
)
from pocketagent.tools import ToolAccess, ToolContext
from pocketagent.tools.files import EditFileTool, ReadFileTool
from pocketagent.tools.search import GlobTool, GrepTool
from pocketagent.tools.shell import BashTool
from tests.helpers import TemporaryDirectories, build_harness, tool_call


class RecordingApprover:
    def __init__(self, answer: ApprovalAnswer = ApprovalAnswer.ALLOW_ONCE) -> None:
        self.answer = answer
        self.requests: list[ApprovalRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        self.requests.append(request)
        return self.answer


def run_in_sandbox(command: str, policy: SandboxPolicy) -> subprocess.CompletedProcess:
    arguments, _ = wrap_command(command, policy)
    return subprocess.run(arguments, cwd=policy.workspace, capture_output=True, text=True, timeout=30)


@unittest.skipUnless(available_sandbox() == "seatbelt", "只在有 sandbox-exec 的 macOS 上测")
class SeatbeltSandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        # 临时目录在沙箱里本来就能写，所以「工作目录外」要放到家目录下
        self.outside = Path(tempfile.mkdtemp(prefix="pocketagent-sandbox-test-", dir=Path.home())).resolve()
        self.secret = self.outside / "secret"
        self.secret.mkdir()
        (self.secret / "key").write_text("TOP-SECRET", encoding="utf-8")
        self.allowed = self.secret / "attachments"
        self.allowed.mkdir()
        (self.allowed / "note.txt").write_text("attachment", encoding="utf-8")
        self.policy = SandboxPolicy(
            workspace=self.workspace, secret_dirs=[self.secret], readable_exceptions=[self.allowed]
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.outside, ignore_errors=True)
        self.directories.cleanup()

    def test_workspace_and_temp_are_writable(self) -> None:
        result = run_in_sandbox("echo hi > inside.txt && f=$(mktemp) && echo ok > $f && cat inside.txt $f", self.policy)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["hi", "ok"])

    def test_writing_outside_the_workspace_is_blocked(self) -> None:
        target = self.outside / "escape.txt"

        result = run_in_sandbox(f"echo bad > {target}", self.policy)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Operation not permitted", result.stderr)
        self.assertFalse(target.exists())

    def test_secrets_are_unreadable_except_listed_exceptions(self) -> None:
        secret = run_in_sandbox(f"cat {self.secret / 'key'}", self.policy)
        exception = run_in_sandbox(f"cat {self.allowed / 'note.txt'}", self.policy)

        self.assertNotIn("TOP-SECRET", secret.stdout)
        self.assertIn("Operation not permitted", secret.stderr)
        self.assertEqual(exception.stdout, "attachment")

    def test_git_hooks_and_config_are_protected(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)

        hooks = run_in_sandbox("echo 'echo pwned' > .git/hooks/pre-commit", self.policy)
        config = run_in_sandbox("echo '[core]' >> .git/config", self.policy)
        commit = run_in_sandbox(
            "echo x > a.txt && git add a.txt && git -c user.name=t -c user.email=t@t commit -qm x && git log --oneline",
            self.policy,
        )

        self.assertNotEqual(hooks.returncode, 0)
        self.assertNotEqual(config.returncode, 0)
        self.assertEqual(commit.returncode, 0, commit.stderr)

    def test_network_is_off_unless_allowed(self) -> None:
        profile_without = seatbelt_profile(self.policy)
        profile_with = seatbelt_profile(SandboxPolicy(workspace=self.workspace, allow_network=True))

        self.assertNotIn("(allow network*)", profile_without)
        self.assertIn("(allow network*)", profile_with)
        result = run_in_sandbox(
            "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\"", self.policy
        )
        self.assertNotEqual(result.returncode, 0)


class ScratchWorkspaceSandboxTest(unittest.TestCase):
    """临时任务的工作区在 ~/.pocketagent 下，而那是密钥目录：沙箱要单独把它放开。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.secret = self.directories.home_dir
        self.scratch = self.secret / "scratch" / "20261026-120000-abcdef"
        self.scratch.mkdir(parents=True)
        (self.secret / "config.json").write_text('{"api_key": "sk-secret"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_only_a_workspace_inside_the_secret_directory_is_an_exception(self) -> None:
        inside = SandboxPolicy(workspace=self.scratch, secret_dirs=[self.secret])
        outside = SandboxPolicy(workspace=self.directories.workspace, secret_dirs=[self.secret])

        self.assertEqual(inside.writable_inside_secret(), [self.scratch.resolve()])
        self.assertEqual(outside.writable_inside_secret(), [])

    def test_seatbelt_reads_the_workspace_but_still_not_the_config(self) -> None:
        profile = seatbelt_profile(SandboxPolicy(workspace=self.scratch, secret_dirs=[self.secret]))
        read_exceptions = [line for line in profile.splitlines() if line.startswith("(allow file-read* (subpath")]

        self.assertEqual(len(read_exceptions), 1)
        self.assertIn(str(self.scratch.resolve()), read_exceptions[0])
        self.assertIn(f'(deny file-read* (subpath "{self.secret.resolve()}")', profile)
        self.assertNotIn("config.json", profile)

    def test_bubblewrap_mounts_the_workspace_back_after_hiding_the_secret_directory(self) -> None:
        arguments = bubblewrap_arguments(SandboxPolicy(workspace=self.scratch, secret_dirs=[self.secret]))
        hidden_at = arguments.index(str(self.secret.resolve()))
        workspace_binds = [index for index, item in enumerate(arguments) if item == str(self.scratch.resolve())]

        self.assertTrue(workspace_binds)
        # tmpfs 盖住密钥目录之后还要能写，所以必须有一次更靠后的 --bind
        self.assertGreater(max(workspace_binds), hidden_at)


class BashSandboxIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_outside_sandbox_parameter_only_exists_when_sandboxed(self) -> None:
        self.assertIn("outside_sandbox", BashTool(sandboxed=True).parameters["properties"])
        self.assertNotIn("outside_sandbox", BashTool(sandboxed=False).parameters["properties"])

    def test_outside_sandbox_is_reported_only_when_there_is_a_sandbox(self) -> None:
        sandboxed = ToolContext(workspace=self.workspace, sandbox=SandboxPolicy(workspace=self.workspace))
        plain = ToolContext(workspace=self.workspace)
        arguments = {"command": "pip install x", "outside_sandbox": True}

        self.assertTrue(BashTool(sandboxed=True).access(arguments, sandboxed).outside_sandbox)
        self.assertFalse(BashTool().access(arguments, plain).outside_sandbox)

    def test_denial_hint_mentions_outside_sandbox(self) -> None:
        policy = SandboxPolicy(workspace=self.workspace)

        self.assertIn("outside_sandbox=true", denial_hint("touch: /x: Operation not permitted", policy))
        self.assertEqual(denial_hint("touch: /x: Operation not permitted", None), "")
        self.assertEqual(denial_hint("all good", policy), "")

    @unittest.skipUnless(available_sandbox() == "seatbelt", "只在有 sandbox-exec 的 macOS 上测")
    async def test_bash_tool_runs_inside_the_sandbox(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="pocketagent-bash-test-", dir=Path.home())).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        context = ToolContext(workspace=self.workspace, sandbox=SandboxPolicy(workspace=self.workspace))
        command = f"echo hi > {outside / 'x.txt'}"

        caged = await BashTool(sandboxed=True).run({"command": command}, context)
        free = await BashTool(sandboxed=True).run({"command": command, "outside_sandbox": True}, context)

        self.assertTrue(caged.is_error)
        self.assertIn("outside_sandbox=true", caged.content)
        self.assertFalse(free.is_error, free.content)
        self.assertEqual((outside / "x.txt").read_text(), "hi\n")


class PermissionCheckTest(unittest.TestCase):
    def gate(self, mode: ApprovalMode) -> PermissionGate:
        return PermissionGate(mode=mode, approver=DenyAllApprover())

    def test_outside_paths_by_mode(self) -> None:
        outside = ToolAccess(outside_paths=[Path("/opt/elsewhere/a.txt")])
        expectations = {
            # (模式, 工具): 结果
            (ApprovalMode.ASK, "read"): Decision.ASK,
            (ApprovalMode.ASK, "edit"): Decision.ASK,
            (ApprovalMode.AI_AUTO, "read"): Decision.AI_REVIEW,
            (ApprovalMode.AI_AUTO, "edit"): Decision.ASK,
            (ApprovalMode.FULL_AUTO, "read"): Decision.ALLOW,
            (ApprovalMode.FULL_AUTO, "edit"): Decision.ALLOW,
            (ApprovalMode.READ_ONLY, "read"): Decision.ASK,
            (ApprovalMode.READ_ONLY, "edit"): Decision.DENY,
        }
        tools = {"read": ReadFileTool(), "edit": EditFileTool()}
        for (mode, tool_name), expected in expectations.items():
            with self.subTest(mode=mode.value, tool=tool_name):
                self.assertEqual(self.gate(mode).decide(tools[tool_name], outside), expected)

    def test_reads_inside_the_workspace_need_nothing(self) -> None:
        self.assertEqual(self.gate(ApprovalMode.ASK).decide(ReadFileTool(), ToolAccess()), Decision.ALLOW)

    def test_outside_sandbox_always_asks_a_human(self) -> None:
        access = ToolAccess(outside_sandbox=True, command="npm install")
        bash = BashTool(sandboxed=True)

        self.assertEqual(self.gate(ApprovalMode.ASK).decide(bash, access), Decision.ASK)
        self.assertEqual(self.gate(ApprovalMode.AI_AUTO).decide(bash, access), Decision.ASK)
        self.assertEqual(self.gate(ApprovalMode.FULL_AUTO).decide(bash, access), Decision.ALLOW)
        self.assertEqual(self.gate(ApprovalMode.AI_AUTO).decide(bash, ToolAccess()), Decision.AI_REVIEW)

    def test_secrets_ask_even_in_full_auto_and_cannot_be_always_allowed(self) -> None:
        secret = Path.home() / ".ssh" / "id_rsa"
        check = self.gate(ApprovalMode.FULL_AUTO).check(
            ReadFileTool(), ToolAccess(outside_paths=[secret], secret_paths=[secret])
        )

        self.assertEqual(check.decision, Decision.ASK)
        self.assertEqual(check.always_label(), "")
        self.assertTrue(any("密钥" in note for note in check.notes))

    def test_always_allow_remembers_the_directory(self) -> None:
        gate = self.gate(ApprovalMode.ASK)
        access = ToolAccess(outside_paths=[Path("/opt/elsewhere/a.txt")])
        check = gate.check(ReadFileTool(), access)

        gate.remember(check)

        self.assertIn("/opt/elsewhere", check.always_label())
        self.assertEqual(gate.decide(ReadFileTool(), ToolAccess(outside_paths=[Path("/opt/elsewhere/sub/b.txt")])), Decision.ALLOW)
        self.assertEqual(gate.decide(ReadFileTool(), ToolAccess(outside_paths=[Path("/opt/other.txt")])), Decision.ASK)

    def test_always_allow_for_edit_outside_remembers_tool_and_directory(self) -> None:
        gate = self.gate(ApprovalMode.ASK)
        check = gate.check(EditFileTool(), ToolAccess(outside_paths=[Path("/opt/elsewhere/a.txt")]))

        gate.remember(check)

        self.assertIn("edit_file", check.always_label())
        self.assertEqual(gate.decide(EditFileTool(), ToolAccess(outside_paths=[Path("/opt/elsewhere/b.txt")])), Decision.ALLOW)


class PathAccessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.root = self.workspace.parent
        self.secret = self.root / "home" / ".pocketagent"
        self.attachments = self.secret / "attachments" / "s1"
        self.attachments.mkdir(parents=True)
        self.context = ToolContext(
            workspace=self.workspace, extra_read_dirs=[self.attachments], secret_dirs=[self.secret]
        )

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_attachments_inside_a_secret_dir_are_free_to_read_but_not_to_write(self) -> None:
        image = self.attachments / "a.png"

        read = self.context.path_access([image], write=False)
        write = self.context.path_access([image], write=True)
        config = self.context.path_access([self.secret / "config.json"], write=False)

        self.assertEqual(read.outside_paths, [])
        self.assertEqual(write.secret_paths, [image])
        self.assertEqual(config.secret_paths, [self.secret / "config.json"])

    async def test_glob_rejects_patterns_that_climb_out(self) -> None:
        result = await GlobTool().run({"pattern": "../**/*.txt"}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("path 参数", result.content)

    async def test_searching_from_outside_skips_secret_dirs(self) -> None:
        (self.secret / "config.json").write_text('{"api_key": "needle"}', encoding="utf-8")
        (self.root / "notes.txt").write_text("needle here\n", encoding="utf-8")

        result = await GrepTool().run({"pattern": "needle", "path": str(self.root)}, self.context)

        self.assertIn("notes.txt", result.content)
        self.assertNotIn("config.json", result.content)


class OutsideAccessFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.outside_file = self.directories.workspace.parent / "elsewhere" / "doc.txt"
        self.outside_file.parent.mkdir()
        self.outside_file.write_text("outside content\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def responses(self) -> list[LLMResponse]:
        return [
            LLMResponse(tool_calls=[tool_call("c1", "read_file", f'{{"path": "{self.outside_file}"}}')]),
            LLMResponse(text="读完了"),
        ]

    async def test_reading_outside_the_workspace_asks_with_a_note(self) -> None:
        approver = RecordingApprover()
        harness = build_harness(self.directories, self.responses(), mode=ApprovalMode.ASK, approver=approver)

        await harness.agent.run_turn("看看那个文件")

        self.assertEqual(len(approver.requests), 1)
        request = approver.requests[0]
        self.assertTrue(any(str(self.outside_file) in note for note in request.notes))
        self.assertIn(str(self.outside_file.parent), request.always_label)
        tool_message = next(m for m in harness.agent.context.messages if m["role"] == "tool")
        self.assertIn("outside content", tool_message["content"])

    async def test_full_auto_reads_outside_without_asking(self) -> None:
        approver = RecordingApprover()
        harness = build_harness(self.directories, self.responses(), mode=ApprovalMode.FULL_AUTO, approver=approver)

        await harness.agent.run_turn("看看那个文件")

        self.assertEqual(approver.requests, [])


if __name__ == "__main__":
    unittest.main()
