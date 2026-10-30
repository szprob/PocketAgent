"""跨平台子进程辅助：找 bash、结束整棵进程树。"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pocketagent.safety.processes import bash_executable, terminate_process_tree
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
