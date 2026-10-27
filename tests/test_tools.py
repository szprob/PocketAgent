import datetime
import time
import unittest
from pathlib import Path

from pocketagent.tools import ToolContext
from pocketagent.tools.base import truncate_middle
from pocketagent.tools.files import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from pocketagent.tools.search import GlobTool, GrepTool
from pocketagent.tools.shell import BashTool
from pocketagent.tools.todo import TodoWriteTool, parse_todos, render_todos
from pocketagent.tools.web import bing_recency_filter, deepseek_query_with_recency, parse_bing_results
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

    async def test_todo_write_replaces_the_shared_list(self) -> None:
        tool = TodoWriteTool()
        changed = []

        async def on_changed(todos: list[dict]) -> None:
            changed.append(todos)

        self.context.todo_list = [{"content": "旧的", "status": "pending"}]
        self.context.on_todos_changed = on_changed

        result = await tool.run(
            {
                "todos": [
                    {"content": "调研口径", "status": "completed"},
                    {"content": "写 runbook", "status": "in_progress", "active_form": "写 runbook 中"},
                    {"content": "跑第一批", "status": "pending"},
                ]
            },
            self.context,
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.context.todo_list[0]["status"], "completed")
        self.assertEqual(self.context.todo_list[1]["active_form"], "写 runbook 中")
        self.assertEqual(len(changed), 1)
        self.assertIn("[x] 调研口径", result.content)
        self.assertIn("[~] 写 runbook", result.content)

    async def test_todo_write_rejects_bad_formats(self) -> None:
        tool = TodoWriteTool()
        bad_statuses = await tool.run({"todos": [{"content": "a", "status": "done"}]}, self.context)
        empty = await tool.run({"todos": []}, self.context)
        missing_content = await tool.run({"todos": [{"status": "pending"}]}, self.context)

        self.assertTrue(bad_statuses.is_error)
        self.assertIn("status", bad_statuses.content)
        self.assertTrue(empty.is_error)
        self.assertTrue(missing_content.is_error)
        self.assertEqual(self.context.todo_list, [])

    def test_todo_parse_defaults_and_limits(self) -> None:
        todos = parse_todos({"todos": [{"content": "只有一句话"}]})
        self.assertEqual(todos, [{"content": "只有一句话", "status": "pending"}])
        self.assertEqual(render_todos(todos), "[ ] 只有一句话")
        with self.assertRaises(ValueError):
            parse_todos({"todos": [{"content": "x" * 201, "status": "pending"}]})
        with self.assertRaises(ValueError):
            parse_todos({"todos": [{"content": str(i), "status": "pending"} for i in range(51)]})

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


class BingSearchParsingTest(unittest.TestCase):
    """必应结果页的解析：标题取 h2 里的链接（不是前面那个站点图标链接），站内链接不影响结果条数。"""

    HTML = """
    <ol id="b_results">
      <li class="b_algo">
        <div class="b_tpcn"><a class="tilk" href="https://python.org/">python.org</a></div>
        <h2><a href="https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9kb2NzLnB5dGhvbi5vcmcvMy9saWJyYXJ5L2h0dHB4Lmh0bWw&ntb=1">httpx 文档</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">httpx 是 Python 的 HTTP 客户端。</p>
          <ul><li>站内链接一</li><li>站内链接二</li></ul>
        </div>
      </li>
      <li class="b_algo">
        <h2><a href="https://example.com/a?x=1">第二条 &amp; 结果</a></h2>
        <div class="b_caption"><p>摘要二</p></div>
      </li>
    </ol>
    """

    def test_results_are_extracted_in_order(self) -> None:
        results = parse_bing_results(self.HTML)

        self.assertEqual([item["title"] for item in results], ["httpx 文档", "第二条 & 结果"])
        self.assertEqual(results[0]["content"], "httpx 是 Python 的 HTTP 客户端。")
        self.assertEqual(results[1]["content"], "摘要二")

    def test_wrapped_bing_link_is_unwrapped(self) -> None:
        results = parse_bing_results(self.HTML)

        self.assertEqual(results[0]["link"], "https://docs.python.org/3/library/httpx.html")
        self.assertEqual(results[1]["link"], "https://example.com/a?x=1")

    def test_empty_page_gives_no_results(self) -> None:
        self.assertEqual(parse_bing_results("<html><body>没有结果</body></html>"), [])

    def test_recency_filter_uses_bing_codes(self) -> None:
        self.assertEqual(bing_recency_filter("oneDay"), 'ex1:"ez1"')
        self.assertEqual(bing_recency_filter("oneWeek"), 'ex1:"ez2"')
        self.assertEqual(bing_recency_filter("oneMonth"), 'ex1:"ez3"')
        self.assertIsNone(bing_recency_filter("noLimit"))
        # 一年没有现成的档位，用自定义区间
        self.assertTrue(bing_recency_filter("oneYear").startswith('ex1:"ez5_'))


class DeepSeekSearchTest(unittest.TestCase):
    """DeepSeek 搜索没有单独的时间参数，时间范围要拼进 query 的 after: 语法里。"""

    def test_recency_becomes_an_after_operator(self) -> None:
        today = datetime.date.today()
        week_ago = (today - datetime.timedelta(days=7)).isoformat()
        year_ago = (today - datetime.timedelta(days=365)).isoformat()

        self.assertEqual(deepseek_query_with_recency("deepseek 发布", "oneWeek"), f"deepseek 发布 after:{week_ago}")
        self.assertEqual(deepseek_query_with_recency("deepseek 发布", "oneYear"), f"deepseek 发布 after:{year_ago}")
        self.assertEqual(deepseek_query_with_recency("deepseek 发布", "noLimit"), "deepseek 发布")


if __name__ == "__main__":
    unittest.main()
