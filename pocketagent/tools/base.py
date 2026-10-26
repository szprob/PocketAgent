"""工具的基类、注册表和公共工具函数。

加一个新工具：继承 Tool，填好 name / description / kind / parameters，实现 run，再注册进 ToolRegistry。
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ToolKind(str, Enum):
    """决定权限策略：只读工具默认放行，写文件和执行命令按审批模式决定。"""

    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class PathOutsideWorkspaceError(ValueError):
    pass


class WriteObserver(Protocol):
    """改文件前要通知的对象（见 changes.ChangeTracker）。"""

    def remember_original(self, path: Path) -> None: ...


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass
class ToolContext:
    workspace: Path
    shell_timeout_seconds: int = 120
    # 只读工具额外能读的目录，例如这个会话的附件目录
    extra_read_dirs: list[Path] = field(default_factory=list)
    # 写文件前通知它记下原始内容，右侧栏的「变更」靠这个
    write_observer: WriteObserver | None = None

    def resolve_path(self, raw_path: str, for_write: bool = False) -> Path:
        """相对路径按工作目录解析。写只能写工作目录内；读还可以读 extra_read_dirs。"""
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        resolved = path.resolve()
        workspace = self.workspace.resolve()
        allowed_roots = [workspace]
        if not for_write:
            allowed_roots += [directory.resolve() for directory in self.extra_read_dirs]
        if not any(_is_within(resolved, root) for root in allowed_roots):
            raise PathOutsideWorkspaceError(f"路径不在工作目录 {workspace} 内：{raw_path}")
        return resolved

    def before_write(self, path: Path) -> None:
        if self.write_observer is not None:
            self.write_observer.remember_original(path)

    def display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace.resolve()))
        except ValueError:
            return str(path)


class Tool(ABC):
    name: str
    description: str
    kind: ToolKind
    # JSON Schema，原样发给模型
    parameters: dict[str, Any]

    @abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult: ...

    def describe(self, arguments: dict[str, Any]) -> str:
        """一行摘要，显示在界面上，例如 bash 显示命令本身。"""
        text = json.dumps(arguments, ensure_ascii=False)
        return text if len(text) <= 120 else text[:117] + "..."

    def preview(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """请求用户批准时展示的内容，改文件的工具可以覆盖成 diff。"""
        return self.describe(arguments)

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]


def find_missing_arguments(tool: Tool, arguments: dict[str, Any]) -> list[str]:
    required = tool.parameters.get("required", [])
    return [name for name in required if name not in arguments]


def truncate_middle(text: str, max_chars: int) -> str:
    """输出太长时保留头尾、截掉中间，报错信息通常在头尾。"""
    if len(text) <= max_chars:
        return text
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars
    return f"{text[:head_chars]}\n\n...（中间省略 {omitted} 个字符）...\n\n{text[-tail_chars:]}"
