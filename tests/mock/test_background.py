import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from pocketagent.safety.processes import bash_executable, process_exists
from pocketagent.tools import ToolContext
from pocketagent.tools import _background_supervisor as background_supervisor
from pocketagent.tools import background
from pocketagent.tools.background import BackgroundTaskManager
from pocketagent.tools.shell import BashOutputTool, BashTool, KillShellTool
from tests.helpers import TemporaryDirectories


async def wait_until_finished(manager: BackgroundTaskManager, shell_id: str, timeout_seconds: float) -> dict:
    for _ in range(int(timeout_seconds / 0.05)):
        record = manager.status(shell_id)
        if record["status"] not in {"starting", "running"}:
            return record
        await asyncio.sleep(0.05)
    return manager.status(shell_id)


class BackgroundShellTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.manager = BackgroundTaskManager(
            self.directories.home_dir / "background_tasks", self.directories.workspace, "session-1"
        )
        self.context = ToolContext(
            workspace=self.directories.workspace,
            shell_timeout_seconds=10,
            background_tasks=self.manager,
        )

    async def asyncTearDown(self) -> None:
        for record in self.manager.list():
            if record["status"] in {"starting", "running"}:
                await self.manager.stop(record["id"], grace_seconds=0.1)
        self.directories.cleanup()

    async def start(self, command: str) -> str:
        started = await BashTool().run({"command": command, "run_in_background": True}, self.context)
        self.assertFalse(started.is_error)
        return self.manager.list()[0]["id"]

    async def test_background_command_returns_immediately_and_can_be_read_and_killed(self) -> None:
        started_at = asyncio.get_running_loop().time()

        started = await BashTool().run(
            {"command": "echo started; sleep 30", "run_in_background": True}, self.context
        )

        self.assertLess(asyncio.get_running_loop().time() - started_at, 5)
        shell_id = self.manager.list()[0]["id"]
        self.assertIn(shell_id, started.content)

        output = await BashOutputTool().run({"id": shell_id, "wait_seconds": 3}, self.context)
        self.assertFalse(output.is_error)
        self.assertIn("started", output.content)
        self.assertIn("还在运行", output.content)

        killed = await KillShellTool().run({"id": shell_id}, self.context)
        self.assertIn("已结束", killed.content)

        after = await BashOutputTool().run({"id": shell_id}, self.context)
        self.assertIn("已退出", after.content)

    async def test_extra_environment_reaches_background_commands(self) -> None:
        """沙箱联网白名单的代理地址是额外注入的环境变量，后台命令也要拿得到。"""
        # 用和 bash 工具同一个 bash：Windows 上 PATH 里的 bash 可能是 WSL 的，拿 Windows 路径的工作目录跑不了
        record = await self.manager.start(
            "echo $POCKETAGENT_SANDBOX_TEST",
            [bash_executable(), "-c", "echo $POCKETAGENT_SANDBOX_TEST"],
            "",
            {"POCKETAGENT_SANDBOX_TEST": "proxied"},
        )
        finished = await wait_until_finished(self.manager, record["id"], 5)
        output = self.manager.read(record["id"])["output"]

        self.assertEqual(finished["exit_code"], 0)
        self.assertIn("proxied", output)

    async def test_finished_command_keeps_output_and_exit_code(self) -> None:
        shell_id = await self.start("echo done; exit 5")
        await wait_until_finished(self.manager, shell_id, 5)

        output = await BashOutputTool().run({"id": shell_id}, self.context)

        self.assertIn("done", output.content)
        self.assertIn("exit code 5", output.content)
        self.assertTrue(output.is_error)

    async def test_reading_is_incremental(self) -> None:
        shell_id = await self.start("echo first; sleep 0.3; echo second")

        first = await BashOutputTool().run({"id": shell_id, "wait_seconds": 2}, self.context)
        await wait_until_finished(self.manager, shell_id, 5)
        second = await BashOutputTool().run({"id": shell_id, "wait_seconds": 2}, self.context)

        self.assertIn("first", first.content)
        self.assertNotIn("second", first.content)
        self.assertIn("second", second.content)
        self.assertNotIn("first", second.content)

    async def test_listing_and_unknown_ids(self) -> None:
        empty = await BashOutputTool().run({}, self.context)
        self.assertIn("没有后台命令", empty.content)

        shell_id = await self.start("sleep 30")
        listing = await BashOutputTool().run({}, self.context)
        unknown = await BashOutputTool().run({"id": "bash_9"}, self.context)

        self.assertIn(shell_id, listing.content)
        self.assertIn("还在运行", listing.content)
        self.assertTrue(unknown.is_error)
        self.assertIn("bash_9", unknown.content)

    async def test_command_survives_backend_restart_and_new_manager_can_stop_it(self) -> None:
        shell_id = await self.start("printf 'persistent\\n'; sleep 30")
        for _ in range(50):
            if "persistent" in self.manager.read(shell_id)["output"]:
                break
            await asyncio.sleep(0.05)
        replacement = BackgroundTaskManager(self.manager.root, self.directories.workspace, "after-restart")

        visible = replacement.status(shell_id)
        read = replacement.read(shell_id)
        stopped = await replacement.stop(shell_id, grace_seconds=1)

        self.assertEqual(visible["status"], "running")
        self.assertIn("persistent", read["output"])
        self.assertEqual(stopped["status"], "stopped")

    @unittest.skipIf(sys.platform == "win32", "Windows 用任务管理器检查进程树")
    async def test_kill_ends_child_processes(self) -> None:
        shell_id = await self.start("sleep 60 & echo child:$!; wait")
        child_pid = None
        for _ in range(50):
            output = self.manager.read(shell_id)["output"]
            if "child:" in output:
                child_pid = int(output.split("child:", 1)[1].splitlines()[0])
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(child_pid)

        await self.manager.stop(shell_id, grace_seconds=0.2)

        for _ in range(50):
            if not process_exists(child_pid):
                break
            await asyncio.sleep(0.05)
        self.assertFalse(process_exists(child_pid))


class MetadataSharingTest(unittest.TestCase):
    """监督进程写 meta.json、主进程同时读：Windows 上撞上对方开着文件会报 PermissionError，要重试而不是直接失败。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.path = self.directories.workspace / "meta.json"

    def tearDown(self) -> None:
        self.directories.cleanup()

    def flaky_replace(self, failures: int):
        original_replace = Path.replace
        calls = {"count": 0}

        def replace(source: Path, target: Path) -> Path:
            calls["count"] += 1
            if calls["count"] <= failures:
                raise PermissionError(13, "The process cannot access the file because it is being used by another process")
            return original_replace(source, target)

        return replace

    def test_replace_is_retried_on_windows(self) -> None:
        for module in (background, background_supervisor):
            with self.subTest(module=module.__name__):
                with (
                    mock.patch.object(sys, "platform", "win32"),
                    mock.patch.object(Path, "replace", self.flaky_replace(failures=2)),
                    mock.patch.object(module.time, "sleep"),
                ):
                    module._write_json(self.path, {"status": "running"})

                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"status": "running"})

    def test_permission_error_is_not_retried_elsewhere(self) -> None:
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(Path, "replace", self.flaky_replace(failures=1)),
        ):
            with self.assertRaises(PermissionError):
                background._write_json(self.path, {"status": "running"})


if __name__ == "__main__":
    unittest.main()
