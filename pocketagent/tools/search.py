"""搜索工具：glob 按文件名找，grep 按内容找。

优先用 ripgrep：它遵守 .gitignore、会跳过大目录，大仓库上快很多。
机器上没有 rg、或者正则用了 Rust 引擎不支持的写法（lookaround 之类）时，回退到原来的纯 Python 实现。
"""

import asyncio
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from pocketagent import ripgrep
from pocketagent.ripgrep import GrepMatch, RipgrepError, RipgrepTimeout
from pocketagent.tools.base import Tool, ToolAccess, ToolContext, ToolKind, ToolResult
from pocketagent.tools.files import SKIPPED_DIRECTORY_NAMES, path_argument_access

GLOB_MAX_RESULTS = 200
GREP_MAX_MATCHES = 200
GREP_MAX_FILE_BYTES = 2_000_000


def _is_skipped(path: Path, root: Path, context: ToolContext) -> bool:
    """跳过 .git、node_modules 这类目录；从工作目录外往下搜时（比如搜整个家目录），密钥目录也跳过。"""
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = path.parts
    if any(part in SKIPPED_DIRECTORY_NAMES for part in relative_parts):
        return True
    return not context.is_secret(root) and context.is_secret(path)


def _modified_time(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=128)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 转成按路径分隔符匹配的正则，语义和 pathlib.glob 对齐：* 不跨目录，** 跨目录。"""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if pattern.startswith("**", index):
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
                    parts.append("(?:.*/)?")
                else:
                    parts.append(".*")
                continue
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        elif character == "[":
            end = pattern.find("]", index + 1)
            if end < 0:
                parts.append(re.escape(character))
            else:
                content = pattern[index + 1 : end]
                if content.startswith("!"):
                    content = "^" + content[1:]
                parts.append(f"[{content}]")
                index = end
        else:
            parts.append(re.escape(character))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


def _relative_path_text(path: Path, root: Path) -> str:
    if path == root:
        return path.name
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name) if root.is_file() else path
    return relative.as_posix()


def _path_matches_glob(path: Path, root: Path, pattern: str, recursive: bool = False) -> bool:
    """recursive=True 按 rglob 的语义（grep 的 glob 参数），否则按 glob（glob 工具）。"""
    if recursive and not pattern.startswith("**/"):
        pattern = "**/" + pattern
    return bool(_glob_regex(pattern).match(_relative_path_text(path, root)))


class GlobTool(Tool):
    name = "glob"
    description = "按 glob 模式找文件，如 **/*.py、src/**/test_*.py；结果按修改时间从新到旧排，优先用 ripgrep（遵守 .gitignore）。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "在哪个目录下找，默认工作目录"},
        },
        "required": ["pattern"],
    }

    def __init__(self, ripgrep_path: str | None = None) -> None:
        self.ripgrep = ripgrep.find_ripgrep(ripgrep_path)

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("pattern", ""))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return path_argument_access(arguments, context, write=False, default=".")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = arguments["pattern"]
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return ToolResult("pattern 里不能有绝对路径或 ..，要搜别的目录请用 path 参数", is_error=True)
        root = context.resolve_path(arguments.get("path", "."))
        if self.ripgrep:
            try:
                return await self._glob_with_ripgrep(root, pattern, context)
            except RipgrepTimeout as error:
                return ToolResult(str(error), is_error=True)
            except RipgrepError:
                # rg 用不了就用 Python 实现，行为可能少一点（不读 .gitignore），但不会失败
                pass
        # 遍历目录是同步 IO，放到线程里，别卡住事件循环
        return await asyncio.to_thread(self._glob_python, root, pattern, context)

    async def _glob_with_ripgrep(self, root: Path, pattern: str, context: ToolContext) -> ToolResult:
        candidates = await ripgrep.list_files(self.ripgrep, root)
        matches = []
        for path in candidates:
            if _path_matches_glob(path, root, pattern) and path.is_file() and not _is_skipped(path, root, context):
                matches.append(path)
        return _glob_result(matches, context)

    def _glob_python(self, root: Path, pattern: str, context: ToolContext) -> ToolResult:
        matches = [
            path for path in root.glob(pattern) if path.is_file() and not _is_skipped(path, root, context)
        ]
        return _glob_result(matches, context)


def _glob_result(matches: list[Path], context: ToolContext) -> ToolResult:
    if not matches:
        return ToolResult("没有匹配的文件")
    matches.sort(key=_modified_time, reverse=True)
    lines = [context.display_path(path) for path in matches[:GLOB_MAX_RESULTS]]
    if len(matches) > GLOB_MAX_RESULTS:
        lines.append(f"（共 {len(matches)} 个，只显示前 {GLOB_MAX_RESULTS} 个）")
    return ToolResult("\n".join(lines))


class GrepTool(Tool):
    name = "grep"
    description = (
        "用正则表达式搜索文件内容，返回「文件:行号:内容」。优先用 ripgrep（遵守 .gitignore），"
        "支持大部分 Python 正则写法；可以用 glob 参数限定文件，如 *.py。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索的文件或目录，默认工作目录"},
            "glob": {"type": "string", "description": "只搜文件名匹配这个 glob 的文件，如 *.py"},
            "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
        },
        "required": ["pattern"],
    }

    def __init__(self, ripgrep_path: str | None = None) -> None:
        self.ripgrep = ripgrep.find_ripgrep(ripgrep_path)

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("pattern", ""))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return path_argument_access(arguments, context, write=False, default=".")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        root = context.resolve_path(arguments.get("path", "."))
        if self.ripgrep:
            file_glob = arguments.get("glob")

            def keep_path(path: Path) -> bool:
                if _is_skipped(path, root, context):
                    return False
                return _path_matches_glob(path, root, file_glob, recursive=True) if file_glob else True

            try:
                matches = await ripgrep.grep(
                    executable=self.ripgrep,
                    pattern=arguments["pattern"],
                    root=root,
                    ignore_case=bool(arguments.get("ignore_case")),
                    max_matches=GREP_MAX_MATCHES,
                    max_file_bytes=GREP_MAX_FILE_BYTES,
                    keep_path=keep_path,
                )
                return _grep_matches_result(matches, context)
            except RipgrepTimeout as error:
                return ToolResult(str(error), is_error=True)
            except RipgrepError:
                # Rust 正则不认 lookaround，或者 rg 半路出错：回退 Python，顺便给出正则错误
                pass
        return await asyncio.to_thread(self._grep_python, arguments, root, context)

    def _grep_python(self, arguments: dict[str, Any], root: Path, context: ToolContext) -> ToolResult:
        flags = re.IGNORECASE if arguments.get("ignore_case") else 0
        try:
            regex = re.compile(arguments["pattern"], flags)
        except re.error as error:
            return ToolResult(f"正则表达式有误：{error}", is_error=True)
        matches = self._collect_matches(root, regex, arguments.get("glob"), context)
        return _grep_matches_result(matches, context)

    def _collect_matches(
        self, root: Path, regex: re.Pattern[str], file_glob: str | None, context: ToolContext
    ) -> list[GrepMatch]:
        matches: list[GrepMatch] = []
        for file_path in _iter_candidate_files(root, file_glob, context):
            for line_number, line in _matching_lines(file_path, regex):
                matches.append(GrepMatch(path=file_path, line_number=line_number, line=line.rstrip()))
                if len(matches) >= GREP_MAX_MATCHES:
                    return matches
        return matches


def _grep_matches_result(matches: list[GrepMatch], context: ToolContext) -> ToolResult:
    if not matches:
        return ToolResult("没有匹配")
    lines = []
    for match in matches[:GREP_MAX_MATCHES]:
        lines.append(f"{context.display_path(match.path)}:{match.line_number}:{match.line}")
    if len(matches) >= GREP_MAX_MATCHES:
        lines.append(f"（已达 {GREP_MAX_MATCHES} 条上限，请缩小范围）")
    return ToolResult("\n".join(lines))


def _iter_candidate_files(root: Path, file_glob: str | None, context: ToolContext) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob(file_glob or "*")):
        if path.is_file() and not _is_skipped(path, root, context) and path.stat().st_size <= GREP_MAX_FILE_BYTES:
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
