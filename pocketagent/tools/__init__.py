import sys

from pocketagent.knowledge.skills import Skill
from pocketagent.tools.ask import AskUserTool
from pocketagent.tools.base import (
    Tool,
    ToolAccess,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolResult,
)
from pocketagent.tools.computer import ComputerTool, ScreenshotTool
from pocketagent.tools.files import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from pocketagent.tools.image import (
    DownloadImageTool,
    GenerateImageTool,
    ImageGenSettings,
    SearchImagesTool,
)
from pocketagent.tools.search import GlobTool, GrepTool
from pocketagent.tools.shell import BashTool
from pocketagent.tools.skill import ActivateSkillTool
from pocketagent.tools.subagent import SubagentTool
from pocketagent.tools.todo import TodoWriteTool
from pocketagent.tools.web import WebFetchTool, WebSearchSettings, WebSearchTool


def default_tool_registry(
    web_search: WebSearchSettings | None = None,
    web_fetch: bool = True,
    sandboxed: bool = False,
    skills: list[Skill] | None = None,
    ripgrep_path: str | None = None,
    image_gen: ImageGenSettings | None = None,
    image_search: bool = True,
    computer_use: bool = False,
) -> ToolRegistry:
    """内置工具。配了联网搜索的密钥才有 web_search；web_fetch 不要密钥，默认带上；有技能才有 activate_skill。
    image_gen 配了密钥才有 generate_image；search_images 和 download_image 默认带上；
    screenshot / computer 能操作整台电脑，只在 macOS 上、而且用户在设置里勾了才有。"""
    tools: list[Tool] = [
        ReadFileTool(),
        ListDirTool(),
        GlobTool(ripgrep_path=ripgrep_path),
        GrepTool(ripgrep_path=ripgrep_path),
        EditFileTool(),
        WriteFileTool(),
        BashTool(sandboxed=sandboxed),
        AskUserTool(),
        TodoWriteTool(),
        SubagentTool(),
    ]
    if web_search is not None:
        tools.append(WebSearchTool(web_search))
    if web_fetch:
        tools.append(WebFetchTool())
    if image_gen is not None:
        tools.append(GenerateImageTool(image_gen))
    if image_search:
        tools.append(SearchImagesTool())
    if image_gen is not None or image_search:
        tools.append(DownloadImageTool())
    if skills:
        tools.append(ActivateSkillTool(skills))
    if computer_use and sys.platform == "darwin":
        # 截屏（screenshot）+ 键鼠操作（computer），需要「屏幕录制」「辅助功能」权限，见 tools/computer.py
        tools.append(ScreenshotTool())
        tools.append(ComputerTool())
    return ToolRegistry(tools)


__all__ = [
    "Tool",
    "ToolAccess",
    "ToolContext",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "WebSearchSettings",
    "ImageGenSettings",
    "default_tool_registry",
]
