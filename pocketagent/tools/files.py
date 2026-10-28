"""文件读写工具：read_file / write_file / edit_file / list_dir。"""

import asyncio
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocketagent.tools.base import Tool, ToolAccess, ToolContext, ToolKind, ToolResult
from pocketagent.tools.edit_report import numbered_snippets, summarize_changes, syntax_hint, syntax_problem

READ_DEFAULT_LINE_LIMIT = 2000
READ_MAX_LINE_CHARS = 2000
# 一次最多返回这么多字，再多就提示用 offset 接着读
READ_MAX_TOTAL_CHARS = 120_000
BINARY_SNIFF_BYTES = 8192
LIST_DIR_MAX_ENTRIES = 500
SKIPPED_DIRECTORY_NAMES = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache"}


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取文本文件，返回带行号的内容。大文件用 offset / limit 分段读。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对路径按工作目录解析"},
            "offset": {"type": "integer", "description": "从第几行开始读，从 1 开始，默认 1"},
            "limit": {"type": "integer", "description": f"最多读多少行，默认 {READ_DEFAULT_LINE_LIMIT}"},
        },
        "required": ["path"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("path", ""))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return path_argument_access(arguments, context, write=False)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(arguments["path"])
        if not path.is_file():
            return ToolResult(f"文件不存在：{arguments['path']}", is_error=True)
        offset = max(int(arguments.get("offset") or 1), 1)
        limit = max(int(arguments.get("limit") or READ_DEFAULT_LINE_LIMIT), 1)
        # 放到线程里读，大文件也不会卡住事件循环（别的任务的流式输出照常走）
        return await asyncio.to_thread(read_numbered_lines, path, offset, limit)


def path_argument_access(
    arguments: dict[str, Any], context: ToolContext, write: bool, default: str | None = None
) -> ToolAccess:
    """按 path 参数算这次调用碰到的路径；没给 path 时用 default（一般是工作目录）。"""
    raw_path = arguments.get("path", default)
    if not raw_path:
        return ToolAccess()
    return context.path_access([context.resolve_path(str(raw_path))], write=write)


def _looks_binary(path: Path) -> bool:
    with path.open("rb") as file:
        return b"\x00" in file.read(BINARY_SNIFF_BYTES)


