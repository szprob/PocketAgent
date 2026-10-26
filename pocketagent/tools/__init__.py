from pocketagent.skills import Skill
from pocketagent.tools.base import (
    Tool,
    ToolAccess,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolResult,
)
from pocketagent.tools.ask import AskUserTool
from pocketagent.tools.files import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from pocketagent.tools.search import GlobTool, GrepTool
from pocketagent.tools.shell import BashTool
from pocketagent.tools.skill import ActivateSkillTool
from pocketagent.tools.subagent import SubagentTool
from pocketagent.tools.web import WebFetchTool, WebSearchSettings, WebSearchTool


def default_tool_registry(
    web_search: WebSearchSettings | None = None,
    web_fetch: bool = True,
    sandboxed: bool = False,
    skills: list[Skill] | None = None,
) -> ToolRegistry:
    """内置工具。配了联网搜索的密钥才有 web_search；web_fetch 不要密钥，默认带上；有技能才有 activate_skill。"""
    tools: list[Tool] = [
        ReadFileTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
        EditFileTool(),
        WriteFileTool(),
        BashTool(sandboxed=sandboxed),
        AskUserTool(),
        SubagentTool(),
    ]
    if web_search is not None:
        tools.append(WebSearchTool(web_search))
    if web_fetch:
        tools.append(WebFetchTool())
    if skills:
        tools.append(ActivateSkillTool(skills))
    return ToolRegistry(tools)


__all__ = [
    "Tool",
    "ToolAccess",
    "ToolContext",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "WebSearchSettings",
    "default_tool_registry",
]
