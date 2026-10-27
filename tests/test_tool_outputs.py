"""工具输出超过上限时落盘，以及落盘目录的读取权限和清理。"""

import json
import os
import re
import stat
import sys
import time
import unittest
from pathlib import Path

from pocketagent.events import EventType
from pocketagent.llm import LLMResponse
from pocketagent.tool_outputs import save_tool_output
from pocketagent.tools.files import ReadFileTool
from tests.helpers import TemporaryDirectories, build_harness, tool_call


class ToolOutputStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.directory = self.directories.home_dir / "tool_outputs" / "session"

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_writes_full_text_with_private_permissions(self) -> None:
        text = "x" * 100

        path = save_tool_output(self.directory, "call_1", text)

        self.assertEqual(path, self.directory / "call_1.log")
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        if sys.platform != "win32":
            # Windows 只认只读位，0o600 这类权限位落不下去
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_unsafe_call_id_cannot_escape_the_directory(self) -> None:
        path = save_tool_output(self.directory, "../../etc/passwd", "x")

        self.assertIsNotNone(path)
        self.assertEqual(path.parent, self.directory)
        self.assertNotIn("..", path.name)
        self.assertNotIn("/", path.name)

    def test_old_outputs_are_pruned(self) -> None:
        import pocketagent.tool_outputs as tool_outputs

        self.addCleanup(setattr, tool_outputs, "_last_prune_at", tool_outputs._last_prune_at)
        tool_outputs._last_prune_at = 0.0
        old_path = self.directory / "old.log"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text("old", encoding="utf-8")
        old_timestamp = time.time() - 8 * 24 * 60 * 60
        os.utime(old_path, (old_timestamp, old_timestamp))

        save_tool_output(self.directory, "new", "new")

        self.assertFalse(old_path.exists())


class ToolOutputSpillTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_long_tool_output_is_saved_and_readable_without_approval(self) -> None:
        command = f"'{sys.executable}' -c \"print('x' * 5000)\""
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("call_big", "bash", json.dumps({"command": command}))]),
                LLMResponse(text="跑完了"),
            ],
            sandbox_enabled=False,
            max_tool_output_chars=300,
        )

        await harness.agent.run_turn("跑一下")

        result = harness.recorder.of_type(EventType.TOOL_RESULT)[0].data["content"]
        self.assertIn("已保存到", result)
        match = re.search(r"已保存到 (.+?\.log)", result)
        self.assertIsNotNone(match)
        path = Path(match.group(1))
        self.assertEqual(
            path,
            harness.settings.tool_outputs_root / harness.agent.session.id / "call_big.log",
        )
        self.assertIn("x" * 5000, path.read_text(encoding="utf-8"))
        self.assertLess(len(result), 1000)

        access = ReadFileTool().access({"path": str(path)}, harness.agent.context.tool_context)
        self.assertEqual(access.outside_paths, [])
        self.assertEqual(access.secret_paths, [])

        tool_message = next(item for item in harness.agent.context.messages if item["role"] == "tool")
        self.assertNotIn("x" * 5000, tool_message["content"])

    async def test_short_tool_output_is_not_spilled(self) -> None:
        harness = build_harness(
            self.directories,
            [
                LLMResponse(tool_calls=[tool_call("call_small", "bash", json.dumps({"command": "echo hi"}))]),
                LLMResponse(text="好"),
            ],
            sandbox_enabled=False,
        )

        await harness.agent.run_turn("跑一下")

        output_directory = harness.settings.tool_outputs_root / harness.agent.session.id
        self.assertEqual(list(output_directory.glob("*.log")), [])