def read_numbered_lines(path: Path, offset: int, limit: int) -> ToolResult:
    """逐行读，只留 [offset, offset+limit) 这几行；剩下的行只数不存。超长的行截断，总字数也有上限。"""
    if _looks_binary(path):
        return ToolResult(f"这是二进制文件（{path.stat().st_size} 字节），不能按文本读", is_error=True)
    numbered: list[str] = []
    collected_chars = 0
    stopped_at = None
    total_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file, start=1):
            total_lines = line_number
            if line_number < offset or line_number >= offset + limit or stopped_at is not None:
                continue
            text = line.rstrip("\n")
            if len(text) > READ_MAX_LINE_CHARS:
                text = f"{text[:READ_MAX_LINE_CHARS]}…（这一行有 {len(text)} 字，后面截掉了）"
            numbered.append(f"{line_number:6d}\t{text}")
            collected_chars += len(text)
            if collected_chars > READ_MAX_TOTAL_CHARS:
                stopped_at = line_number
    if not numbered:
        return ToolResult(f"（文件共 {total_lines} 行，第 {offset} 行之后没有内容）")
    last_shown = stopped_at or min(offset + limit - 1, total_lines)
    if last_shown < total_lines:
        numbered.append(f"（共 {total_lines} 行，显示到第 {last_shown} 行；用 offset={last_shown + 1} 接着读）")
    return ToolResult("\n".join(numbered))


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "把完整内容写入文件，文件已存在则覆盖。改已有文件请优先用 edit_file。"
        "覆盖时结果里带改动处前后几行（带行号）；.py / .json / .toml 会顺手查语法。"
    )
    kind = ToolKind.EDIT
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整的文件内容"},
        },
        "required": ["path", "content"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("path", ""))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return path_argument_access(arguments, context, write=True)

    def preview(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = context.resolve_path(arguments["path"])
        new_text = arguments["content"]
        if not path.exists():
            return f"新建 {context.display_path(path)}（{len(new_text.splitlines())} 行）"
        old_text = path.read_text(encoding="utf-8", errors="replace")
        return _unified_diff(old_text, new_text, context.display_path(path))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(arguments["path"])
        new_text = arguments["content"]
        old_text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        display_path = context.display_path(path)
        if old_text is None:
            # 新建的文件内容模型刚写过，不用再贴一遍
            headline = f"已新建 {display_path}（{len(new_text.splitlines())} 行）"
            return ToolResult(_with_syntax_hint(headline, path, new_text, display_path))
        return ToolResult(_edit_receipt(f"已覆盖 {display_path}", old_text, new_text, path, display_path))


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "精确替换文件里的一段文本。old_string 必须和文件内容逐字一致（含缩进）且只出现一次；"
        "要替换所有出现的地方就把 replace_all 设为 true。"
        "结果里带改动处前后几行（改完后的内容，带行号）和替换了几处，不用再 read_file 确认；"
        ".py / .json / .toml 会顺手查语法。"
    )
    kind = ToolKind.EDIT
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换成的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有出现的地方，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("path", ""))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return path_argument_access(arguments, context, write=True)

    def preview(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = context.resolve_path(arguments["path"])
        old_text = path.read_text(encoding="utf-8", errors="replace")
        edit = _apply_edit(old_text, arguments)
        if edit.error:
            return f"{context.display_path(path)}：{edit.error}"
        return _unified_diff(old_text, edit.text, context.display_path(path))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(arguments["path"])
        if not path.is_file():
            return ToolResult(f"文件不存在：{arguments['path']}", is_error=True)
        old_text = path.read_text(encoding="utf-8")
        edit = _apply_edit(old_text, arguments)
        if edit.error:
            return ToolResult(edit.error, is_error=True)
        path.write_text(edit.text, encoding="utf-8")
        display_path = context.display_path(path)
        headline = f"已修改 {display_path}（替换了 {edit.replacements} 处）"
        return ToolResult(_edit_receipt(headline, old_text, edit.text, path, display_path))


@dataclass
class EditOutcome:
    # 替换后的全文；出错时是原文
    text: str
    error: str | None = None
    # 实际替换了几处（replace_all 时可能不止一处）
    replacements: int = 0


def _apply_edit(old_text: str, arguments: dict[str, Any]) -> EditOutcome:
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    replace_all = bool(arguments.get("replace_all", False))
    if old_string == new_string:
        return EditOutcome(old_text, "old_string 和 new_string 相同，没有可改的")
    occurrences = old_text.count(old_string)
    if occurrences == 0:
        return EditOutcome(old_text, "文件里找不到 old_string，先 read_file 确认原文（注意缩进和空白）")
    if occurrences > 1 and not replace_all:
        return EditOutcome(old_text, f"old_string 出现了 {occurrences} 次，请带上更多上下文让它唯一，或设 replace_all=true")
    if replace_all:
        return EditOutcome(old_text.replace(old_string, new_string), replacements=occurrences)
    return EditOutcome(old_text.replace(old_string, new_string, 1), replacements=1)


def _edit_receipt(headline: str, old_text: str, new_text: str, path: Path, display_path: str) -> str:
    """改完的回执：加减了几行 + 改动处前后几行（改完后的内容，带行号）+ 语法提示。"""
    changes = summarize_changes(old_text, new_text)
    lines = [f"{headline}，+{changes.added_lines} / −{changes.removed_lines} 行"]
    snippets = numbered_snippets(new_text, changes.ranges)
    if snippets:
        lines.append("改动处（改完后的内容，带行号）：")
        lines.append(snippets)
    return _with_syntax_hint("\n".join(lines), path, new_text, display_path)


def _with_syntax_hint(text: str, path: Path, content: str, display_path: str) -> str:
    problem = syntax_problem(path, content)
    if problem is None:
        return text
    return f"{text}\n\n{syntax_hint(display_path, problem)}"


def _unified_diff(old_text: str, new_text: str, display_path: str) -> str:
    diff_lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{display_path}",
        tofile=f"b/{display_path}",
        lineterm="",
    )
    return "\n".join(diff_lines) or "（内容没有变化）"


class ListDirTool(Tool):
    name = "list_dir"
    description = "列出目录下的文件和子目录（子目录以 / 结尾），会跳过 .git、node_modules 等。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径，默认工作目录"}},
        "required": [],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("path", "."))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return path_argument_access(arguments, context, write=False, default=".")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        directory = context.resolve_path(arguments.get("path", "."))
        if not directory.is_dir():
            return ToolResult(f"目录不存在：{arguments.get('path', '.')}", is_error=True)
        entries = []
        for child in sorted(directory.iterdir()):
            if child.name in SKIPPED_DIRECTORY_NAMES:
                continue
            entries.append(child.name + "/" if child.is_dir() else child.name)
        if not entries:
            return ToolResult("（空目录）")
        if len(entries) > LIST_DIR_MAX_ENTRIES:
            hidden = len(entries) - LIST_DIR_MAX_ENTRIES
            entries = entries[:LIST_DIR_MAX_ENTRIES] + [f"（还有 {hidden} 项未显示）"]
        return ToolResult("\n".join(entries))
