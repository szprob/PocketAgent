from pocketagent.tools.base import (
    PathOutsideWorkspaceError,
    Tool,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolResult,
)
from pocketagent.tools.files import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from pocketagent.tools.search import GlobTool, GrepTool
from pocketagent.tools.shell import BashTool


def default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ReadFileTool(),
            ListDirTool(),
            GlobTool(),
            GrepTool(),
            EditFileTool(),
            WriteFileTool(),
            BashTool(),
        ]
    )


__all__ = [
    "PathOutsideWorkspaceError",
    "Tool",
    "ToolContext",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "default_tool_registry",
]
