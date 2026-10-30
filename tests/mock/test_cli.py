"""命令行入口：参数解析、设置合成、会话列表和各模式的跳转。

这里只测命令行壳子，不真的调模型；模型交互在 agent / live 测试里。
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
from pocketagent.core.session import SessionStore
from pocketagent.safety.permissions import ApprovalMode


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        (self.home / "config.json").write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "id": "one",
                            "name": "模型一",
                            "model": "model-one",
                            "base_url": "https://gateway.example.com/v1",
                            "api_key": "sk-test-one",
                        },
                        {
                            "id": "two",
                            "name": "模型二",
                            "model": "model-two",
                            "base_url": "https://gateway.example.com/v1",
                            "api_key": "sk-test-two",
                        },
                    ],
                    "default_model": "one",
                }
            ),
            encoding="utf-8",
        )
        environment = mock.patch.dict(os.environ, {"POCKETAGENT_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def parse(self, *arguments: str):
        return console._parse_arguments(list(arguments))

    def test_parse_defaults_and_short_flags(self) -> None:
        defaults = self.parse()
        self.assertFalse(defaults.print_mode)
        self.assertIsNone(defaults.workspace)
        self.assertEqual(defaults.prompt, [])
        self.assertEqual(defaults.host, "127.0.0.1")

        parsed = self.parse(
            "-p", "-C", str(self.workspace), "-m", "two", "--mode", "read_only", "hello", "world"
        )
        self.assertTrue(parsed.print_mode)
        self.assertEqual(parsed.workspace, str(self.workspace))
        self.assertEqual(parsed.model, "two")
        self.assertEqual(parsed.mode, "read_only")
        self.assertEqual(parsed.prompt, ["hello", "world"])

    def test_settings_from_arguments_use_model_workspace_and_mode(self) -> None:
        settings = console._settings_from_arguments(
            self.parse("-C", str(self.workspace), "-m", "two", "--mode", "read_only")
        )

        self.assertEqual(settings.model, "model-two")
        self.assertEqual(settings.model_profile_id, "two")
        self.assertEqual(settings.workspace, self.workspace.resolve())
        self.assertEqual(settings.approval_mode, ApprovalMode.READ_ONLY)

    def test_scratch_and_workspace_conflict(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            console._settings_from_arguments(self.parse("--scratch", "-C", str(self.workspace)))
        self.assertIn("只能给一个", str(raised.exception))

    def test_print_mode_requires_a_prompt(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            console.main(["-p", "-C", str(self.workspace)])
        self.assertIn("-p 需要给一句 prompt", str(raised.exception))

    def test_print_mode_calls_run_once(self) -> None:
        with mock.patch("pocketagent.cli.console.run_once", return_value=0) as run_once:
            with self.assertRaises(SystemExit) as raised:
                console.main(["-p", "-C", str(self.workspace), "-m", "one", "hello", "there"])

        self.assertEqual(raised.exception.code, 0)
        settings, session, prompt, show_reasoning = run_once.call_args.args
        self.assertEqual(settings.model, "model-one")
        self.assertIsNone(session)
        self.assertEqual(prompt, "hello there")
        self.assertFalse(show_reasoning)

    def test_sessions_lists_the_current_workspace(self) -> None:
        settings = console._settings_from_arguments(self.parse("-C", str(self.workspace)))
        session = SessionStore(settings.sessions_dir).new_session(self.workspace)
        SessionStore(settings.sessions_dir).save(session)

        output = io.StringIO()
        with redirect_stdout(output):
            console.main(["--sessions", "-C", str(self.workspace)])

        self.assertIn(session.id, output.getvalue())

    def test_web_and_tui_are_exclusive(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            console.main(["--web", "--tui", "-C", str(self.workspace)])
        self.assertIn("只能选一个", str(raised.exception))

    def test_tui_and_print_are_exclusive(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            console.main(["--tui", "-p", "hi", "-C", str(self.workspace)])
        self.assertIn("TUI 是持续交互界面", str(raised.exception))

    def test_scratch_directory_is_created_only_right_before_running(self) -> None:
        with mock.patch("pocketagent.cli.console.run_once", return_value=0) as run_once:
            with self.assertRaises(SystemExit):
                console.main(["--scratch", "-p", "hi"])
        scratch_root = self.home / "scratch"
        created = [path for path in scratch_root.iterdir() if path.is_dir()]
        self.assertEqual(len(created), 1)
        self.assertEqual(run_once.call_args.args[0].workspace, created[0])

        shutil.rmtree(scratch_root)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            console.main(["--scratch", "--sessions"])
        self.assertFalse(scratch_root.exists(), "--sessions 不该建临时工作区")
