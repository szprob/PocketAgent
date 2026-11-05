"""跨平台子进程辅助：找 bash、探活、结束整棵进程树。"""

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pocketagent.safety.processes import bash_executable, process_exists, terminate_process_tree
from pocketagent.safety.sandbox import BashUnavailable, wrap_command

HAS_BASH = bash_executable() is not None


@unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
class BashExecutableTest(unittest.TestCase):
    def test_wrap_command_uses_found_bash(self) -> None:
        prepared = wrap_command("echo hi", None)

        self.assertIn(Path(prepared.command_line[0]).name.lower(), ("bash", "bash.exe"))
        self.assertEqual(prepared.command_line[1:], ["-c", "echo hi"])
        self.assertEqual(prepared.note, "")
        self.assertEqual(prepared.environment, {})


class MissingBashTest(unittest.TestCase):
    def test_wrap_command_explains_how_to_install_bash(self) -> None:
        with patch("pocketagent.safety.sandbox.bash_executable", return_value=None):
            with self.assertRaises(BashUnavailable) as raised:
                wrap_command("echo hi", None)

        self.assertIn("Git for Windows", str(raised.exception))


class TerminateProcessTreeTest(unittest.IsolatedAsyncioTestCase):
    async def test_kills_running_process(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        async with asyncio.timeout(20):
            await terminate_process_tree(process)

        self.assertIsNotNone(process.returncode)


class ProcessExistsTest(unittest.TestCase):
    def test_probing_reports_running_process_without_killing_it(self) -> None:
        # Windows 上 os.kill(pid, 0) 会直接结束进程，探活不能用它
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)

        self.assertTrue(process_exists(process.pid))
        self.assertTrue(process_exists(process.pid))
        self.assertIsNone(process.poll())

    def test_finished_process_is_reported_gone(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()

        self.assertFalse(process_exists(process.pid))
        self.assertFalse(process_exists(0))
