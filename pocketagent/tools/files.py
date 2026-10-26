"""文件读写工具：read_file / write_file / edit_file / list_dir。"""

import difflib
from typing import Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

READ_DEFAULT_LINE_LIMIT = 2000
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

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(arguments["path"])
        if not path.is_file():
            return ToolResult(f"文件不存在：{arguments['path']}", is_error=True)
        offset = max(int(arguments.get("offset", 1)), 1)
        limit = max(int(arguments.get("limit", READ_DEFAULT_LINE_LIMIT)), 1)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        numbered = [f"{offset + index:6d}\t{line}" for index, line in enumerate(selected)]
        if not numbered:
            return ToolResult(f"（文件共 {len(lines)} 行，第 {offset} 行之后没有内容）")
        remaining = len(lines) - (offset - 1 + len(selected))
        if remaining > 0:
            numbered.append(f"（还有 {remaining} 行未显示，用 offset 继续读）")
        return ToolResult("\n".join(numbered))


class WriteFileTool(Tool):
    name = "write_file"
    description = "把完整内容写入文件，文件已存在则覆盖。改已有文件请优先用 edit_file。"
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

    def preview(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = context.resolve_path(arguments["path"], for_write=True)
        new_text = arguments["content"]
        if not path.exists():
            return f"新建 {context.display_path(path)}（{len(new_text.splitlines())} 行）"
        old_text = path.read_text(encoding="utf-8", errors="replace")
        return _unified_diff(old_text, new_text, context.display_path(path))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(arguments["path"], for_write=True)
        existed = path.exists()
        context.before_write(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments["content"], encoding="utf-8")
        action = "覆盖" if existed else "新建"
        return ToolResult(f"已{action} {context.display_path(path)}")


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "精确替换文件里的一段文本。old_string 必须和文件内容逐字一致（含缩进）且只出现一次；"
        "要替换所有出现的地方就把 replace_all 设为 true。"
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

    def preview(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = context.resolve_path(arguments["path"], for_write=True)
        old_text = path.read_text(encoding="utf-8", errors="replace")
        new_text, error = _apply_edit(old_text, arguments)
        if error:
            return f"{context.display_path(path)}：{error}"
        return _unified_diff(old_text, new_text, context.display_path(path))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(arguments["path"], for_write=True)
        if not path.is_file():
            return ToolResult(f"文件不存在：{arguments['path']}", is_error=True)
        old_text = path.read_text(encoding="utf-8")
        new_text, error = _apply_edit(old_text, arguments)
        if error:
            return ToolResult(error, is_error=True)
        context.before_write(path)
        path.write_text(new_text, encoding="utf-8")
        return ToolResult(f"已修改 {context.display_path(path)}")


def _apply_edit(old_text: str, arguments: dict[str, Any]) -> tuple[str, str | None]:
    """返回 (替换后的全文, 错误信息)；出错时全文原样返回。"""
    old_string = arguments["old_string"]
    new_string = arguments["new_string"]
    replace_all = bool(arguments.get("replace_all", False))
    if old_string == new_string:
        return old_text, "old_string 和 new_string 相同，没有可改的"
    occurrences = old_text.count(old_string)
    if occurrences == 0:
        return old_text, "文件里找不到 old_string，先 read_file 确认原文（注意缩进和空白）"
    if occurrences > 1 and not replace_all:
        return old_text, f"old_string 出现了 {occurrences} 次，请带上更多上下文让它唯一，或设 replace_all=true"
    if replace_all:
        return old_text.replace(old_string, new_string), None
    return old_text.replace(old_string, new_string, 1), None


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
