"""影子 git 检查点：嵌套仓库、新任务继承 index、检查点和第一次调用模型同时进行。"""

import asyncio
import subprocess
import time
import unittest
from dataclasses import dataclass, field

from pocketagent.core.events import EventType
from pocketagent.llm import LLMResponse
from pocketagent.safety.checkpoints import Checkpoint, TaskCheckpoints, find_nested_repos
from tests.helpers import TemporaryDirectories, build_harness, tool_call


@dataclass
class FakeSession:
    id: str
    checkpoints: list = field(default_factory=list)


def git(directory, *arguments) -> None:
    subprocess.run(["git", *arguments], cwd=directory, check=True, capture_output=True)


class NestedRepoCheckpointTest(unittest.IsolatedAsyncioTestCase):
    """工作区是「一个目录下放好几个仓库」时，仓库里面的改动也要看得到、撤得掉。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.shadow_root = self.directories.home_dir / "shadow"
        (self.workspace / "notes.txt").write_text("top v1\n", encoding="utf-8")
        self.project = self.workspace / "proj"
        self.project.mkdir()
        (self.project / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (self.project / ".gitignore").write_text("data/\n", encoding="utf-8")
        git(self.project, "init", "-q")
        git(self.project, "add", ".")
        git(self.project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def checkpoints(self, session_id: str = "s1") -> TaskCheckpoints:
        return TaskCheckpoints(self.shadow_root, self.workspace, FakeSession(session_id))

    def edit_everything(self) -> None:
        (self.workspace / "notes.txt").write_text("top v2\n", encoding="utf-8")
        (self.project / "app.py").write_text("print('v2')\n", encoding="utf-8")
        (self.project / "new.txt").write_text("new\n", encoding="utf-8")
        (self.project / "data").mkdir()
        (self.project / "data" / "big.bin").write_text("ignored\n", encoding="utf-8")

    async def test_changes_inside_nested_repos_are_seen(self) -> None:
        checkpoints = self.checkpoints()
        baseline = await checkpoints.create("任务开始时", "baseline")
        self.edit_everything()

        changes = {change.path: change.status for change in await checkpoints.changes()}
        diff = await checkpoints.diff("proj/app.py")

        self.assertEqual(list(baseline.nested), ["proj"])
        self.assertEqual(changes, {"notes.txt": "modified", "proj/app.py": "modified", "proj/new.txt": "added"})
        self.assertIn("+print('v2')", diff)

    async def test_revert_and_restore_reach_into_nested_repos(self) -> None:
        checkpoints = self.checkpoints()
        baseline = await checkpoints.create("任务开始时", "baseline")
        self.edit_everything()

        await checkpoints.revert_file("proj/app.py")
        reverted = (self.project / "app.py").read_text(encoding="utf-8")
        restored = await checkpoints.restore(baseline.commit)

        self.assertEqual(reverted, "print('v1')\n")
        self.assertEqual(restored, ["notes.txt", "proj/new.txt"])
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "top v1\n")
        self.assertFalse((self.project / "new.txt").exists())
        # 被 .gitignore 忽略的东西不碰，嵌套仓库自己的 .git 也不碰
        self.assertTrue((self.project / "data" / "big.bin").exists())
        self.assertTrue((self.project / ".git").is_dir())
        self.assertEqual(await checkpoints.changes(), [])

    async def test_old_checkpoints_without_nested_info_do_not_flood_the_list(self) -> None:
        checkpoints = self.checkpoints()
        commit = await checkpoints.repo.snapshot("老检查点")
        checkpoints.session.checkpoints.append(
            {"commit": commit, "label": "任务开始时", "created_at": "2026-10-26T10:00:00", "kind": "baseline"}
        )
        self.edit_everything()

        changes = [change.path for change in await checkpoints.changes()]

        self.assertEqual(changes, ["notes.txt"])
        self.assertIsNone(checkpoints.baseline().nested)

    async def test_new_task_reuses_the_latest_index(self) -> None:
        await self.checkpoints("s1").create("任务开始时", "baseline")
        second = self.checkpoints("s2")
        self.assertFalse(second.repo.index_file.exists())

        await second.create("任务开始时", "baseline")

        self.assertTrue(second.repo.index_file.exists())
        self.assertEqual(await second.changes(), [])

    def test_find_nested_repos(self) -> None:
        (self.workspace / "node_modules" / "pkg").mkdir(parents=True)
        (self.workspace / "node_modules" / "pkg" / ".git").mkdir()
        worktree = self.workspace / "group" / "wt"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")

        self.assertEqual(find_nested_repos(self.workspace), ["group/wt", "proj"])

    def test_checkpoint_round_trip_keeps_nested(self) -> None:
        checkpoint = Checkpoint(commit="a", label="x", created_at="t", kind="turn", nested={"proj": "b"})

        self.assertEqual(Checkpoint(**checkpoint.to_dict()), checkpoint)


class SlowCheckpointTest(unittest.IsolatedAsyncioTestCase):
    """检查点和第一次调用模型同时进行，但工具一定在检查点拍完之后才动。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_model_call_does_not_wait_for_the_snapshot_but_tools_do(self) -> None:
        responses = [
            LLMResponse(tool_calls=[tool_call("c1", "write_file", '{"path": "a.txt", "content": "x"}')]),
            LLMResponse(text="好了"),
        ]
        harness = build_harness(self.directories, responses)
        marks: dict[str, float] = {}
        original_create = harness.agent.checkpoints.create
        original_complete = harness.provider.complete

        async def slow_create(label, kind):
            await asyncio.sleep(0.3)
            checkpoint = await original_create(label, kind)
            marks["checkpoint_done"] = time.monotonic()
            return checkpoint

        async def recording_complete(messages, tools, listener):
            marks.setdefault("model_called", time.monotonic())
            return await original_complete(messages, tools, listener)

        def record_tool_call(event):
            if event.type == EventType.TOOL_CALL:
                marks.setdefault("tool_called", time.monotonic())

        harness.agent.checkpoints.create = slow_create
        harness.provider.complete = recording_complete
        harness.agent.context.provider = harness.provider
        harness.agent.context.events.subscribe(record_tool_call)

        await harness.agent.run_turn("写个文件")

        self.assertLess(marks["model_called"], marks["checkpoint_done"])
        # Windows 的计时器精度约 15ms，两个事件可能记到同一个时刻，这里只要求不早于快照
        self.assertGreaterEqual(marks["tool_called"], marks["checkpoint_done"])
        self.assertEqual(len(harness.agent.checkpoints.all_checkpoints()), 1)
        self.assertEqual([change.path for change in await harness.agent.checkpoints.changes()], ["a.txt"])

    async def test_read_only_batches_do_not_wait_but_the_first_write_does(self) -> None:
        """检查点被卡住时，只读的批次照样执行；读完才放行检查点，之后的写一定排在检查点后面。"""
        (self.directories.workspace / "notes.txt").write_text("hi\n", encoding="utf-8")
        responses = [
            LLMResponse(tool_calls=[tool_call("c1", "read_file", '{"path": "notes.txt"}')]),
            LLMResponse(tool_calls=[tool_call("c2", "write_file", '{"path": "a.txt", "content": "x"}')]),
            LLMResponse(text="好了"),
        ]
        harness = build_harness(self.directories, responses)
        order: list[str] = []
        release_checkpoint = asyncio.Event()
        original_create = harness.agent.checkpoints.create

        async def blocked_create(label, kind):
            await release_checkpoint.wait()
            checkpoint = await original_create(label, kind)
            order.append("checkpoint")
            return checkpoint

        def record_results(event):
            if event.type != EventType.TOOL_RESULT:
                return
            order.append(event.data["name"])
            if event.data["name"] == "read_file":
                release_checkpoint.set()

        harness.agent.checkpoints.create = blocked_create
        harness.agent.context.events.subscribe(record_results)

        # 要是只读批次也等检查点，这里会互相等死，靠超时报错
        await asyncio.wait_for(harness.agent.run_turn("看看再写"), timeout=5)

        self.assertEqual(order, ["read_file", "checkpoint", "write_file"])
        self.assertEqual([change.path for change in await harness.agent.checkpoints.changes()], ["a.txt"])


if __name__ == "__main__":
    unittest.main()
