import time
import unittest
from pathlib import Path

from pocketagent.tools import ToolContext
from pocketagent.tools.base import truncate_middle
from pocketagent.tools.files import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from pocketagent.tools.search import GlobTool, GrepTool
from pocketagent.tools.shell import BashTool
from tests.helpers import TemporaryDirectories


class ToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.context = ToolContext(workspace=self.workspace, shell_timeout_seconds=10)

    def tearDown(self) -> None:
        self.directories.cleanup()

    def write(self, relative_path: str, content: str) -> None:
        path = self.workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    async def test_read_file_numbers_lines_and_pages(self) -> None:
        self.write("a.txt", "one\ntwo\nthree\n")

        result = await ReadFileTool().run({"path": "a.txt", "offset": 2, "limit": 1}, self.context)

        self.assertEqual(result.content.splitlines()[0], "     2\ttwo")
        self.assertIn("用 offset=3 接着读", result.content)

    async def test_write_file_creates_parent_directories(self) -> None:
        result = await WriteFileTool().run({"path": "deep/dir/b.txt", "content": "hello"}, self.context)

        self.assertFalse(result.is_error)
        self.assertEqual((self.workspace / "deep/dir/b.txt").read_text(encoding="utf-8"), "hello")

    async def test_edit_file_requires_unique_match(self) -> None:
        self.write("c.py", "x = 1\nx = 1\n")
        edit_tool = EditFileTool()

        ambiguous = await edit_tool.run({"path": "c.py", "old_string": "x = 1", "new_string": "x = 2"}, self.context)
        missing = await edit_tool.run({"path": "c.py", "old_string": "y = 1", "new_string": "y = 2"}, self.context)
        replaced = await edit_tool.run(
            {"path": "c.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, self.context
        )

        self.assertTrue(ambiguous.is_error)
        self.assertIn("出现了 2 次", ambiguous.content)
        self.assertTrue(missing.is_error)
        self.assertFalse(replaced.is_error)
        self.assertEqual((self.workspace / "c.py").read_text(encoding="utf-8"), "x = 2\nx = 2\n")

    def test_edit_preview_is_a_diff(self) -> None:
        self.write("d.py", "print('old')\n")

        preview = EditFileTool().preview(
            {"path": "d.py", "old_string": "old", "new_string": "new"}, self.context
        )

        self.assertIn("-print('old')", preview)
        self.assertIn("+print('new')", preview)

    def test_paths_outside_workspace_resolve_but_are_reported(self) -> None:
        outside = self.context.resolve_path("../outside.txt")
        inside = self.context.resolve_path("src/a.py")

        self.assertEqual(outside, (self.workspace / "../outside.txt").resolve())
        self.assertEqual(ReadFileTool().access({"path": "../outside.txt"}, self.context).outside_paths, [outside])
        self.assertEqual(ReadFileTool().access({"path": "src/a.py"}, self.context).outside_paths, [])
        self.assertEqual(WriteFileTool().access({"path": "/etc/hosts", "content": ""}, self.context).outside_paths, [Path("/etc/hosts").resolve()])
        self.assertTrue(str(inside).startswith(str(self.workspace)))

    async def test_list_dir_marks_directories_and_skips_git(self) -> None:
        self.write("src/main.py", "")
        self.write(".git/config", "")

        result = await ListDirTool().run({}, self.context)

        self.assertEqual(result.content.splitlines(), ["src/"])

    async def test_glob_and_grep(self) -> None:
        self.write("src/app.py", "def handler():\n    return 42\n")
        self.write("src/util.txt", "handler mentioned here\n")
        self.write(".git/objects/x.py", "def handler(): pass\n")

        glob_result = await GlobTool().run({"pattern": "**/*.py"}, self.context)
        grep_result = await GrepTool().run({"pattern": r"def \w+", "glob": "*.py"}, self.context)

        self.assertEqual(glob_result.content, "src/app.py")
        self.assertEqual(grep_result.content, "src/app.py:1:def handler():")

    async def test_bash_reports_exit_code(self) -> None:
        success = await BashTool().run({"command": "echo hi"}, self.context)
        failure = await BashTool().run({"command": "echo boom >&2; exit 3"}, self.context)

        self.assertEqual(success.content, "hi\n\n[exit code 0]")
        self.assertFalse(success.is_error)
        self.assertIn("boom", failure.content)
        self.assertIn("[exit code 3]", failure.content)
        self.assertTrue(failure.is_error)

    async def test_bash_timeout_kills_the_process(self) -> None:
        started_at = time.monotonic()

        result = await BashTool().run({"command": "sleep 30", "timeout_seconds": 1}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("超过 1 秒", result.content)
        self.assertLess(time.monotonic() - started_at, 5)

    def test_truncate_middle_keeps_head_and_tail(self) -> None:
        text = "HEAD" + "x" * 1000 + "TAIL"

        truncated = truncate_middle(text, 100)

        self.assertTrue(truncated.startswith("HEAD"))
        self.assertTrue(truncated.endswith("TAIL"))
        self.assertIn("中间省略", truncated)


if __name__ == "__main__":
    unittest.main()
