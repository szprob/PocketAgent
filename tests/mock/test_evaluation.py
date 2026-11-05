"""trace 回放和评测：读 trace、在临时副本里重放、检查点找不到时拒绝、评测检查的通过和失败、报告落盘。

trace 都是在这里手写的（不拿真实 trace），~/.pocketagent 换成临时目录（POCKETAGENT_HOME），不碰本机的配置和记录。
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pocketagent.cli import console
from pocketagent.config import Settings
from pocketagent.core import agent as agent_module
from pocketagent.evaluation.cases import (
    CaseError,
    EvalCheck,
    answer_contains_check,
    load_cases,
    run_command_check,
)
from pocketagent.evaluation.evaluate import EvalRequest, evaluate_cases
from pocketagent.evaluation.replay import ReplayError, ReplayRequest, replay_trace
from pocketagent.evaluation.report import display_width, truncate_to_width
from pocketagent.evaluation.runner import UnattendedApprover, unattended_settings
from pocketagent.evaluation.trace_reader import TraceError, first_line, read_trace
from pocketagent.llm import LLMResponse, ScriptedProvider
from pocketagent.llm.base import Usage
from pocketagent.safety.checkpoints import (
    ShadowRepo,
    export_snapshot,
    find_snapshot,
    git_available,
)
from pocketagent.safety.hooks import HookDefinition
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalMode, ApprovalRequest
from pocketagent.safety.processes import bash_executable
from pocketagent.safety.sandbox import available_sandbox
from tests.helpers import tool_call

HAS_GIT = git_available()
HAS_BASH = bash_executable() is not None


def trace_line(record_type: str, time: float, **data) -> str:
    return json.dumps({"type": record_type, "time": time, **data}, ensure_ascii=False)


def write_trace(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def two_turn_trace_lines() -> list[str]:
    """第一轮：两步、三个工具调用（成功 / 出错 / 被拒绝）、一次报错、正常结束；
    第二轮中间有一行写坏了，也没有 turn_end（像是进程被杀了）。"""
    return [
        trace_line(
            "turn_start",
            100.0,
            session_id="20260101-000000-abc123",
            user_text="把 notes.txt 改一下\n第二行细节",
            attachments=[{"id": "a", "name": "x.png", "kind": "image", "size": 3}],
            workspace="/tmp/project",
            model="model-one",
            model_id="one",
            model_name="模型一",
        ),
        trace_line("node_start", 100.1, node="GuardNode"),
        trace_line("node_start", 100.2, node="ThinkNode"),
        trace_line("checkpoint", 100.3, commit="a" * 40, label="任务开始时"),
        trace_line(
            "llm_response",
            101.0,
            model="model-one",
            text="",
            tool_calls=[],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 50, "cost": 0.01},
        ),
        trace_line("tool_call", 101.1, call_id="c1", name="read_file", summary="notes.txt", arguments={}),
        trace_line("tool_result", 101.2, call_id="c1", name="read_file", content="hi", is_error=False, denied=False),
        trace_line("tool_call", 101.3, call_id="c2", name="bash", summary="make", arguments={}),
        trace_line("tool_result", 101.4, call_id="c2", name="bash", content="boom", is_error=True, denied=False),
        trace_line("tool_call", 101.5, call_id="c3", name="write_file", summary="/etc/x", arguments={}),
        trace_line("tool_result", 101.6, call_id="c3", name="write_file", content="拒绝", is_error=True, denied=True),
        trace_line("node_start", 101.7, node="ThinkNode"),
        trace_line("error", 101.8, message="APIConnectionError: 断了一下"),
        trace_line(
            "llm_response",
            102.0,
            model="model-one",
            text="改好了\n细节",
            tool_calls=[],
            usage={"prompt_tokens": 200, "completion_tokens": 20, "cost": 0.02},
        ),
        trace_line("turn_end", 103.5, stop_reason="done", final_text="\n改好了\n细节"),
        trace_line("turn_start", 200.0, session_id="20260101-000000-abc123", user_text="再看看", attachments=[]),
        '{"type": "llm_response", "time": 201',
        trace_line("node_start", 201.0, node="ThinkNode"),
        trace_line("interjection_applied", 202.0, user_text="顺便看下 README", attachments=[]),
        trace_line("tool_call", 204.0, call_id="c9", name="grep", summary="x", arguments={}),
    ]


class TraceReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_summarizes_each_turn_and_skips_garbled_lines(self) -> None:
        path = write_trace(self.root / "20260101-000000-abc123.jsonl", two_turn_trace_lines())

        summary = read_trace(path)

        self.assertEqual(summary.session_id, "20260101-000000-abc123")
        self.assertEqual(summary.workspace, "/tmp/project")
        self.assertEqual(summary.skipped_lines, 1)
        self.assertEqual(len(summary.turns), 2)
        first, second = summary.turns
        self.assertEqual(first.user_text, "把 notes.txt 改一下\n第二行细节")
        self.assertEqual(first.models, ["model-one"])
        self.assertEqual(first.model_id, "one")
        self.assertEqual(first.steps, 2)
        self.assertEqual([call.status for call in first.tool_calls], ["ok", "error", "denied"])
        self.assertEqual(first.error_messages, ["APIConnectionError: 断了一下"])
        self.assertEqual((first.prompt_tokens, first.completion_tokens, first.cached_tokens), (300, 30, 50))
        self.assertAlmostEqual(first.cost, 0.03)
        self.assertEqual(first.checkpoint_commit, "a" * 40)
        self.assertEqual(first.attachment_count, 1)
        self.assertEqual(first.stop_reason, "done")
        self.assertAlmostEqual(first.seconds, 3.5)
        # 第二轮被截断：没有 turn_end，耗时算到最后一条记录；没有结果的工具调用记为 missing
        self.assertEqual(second.stop_reason, "incomplete")
        self.assertAlmostEqual(second.seconds, 4.0)
        self.assertEqual(second.interjections, ["顺便看下 README"])
        self.assertEqual([call.status for call in second.tool_calls], ["missing"])

        totals = summary.totals()
        self.assertFalse(totals.succeeded)
        self.assertEqual(totals.steps, 3)
        self.assertEqual(totals.tool_calls, 4)
        # 一个工具出错 + 一次报错；被拒绝的单独算
        self.assertEqual(totals.errors, 2)
        self.assertEqual(totals.denied_calls, 1)
        self.assertEqual(totals.final_text, "")
        self.assertEqual(summary.start_checkpoint().index, 1)
        self.assertEqual(first_line(first.final_text), "改好了")

    def test_old_trace_without_model_fields(self) -> None:
        """加模型字段之前的 trace：没有模型、也没有费用；记录出现在第一轮之前的算孤儿。"""
        path = write_trace(
            self.root / "old.jsonl",
            [
                trace_line("node_start", 9.0, node="ThinkNode"),
                trace_line("turn_start", 10.0, session_id="old", user_text="你好", attachments=[]),
                trace_line("llm_response", 11.0, text="你好呀", tool_calls=[], usage=None),
                trace_line("turn_end", 12.0, stop_reason="done", final_text="你好呀"),
            ],
        )

        summary = read_trace(path)

        self.assertEqual(summary.orphan_records, 1)
        self.assertEqual(summary.turns[0].models, [])
        totals = summary.totals()
        self.assertTrue(totals.succeeded)
        self.assertIsNone(totals.cost)
        self.assertIsNone(summary.start_checkpoint())

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(TraceError):
            read_trace(self.root / "nope.jsonl")


class TableTextTest(unittest.TestCase):
    def test_width_counts_chinese_as_two_columns(self) -> None:
        self.assertEqual(display_width("ab中文"), 6)
        truncated = truncate_to_width("一二三四五六", 7)
        self.assertTrue(truncated.endswith("…"))
        self.assertLessEqual(display_width(truncated), 7)


class UnattendedPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_sandboxed_bash_inside_the_copy_is_approved(self) -> None:
        approver = UnattendedApprover(allow_sandboxed_bash=True)
        plain_bash = ApprovalRequest(tool_name="bash", summary="ls", preview="")
        escaping_bash = ApprovalRequest(tool_name="bash", summary="ls", preview="", notes=["要在沙箱外执行"])
        outside_edit = ApprovalRequest(tool_name="write_file", summary="/etc/x", preview="", notes=["工作目录以外"])
        mcp_call = ApprovalRequest(tool_name="mcp__db__drop", summary="", preview="")

        self.assertEqual(await approver.request_approval(plain_bash), ApprovalAnswer.ALLOW_ONCE)
        self.assertEqual(await approver.request_approval(escaping_bash), ApprovalAnswer.DENY)
        self.assertEqual(await approver.request_approval(outside_edit), ApprovalAnswer.DENY)
        self.assertEqual(await approver.request_approval(mcp_call), ApprovalAnswer.DENY)
        self.assertIsNone(await approver.ask_questions([]))
        # 没有沙箱：命令没有隔离，bash 也拒绝
        no_sandbox = UnattendedApprover(allow_sandboxed_bash=False)
        self.assertEqual(await no_sandbox.request_approval(plain_bash), ApprovalAnswer.DENY)

    def test_settings_drop_everything_that_reaches_outside_the_copy(self) -> None:
        base = Settings(
            workspace=Path("/real/project"),
            approval_mode=ApprovalMode.FULL_AUTO,
            trusted_dirs=["/real"],
            allow_outside_workspace=True,
            hooks=[HookDefinition(event="turn_end", command="say done")],
            computer_use=True,
            sandbox_enabled=False,
            worktree_background=True,
        )
        copy_dir = Path("/tmp/copy")

        settings = unattended_settings(base, copy_dir)

        self.assertEqual(settings.workspace, copy_dir)
        self.assertEqual(settings.approval_mode, ApprovalMode.AUTO_EDIT)
        self.assertEqual(settings.trusted_dirs, [])
        self.assertFalse(settings.allow_outside_workspace)
        self.assertEqual(settings.hooks, [])
        self.assertFalse(settings.computer_use)
        self.assertFalse(settings.memory_enabled)
        self.assertTrue(settings.sandbox_enabled)
        self.assertFalse(settings.worktree_background)
        # 原来的设置不动
        self.assertEqual(base.approval_mode, ApprovalMode.FULL_AUTO)
        self.assertEqual(base.trusted_dirs, ["/real"])


class IsolatedHomeTestCase(unittest.IsolatedAsyncioTestCase):
    """临时的 ~/.pocketagent（带两个模型的配置）和一个「用户的真实工作区」。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text(
            json.dumps(
                {
                    "models": [
                        {"id": "one", "name": "模型一", "model": "model-one", "base_url": "https://gateway.example.com/v1"},
                        {"id": "two", "name": "模型二", "model": "model-two", "base_url": "https://gateway.example.com/v1"},
                    ],
                    "default_model": "one",
                    # 别去读本机装的技能，结果不受机器影响
                    "skills_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        environment = mock.patch.dict(os.environ, {"POCKETAGENT_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.built_settings: list[Settings] = []
        self.providers: dict[str, ScriptedProvider] = {}

    def factory_with_scripts(self, scripts: dict[str, list[LLMResponse]]):
        """按模型名给每次运行一个按剧本回复的假模型，顺手记下每次运行用的设置。"""

        def build(settings: Settings) -> ScriptedProvider:
            self.built_settings.append(settings)
            provider = ScriptedProvider(list(scripts[settings.model]))
            self.providers[settings.model] = provider
            return provider

        return build

    def report_files(self) -> list[Path]:
        return sorted((self.home / "evals").glob("*/report.*"))


def read_then_write_script(final_text: str) -> list[LLMResponse]:
    return [
        LLMResponse(tool_calls=[tool_call("r1", "read_file", '{"path": "notes.txt"}')]),
        LLMResponse(
            tool_calls=[tool_call("w1", "write_file", '{"path": "notes.txt", "content": "回放写的\\n"}')],
            usage=Usage(prompt_tokens=40, completion_tokens=5),
        ),
        LLMResponse(text=final_text, usage=Usage(prompt_tokens=60, completion_tokens=8, cost=0.002)),
    ]


@unittest.skipUnless(HAS_GIT, "这台机器上没有 git")
class ReplayTest(IsolatedHomeTestCase):
    async def asyncSetUp(self) -> None:
        # 原任务开始时工作区的样子拍进影子仓库，之后用户又改过：回放必须用当时那份
        (self.workspace / "notes.txt").write_text("原始内容\n", encoding="utf-8")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        repo = ShadowRepo(self.home / "shadow", self.workspace, "20260101-000000-abc123")
        self.commit = await repo.snapshot("任务开始时")
        (self.workspace / "notes.txt").write_text("后来改过\n", encoding="utf-8")

    def write_original_trace(self, commit: str) -> Path:
        session_id = "20260101-000000-abc123"
        return write_trace(
            self.home / "traces" / f"{session_id}.jsonl",
            [
                trace_line(
                    "turn_start",
                    1.0,
                    session_id=session_id,
                    user_text="读一下 notes.txt 然后改掉它",
                    attachments=[],
                    workspace=str(self.workspace),
                    model="model-one",
                    model_id="one",
                    model_name="模型一",
                ),
                trace_line("node_start", 1.1, node="ThinkNode"),
                trace_line("checkpoint", 1.2, commit=commit, label="任务开始时"),
                trace_line("llm_response", 2.0, model="model-one", text="原来的回答", tool_calls=[], usage=None),
                trace_line("turn_end", 3.0, stop_reason="done", final_text="原来的回答"),
            ],
        )

    async def test_snapshot_export_and_lookup(self) -> None:
        destination = self.root / "exported"

        git_dir = await find_snapshot(self.home / "shadow", self.commit)
        await export_snapshot(git_dir, self.commit, destination)

        self.assertEqual((destination / "notes.txt").read_text(encoding="utf-8"), "原始内容\n")
        self.assertEqual((destination / "src" / "app.py").read_text(encoding="utf-8"), "print('v1')\n")
        self.assertFalse((destination / ".git").exists())
        # 提交号来自 trace，不是十六进制的一律当找不到，不会被 git 当成选项
        self.assertIsNone(await find_snapshot(self.home / "shadow", "--output=/tmp/x"))

    async def test_replays_in_a_restored_copy_against_each_model(self) -> None:
        trace_path = self.write_original_trace(self.commit)
        factory = self.factory_with_scripts(
            {"model-one": read_then_write_script("改好了"), "model-two": read_then_write_script("我也改好了")}
        )
        request = ReplayRequest(
            trace_path=trace_path,
            model_keys=["one", "two"],
            system_prompt_template="自定义的提示词\n{{default_system_prompt}}",
            system_prompt_file="prompt.md",
        )

        result = await replay_trace(request, provider_factory=factory, progress=lambda text: None)

        self.assertEqual([run.label for run in result.runs], ["原始", "模型一（model-one）", "模型二（model-two）"])
        original, first_run, second_run = result.runs
        self.assertEqual(original.model, "model-one")
        self.assertTrue(first_run.succeeded)
        self.assertEqual(first_run.totals.tool_calls, 2)
        self.assertEqual(first_run.totals.prompt_tokens, 100)
        self.assertAlmostEqual(first_run.totals.cost, 0.002)
        self.assertEqual(second_run.totals.final_text, "我也改好了")
        # 读到的是检查点里的内容，不是用户后来改过的
        tool_result = self.providers["model-one"].received_messages[1][-1]
        self.assertIn("原始内容", tool_result["content"])
        # 换了系统提示词，默认那份接在后面
        system_prompt = self.providers["model-one"].received_messages[0][0]["content"]
        self.assertTrue(system_prompt.startswith("自定义的提示词\n"))
        self.assertIn("PocketAgent", system_prompt)
        # 用户的真实工作区一点没动
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "后来改过\n")
        # 每次运行在自己的临时副本里，用收紧过的设置，跑完副本删掉
        for settings in self.built_settings:
            self.assertNotEqual(settings.workspace, self.workspace)
            self.assertTrue(settings.workspace.name.startswith("pocketagent-replay-"))
            self.assertFalse(settings.workspace.exists())
            self.assertEqual(settings.approval_mode, ApprovalMode.AUTO_EDIT)
        # 每次运行写了自己的 trace，记着在哪跑、用什么模型；会话文件跑完删掉
        new_trace = read_trace(Path(first_run.trace_path))
        self.assertNotEqual(Path(first_run.trace_path), trace_path)
        self.assertEqual(new_trace.turns[0].user_text, "读一下 notes.txt 然后改掉它")
        self.assertEqual(new_trace.turns[0].models, ["model-one"])
        self.assertEqual(new_trace.workspace, str(self.built_settings[0].workspace))
        self.assertEqual(list((self.home / "sessions").glob("*.json")), [])
        # 报告写在临时 HOME 的 evals 下面
        self.assertEqual(result.report_dir.parent, self.home / "evals")
        data = json.loads((result.report_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["kind"], "replay")
        self.assertEqual(len(data["runs"]), 3)
        self.assertEqual(data["system_prompt_file"], "prompt.md")
        markdown = (result.report_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("# 回放报告", markdown)
        self.assertIn("| 结果 | 成功 | 成功 | 成功 |", markdown)
        table = result.text_table()
        self.assertIn("原始", table)
        self.assertIn("改好了", table)

    async def test_missing_checkpoint_refuses_unless_workspace_given(self) -> None:
        trace_path = self.write_original_trace("b" * 40)

        with self.assertRaises(ReplayError) as raised:
            await replay_trace(ReplayRequest(trace_path=trace_path), progress=lambda text: None)
        self.assertIn("--workspace", str(raised.exception))
        self.assertEqual(self.report_files(), [])

        # 给了目录：复制它现在的样子（不带 .git）
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        factory = self.factory_with_scripts({"model-one": read_then_write_script("好了")})
        result = await replay_trace(
            ReplayRequest(trace_path=trace_path, workspace_override=self.workspace, keep_workspaces=True),
            provider_factory=factory,
            progress=lambda text: None,
        )

        replayed = result.runs[1]
        self.assertTrue(replayed.succeeded)
        self.assertIn("后来改过", self.providers["model-one"].received_messages[1][-1]["content"])
        kept = Path(replayed.workspace)
        self.addCleanup(shutil.rmtree, kept, True)
        self.assertEqual((kept / "notes.txt").read_text(encoding="utf-8"), "回放写的\n")
        self.assertFalse((kept / ".git").exists())
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "后来改过\n")

    @unittest.skipUnless(available_sandbox() is not None, "这台机器没有沙箱：回放里的 bash 会被拒绝")
    async def test_sandboxed_bash_runs_and_background_commands_are_stopped(self) -> None:
        trace_path = self.write_original_trace(self.commit)
        script = [
            LLMResponse(tool_calls=[tool_call("b1", "bash", '{"command": "echo hi > made.txt"}')]),
            LLMResponse(tool_calls=[tool_call("b2", "bash", '{"command": "sleep 30", "run_in_background": true}')]),
            LLMResponse(text="都跑起来了"),
        ]
        factory = self.factory_with_scripts({"model-one": script})

        result = await replay_trace(
            ReplayRequest(trace_path=trace_path, keep_workspaces=True),
            provider_factory=factory,
            progress=lambda text: None,
        )

        replayed = result.runs[1]
        self.addCleanup(shutil.rmtree, replayed.workspace, True)
        self.assertTrue(replayed.succeeded)
        self.assertEqual([call["status"] for call in replayed.turns[0]["tool_calls"]], ["ok", "ok"])
        self.assertEqual((Path(replayed.workspace) / "made.txt").read_text(encoding="utf-8"), "hi\n")
        # 副本删掉之前，模型留在后台的命令都停了
        statuses = [
            json.loads(meta.read_text(encoding="utf-8"))["status"]
            for meta in (self.home / "background_tasks").glob("*/meta.json")
        ]
        self.assertEqual(len(statuses), 1)
        self.assertNotIn(statuses[0], ("starting", "running"))

    async def test_trace_without_user_text_is_refused(self) -> None:
        only_node_records = [trace_line("node_start", 1.0, node="GuardNode")]
        trace_path = write_trace(self.home / "traces" / "empty.jsonl", only_node_records)

        with self.assertRaises(ReplayError):
            await replay_trace(ReplayRequest(trace_path=trace_path), progress=lambda text: None)


class EvalCasesTest(IsolatedHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cases_dir = self.root / "cases"
        fixture = self.cases_dir / "fixtures" / "calc"
        fixture.mkdir(parents=True)
        (fixture / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        # 样例目录里的 .json 不该被当成用例
        (fixture / "package.json").write_text("{}", encoding="utf-8")
        self.fixture = fixture

    def write_case(self, data, name: str = "fix-add.json") -> Path:
        path = self.cases_dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def calc_case(self) -> dict:
        return {
            "name": "修复加法",
            "prompt": "calc.py 的 add 算错了，修好它",
            "fixture": "fixtures/calc",
            "checks": [
                {"type": "command", "command": "grep -q 'a + b' calc.py"},
                {"type": "answer_contains", "text": "calc.py"},
            ],
        }

    def test_loads_cases_from_a_directory(self) -> None:
        self.write_case(self.calc_case())
        self.write_case([{**self.calc_case(), "name": "第二个", "prompt": ["先看看", "再改"]}], name="more.json")

        cases = load_cases(self.cases_dir)

        self.assertEqual([case.name for case in cases], ["修复加法", "第二个"])
        self.assertEqual(cases[0].fixture, self.fixture.resolve())
        self.assertEqual(cases[1].prompts, ["先看看", "再改"])
        self.assertEqual([check.kind for check in cases[0].checks], ["command", "answer_contains"])

    def test_bad_cases_are_reported_with_the_file(self) -> None:
        broken_variants = [
            {**self.calc_case(), "checks": []},
            {**self.calc_case(), "checks": [{"type": "regex", "pattern": "x"}]},
            {**self.calc_case(), "fixture": "fixtures/missing"},
            {**self.calc_case(), "prompt": ""},
        ]
        for variant in broken_variants:
            path = self.write_case(variant, name="broken.json")
            with self.subTest(variant=variant):
                with self.assertRaises(CaseError) as raised:
                    load_cases(path)
                self.assertIn("broken.json", str(raised.exception))
        duplicated = self.write_case([self.calc_case(), self.calc_case()], name="broken.json")
        with self.assertRaises(CaseError) as raised:
            load_cases(duplicated)
        self.assertIn("重复", str(raised.exception))
        (self.cases_dir / "broken.json").write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(CaseError):
            load_cases(self.cases_dir / "broken.json")
        with self.assertRaises(CaseError):
            load_cases(self.root / "no-such-cases")

    def test_shipped_example_case_is_valid(self) -> None:
        examples = Path(__file__).resolve().parents[2] / "examples" / "eval"

        cases = load_cases(examples)

        self.assertEqual([case.name for case in cases], ["修复加法"])
        self.assertTrue((cases[0].fixture / "calc.py").is_file())

    def test_answer_check(self) -> None:
        check = EvalCheck(kind="answer_contains", text="calc.py")
        self.assertTrue(answer_contains_check(check, "改了 calc.py").passed)
        failed = answer_contains_check(check, "改好了")
        self.assertFalse(failed.passed)
        self.assertIn("calc.py", failed.detail)

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_command_check_reports_exit_code_and_timeout(self) -> None:
        passed = await run_command_check(EvalCheck(kind="command", command="true"), self.root, sandbox=None)
        failing_check = EvalCheck(kind="command", command="echo 坏了; exit 3")
        failed = await run_command_check(failing_check, self.root, sandbox=None)
        timed_out = await run_command_check(
            EvalCheck(kind="command", command="sleep 5", timeout_seconds=1), self.root, sandbox=None
        )

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)
        self.assertIn("退出码 3", failed.detail)
        self.assertIn("坏了", failed.detail)
        self.assertFalse(timed_out.passed)
        self.assertIn("超过 1 秒", timed_out.detail)

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_one_model_passes_and_the_other_fails(self) -> None:
        self.write_case(self.calc_case())
        fixing_script = [
            LLMResponse(
                tool_calls=[
                    tool_call("e1", "edit_file", '{"path": "calc.py", "old_string": "a - b", "new_string": "a + b"}')
                ]
            ),
            LLMResponse(text="改好了 calc.py"),
        ]
        lazy_script = [LLMResponse(text="看起来没问题")]
        factory = self.factory_with_scripts({"model-one": fixing_script, "model-two": lazy_script})

        result = await evaluate_cases(
            EvalRequest(cases_path=self.cases_dir, model_keys=["one", "two"]),
            provider_factory=factory,
            progress=lambda text: None,
        )

        self.assertFalse(result.all_passed)
        fixed_run, lazy_run = result.case_results[0].runs
        self.assertTrue(fixed_run.passed, [check.detail for check in fixed_run.checks])
        self.assertEqual([check.passed for check in fixed_run.checks], [True, True])
        self.assertFalse(lazy_run.passed)
        self.assertEqual([check.passed for check in lazy_run.checks], [False, False])
        # 样例目录本身没被改
        self.assertIn("a - b", (self.fixture / "calc.py").read_text(encoding="utf-8"))
        self.assertEqual([row.cells for row in result.summary_rows()][-1], ["1/1", "0/1"])
        data = json.loads((result.report_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["kind"], "eval")
        self.assertEqual(data["summary"], [
            {"model": "模型一（model-one）", "passed": 1, "total": 1},
            {"model": "模型二（model-two）", "passed": 0, "total": 1},
        ])
        markdown = (result.report_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("没过「回答里包含「calc.py」」", markdown)
        self.assertIn("检查：命令 `grep -q 'a + b' calc.py` 退出码为 0", result.text_tables())


class EvaluationCommandLineTest(IsolatedHomeTestCase):
    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exited:
            console.main(list(arguments))
        return exited.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_missing_trace_is_a_clear_error(self) -> None:
        code, _, error_output = self.run_main("replay", str(self.root / "nope.jsonl"))

        self.assertEqual(code, 1)
        self.assertIn("找不到 trace", error_output)

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    def test_eval_command_prints_tables_and_exit_code(self) -> None:
        cases_dir = self.root / "cases"
        cases_dir.mkdir()
        (cases_dir / "hello.json").write_text(
            json.dumps(
                {
                    "name": "打招呼",
                    "prompt": "写一个 hello.txt",
                    "checks": [
                        {"type": "command", "command": "test -f hello.txt"},
                        {"type": "answer_contains", "text": "hello"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        script = [
            LLMResponse(tool_calls=[tool_call("w1", "write_file", '{"path": "hello.txt", "content": "hi"}')]),
            LLMResponse(text="写好了 hello.txt"),
        ]
        factory = self.factory_with_scripts({"model-one": script})

        with mock.patch.object(agent_module, "create_provider", factory):
            code, output, _ = self.run_main("eval", str(cases_dir))

        self.assertEqual(code, 0)
        self.assertIn("汇总", output)
        self.assertIn("1/1", output)
        self.assertIn(str(self.home / "evals"), output)


if __name__ == "__main__":
    unittest.main()
