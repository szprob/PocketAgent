"""搜索工具的 ripgrep 路径，以及 rg 用不了时的 Python 回退。"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pocketagent.ripgrep import RipgrepTimeout, find_ripgrep, parse_match_line
from pocketagent.tools import ToolContext
from pocketagent.tools.search import GlobTool, GrepTool
from tests.helpers import TemporaryDirectories

HAS_RIPGREP = find_ripgrep() is not None
# 只要是个存在的可执行文件就行（rg 的调用已经被 patch 掉，不会真跑）；
# 别写 /bin/true，macOS 上只有 /usr/bin/true，会被当成「没有 rg」走回退
STAND_IN_EXECUTABLE = sys.executable


class RipgrepParsingTest(unittest.TestCase):
    def test_parse_match_event(self) -> None:
        line = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "src/a.py"},
                    "line_number": 12,
                    "lines": {"text": "handler()\n"},
                },
            }
        ).encode("utf-8")

        match = parse_match_line(line)

        self.assertIsNotNone(match)
        self.assertEqual(match.path, Path("src/a.py"))
        self.assertEqual(match.line_number, 12)
        self.assertEqual(match.line, "handler()")

    def test_non_match_events_are_ignored(self) -> None:
        self.assertIsNone(parse_match_line(b'{"type": "summary", "data": {}}'))
        self.assertIsNone(parse_match_line(b"not json"))

    def test_base64_path_and_content(self) -> None:
        line = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"bytes": "c3JjL2EucHk="},
                    "line_number": 1,
                    "lines": {"bytes": "5Lit5paH" },
                },
            }
        ).encode("utf-8")

        match = parse_match_line(line)

        self.assertIsNotNone(match)
        self.assertEqual(match.path, Path("src/a.py"))
        self.assertEqual(match.line, "中文")


@unittest.skipUnless(HAS_RIPGREP, "这台机器上没有 ripgrep")
class RipgrepSearchTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_grep_respects_gitignore(self) -> None:
        self.write(".gitignore", "ignored.txt\n")
        self.write("ignored.txt", "needle\n")
        self.write("kept.txt", "needle\n")

        result = await GrepTool().run({"pattern": "needle"}, self.context)

        self.assertIn("kept.txt:1:needle", result.content)
        self.assertNotIn("ignored.txt", result.content)

    async def test_glob_respects_gitignore(self) -> None:
        self.write(".gitignore", "generated.py\n")
        self.write("generated.py", "")
        self.write("keep.py", "")

        result = await GlobTool().run({"pattern": "**/*.py"}, self.context)

        self.assertIn("keep.py", result.content)
        self.assertNotIn("generated.py", result.content)

    async def test_grep_ignore_case(self) -> None:
        self.write("a.txt", "Hello World\n")

        result = await GrepTool().run({"pattern": "hello", "ignore_case": True}, self.context)

        self.assertIn("a.txt:1:Hello World", result.content)

    async def test_glob_star_does_not_cross_directories(self) -> None:
        self.write("a.py", "")
        self.write("src/b.py", "")
        self.write("src/deep/c.py", "")

        top_level = await GlobTool().run({"pattern": "*.py"}, self.context)
        one_level = await GlobTool().run({"pattern": "src/*.py"}, self.context)

        self.assertEqual(top_level.content, "a.py")
        self.assertEqual(one_level.content, "src/b.py")

    async def test_grep_glob_is_recursive(self) -> None:
        self.write("src/app.py", "needle\n")
        self.write("notes.txt", "needle\n")

        result = await GrepTool().run({"pattern": "needle", "glob": "*.py"}, self.context)

        self.assertIn("src/app.py:1:needle", result.content)
        self.assertNotIn("notes.txt", result.content)


class RipgrepFallbackTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_python_fallback_when_ripgrep_is_missing(self) -> None:
        self.write("a.txt", "needle\n")
        with patch("pocketagent.tools.search.ripgrep.find_ripgrep", return_value=None):
            tool = GrepTool()

        result = await tool.run({"pattern": "needle"}, self.context)

        self.assertIsNone(tool.ripgrep)
        self.assertIn("a.txt:1:needle", result.content)

    @unittest.skipUnless(HAS_RIPGREP, "这台机器上没有 ripgrep")
    async def test_lookahead_falls_back_to_python_regex(self) -> None:
        self.write("a.py", "value = handler ()\n")

        result = await GrepTool().run({"pattern": r"handler(?= )"}, self.context)

        self.assertIn("a.py:1:value = handler ()", result.content)

    async def test_glob_falls_back_when_ripgrep_is_missing(self) -> None:
        self.write("a.py", "")
        with patch("pocketagent.tools.search.ripgrep.find_ripgrep", return_value=None):
            tool = GlobTool()

        result = await tool.run({"pattern": "**/*.py"}, self.context)

        self.assertEqual(result.content, "a.py")

    async def test_grep_timeout_is_reported_without_python_fallback(self) -> None:
        with patch("pocketagent.tools.search.ripgrep.grep", side_effect=RipgrepTimeout("搜索超过 60 秒")):
            tool = GrepTool(ripgrep_path=STAND_IN_EXECUTABLE)

            result = await tool.run({"pattern": "needle"}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("搜索超过 60 秒", result.content)

    async def test_glob_timeout_is_reported_without_python_fallback(self) -> None:
        with patch("pocketagent.tools.search.ripgrep.list_files", side_effect=RipgrepTimeout("列文件超过 30 秒")):
            tool = GlobTool(ripgrep_path=STAND_IN_EXECUTABLE)

            result = await tool.run({"pattern": "**/*.py"}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("列文件超过 30 秒", result.content)


if __name__ == "__main__":
    unittest.main()
