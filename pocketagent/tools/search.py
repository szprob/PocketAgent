"""搜索工具：glob 按文件名找，grep 按内容找。

骨架阶段是纯 Python 实现；大仓库慢了再换成调 ripgrep。
"""

import re
from pathlib import Path
from typing import Any, Iterator

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult
from pocketagent.tools.files import SKIPPED_DIRECTORY_NAMES

GLOB_MAX_RESULTS = 200
GREP_MAX_MATCHES = 200
GREP_MAX_FILE_BYTES = 2_000_000


def _is_skipped(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return any(part in SKIPPED_DIRECTORY_NAMES for part in relative_parts)


class GlobTool(Tool):
    name = "glob"
    description = "按 glob 模式找文件，如 **/*.py、src/**/test_*.py；结果按修改时间从新到旧排。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "在哪个目录下找，默认工作目录"},
        },
        "required": ["pattern"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("pattern", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = context.resolve_path(arguments.get("path", "."))
        matches = [
            path for path in root.glob(arguments["pattern"]) if path.is_file() and not _is_skipped(path, root)
        ]
        if not matches:
            return ToolResult("没有匹配的文件")
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        lines = [context.display_path(path) for path in matches[:GLOB_MAX_RESULTS]]
        if len(matches) > GLOB_MAX_RESULTS:
            lines.append(f"（共 {len(matches)} 个，只显示前 {GLOB_MAX_RESULTS} 个）")
        return ToolResult("\n".join(lines))


class GrepTool(Tool):
    name = "grep"
    description = "用正则表达式搜索文件内容，返回「文件:行号:内容」。可以用 glob 参数限定文件，如 *.py。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "搜索的文件或目录，默认工作目录"},
            "glob": {"type": "string", "description": "只搜文件名匹配这个 glob 的文件，如 *.py"},
            "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
        },
        "required": ["pattern"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("pattern", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        flags = re.IGNORECASE if arguments.get("ignore_case") else 0
        try:
            regex = re.compile(arguments["pattern"], flags)
        except re.error as error:
            return ToolResult(f"正则表达式有误：{error}", is_error=True)
        root = context.resolve_path(arguments.get("path", "."))
        matches = []
        for file_path in _iter_candidate_files(root, arguments.get("glob")):
            for line_number, line in _matching_lines(file_path, regex):
                matches.append(f"{context.display_path(file_path)}:{line_number}:{line.rstrip()}")
                if len(matches) >= GREP_MAX_MATCHES:
                    matches.append(f"（已达 {GREP_MAX_MATCHES} 条上限，请缩小范围）")
                    return ToolResult("\n".join(matches))
        if not matches:
            return ToolResult("没有匹配")
        return ToolResult("\n".join(matches))


def _iter_candidate_files(root: Path, file_glob: str | None) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob(file_glob or "*")):
        if path.is_file() and not _is_skipped(path, root) and path.stat().st_size <= GREP_MAX_FILE_BYTES:
            yield path


def _matching_lines(file_path: Path, regex: re.Pattern[str]) -> Iterator[tuple[int, str]]:
    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # 二进制文件或读不了的文件直接跳过
        return
    for line_number, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            yield line_number, line
