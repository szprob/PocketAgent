import asyncio
import time
import unittest

from pocketagent.tools import ToolContext
from pocketagent.tools.shell import BashOutputTool, BashTool, KillShellTool
from pocketagent.safety.processes import BackgroundShell
from tests.helpers import TemporaryDirectories


async def wait_until_finished(shell: BackgroundShell, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while shell.running and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


class BackgroundShellTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.context = ToolContext(workspace=self.directories.workspace, shell_timeout_seconds=10)

    async def asyncTearDown(self) -> None:
        await self.context.background.kill_all()
        self.directories.cleanup()

    async def test_background_command_returns_immediately_and_can_be_read_and_killed(self) -> None:
        started_at = time.monotonic()

        started = await BashTool().run(
            {"command": "echo started; sleep 30", "run_in_background": True}, self.context
        )

        self.assertLess(time.monotonic() - started_at, 5)
        self.assertIn("bash_1", started.content)
        self.assertFalse(started.is_error)

        output = await BashOutputTool().run({"id": "bash_1", "wait_seconds": 3}, self.context)
        self.assertFalse(output.is_error)
        self.assertIn("started", output.content)
        self.assertIn("还在运行", output.content)

        killed = await KillShellTool().run({"id": "bash_1"}, self.context)
        self.assertIn("已结束", killed.content)

        after = await BashOutputTool().run({"id": "bash_1"}, self.context)
        self.assertIn("已退出", after.content)

    async def test_finished_command_keeps_output_and_exit_code(self) -> None:
        await BashTool().run({"command": "echo done; exit 5", "run_in_background": True}, self.context)
        shell = self.context.background.get("bash_1")
        await wait_until_finished(shell, 5)

        output = await BashOutputTool().run({"id": "bash_1"}, self.context)

        self.assertIn("done", output.content)
        self.assertIn("exit code 5", output.content)
        self.assertTrue(output.is_error)

    async def test_reading_is_incremental(self) -> None:
        await BashTool().run({"command": "echo first; sleep 0.3; echo second", "run_in_background": True}, self.context)

        first = await BashOutputTool().run({"id": "bash_1", "wait_seconds": 2}, self.context)
        shell = self.context.background.get("bash_1")
        await wait_until_finished(shell, 5)
        second = await BashOutputTool().run({"id": "bash_1", "wait_seconds": 2}, self.context)

        self.assertIn("first", first.content)
        self.assertNotIn("second", first.content)
        self.assertIn("second", second.content)
        self.assertNotIn("first", second.content)

    async def test_listing_and_unknown_ids(self) -> None:
        empty = await BashOutputTool().run({}, self.context)
        self.assertIn("没有后台命令", empty.content)

        await BashTool().run({"command": "sleep 30", "run_in_background": True}, self.context)
        listing = await BashOutputTool().run({}, self.context)
        unknown = await BashOutputTool().run({"id": "bash_9"}, self.context)

        self.assertIn("bash_1", listing.content)
        self.assertIn("还在运行", listing.content)
        self.assertTrue(unknown.is_error)
        self.assertIn("bash_9", unknown.content)

    async def test_kill_all_finishes_everything(self) -> None:
        await BashTool().run({"command": "sleep 30", "run_in_background": True}, self.context)
        await BashTool().run({"command": "sleep 30", "run_in_background": True}, self.context)
        shell = self.context.background.get("bash_1")

        await self.context.background.kill_all()

        self.assertTrue(shell.finished)
        self.assertIsNotNone(shell.exit_code)
        self.assertEqual(self.context.background.list_all(), [])


if __name__ == "__main__":
    unittest.main()
