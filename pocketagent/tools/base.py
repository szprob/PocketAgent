"""工具的基类、注册表和公共工具函数。

加一个新工具：继承 Tool，填好 name / description / kind / parameters，实现 run，再注册进 ToolRegistry。
"""

import json
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.safety.sandbox import SandboxPolicy, default_secret_dirs, is_within


class ToolKind(str, Enum):
    """决定权限策略：只读工具默认放行，写文件和执行命令按审批模式决定。"""

    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # 要让模型直接看到的图片（如截图）；进历史时展开成图片段，不支持看图的模型退回纯文本
    images: list[Path] = field(default_factory=list)


# 正在执行的那次工具调用的 id。并发执行的工具各有一份（contextvars 跟着 asyncio 任务走），
# 子 agent 用它把自己的进度挂到主 agent 对应的那张工具卡片上
current_tool_call_id: ContextVar[str] = ContextVar("current_tool_call_id", default="")
# 工具往界面上流式吐输出用的回调；并发工具各有一份，和上面的 id 一样跟着 asyncio 任务走
ToolOutputListener = Callable[[str], Awaitable[None]]
current_tool_output: ContextVar[ToolOutputListener | None] = ContextVar("current_tool_output", default=None)


@dataclass
class ToolAccess:
    """一次调用要碰到的、超出常规的地方，权限审批按它决定要不要多问一句。"""

    # 工作目录（读的话还有附件、技能目录）以外的路径
    outside_paths: list[Path] = field(default_factory=list)
    # 密钥、凭据所在的路径，什么模式下都要用户确认
    secret_paths: list[Path] = field(default_factory=list)
    # bash 要求在沙箱外执行
    outside_sandbox: bool = False
    command: str = ""
    # 改的全是项目记忆目录里的文件：不用确认（只读模式除外）
    memory_only: bool = False


def resolve_trusted_dirs(directories: list[Path], secret_dirs: list[Path]) -> list[Path]:
    """免问目录先过一道密钥：和密钥目录有父子关系的整个丢掉。

    不然把家目录加进来，~/.ssh、~/.pocketagent 也跟着免问了。
    """
    secrets = [directory.resolve() for directory in secret_dirs]
    trusted: list[Path] = []
    for directory in directories:
        resolved = Path(directory).expanduser().resolve()
        if any(is_within(resolved, secret) or is_within(secret, resolved) for secret in secrets):
            continue
        if resolved not in trusted:
            trusted.append(resolved)
    return trusted


@dataclass
class ToolContext:
    workspace: Path
    shell_timeout_seconds: int = 120
    # 不用确认就能读的目录：这个会话的附件目录、技能目录
    extra_read_dirs: list[Path] = field(default_factory=list)
    # 用户配的免问目录：和工作目录一样对待，读不问、改照审批模式走（和密钥目录重叠的会被过滤掉）
    trusted_dirs: list[Path] = field(default_factory=list)
    # 设置里勾了「工作目录外不再询问」：除了密钥目录，所有路径都和工作目录一样对待
    allow_outside_workspace: bool = False
    # 密钥、凭据目录：工具碰到要确认，bash 在沙箱里读不到
    secret_dirs: list[Path] = field(default_factory=default_secret_dirs)
    # bash 的沙箱；None 表示不隔离（设置里关掉了）
    sandbox: SandboxPolicy | None = None
    # 项目记忆目录：读写都不用确认；None 表示记忆关掉了
    memory_dir: Path | None = None
    # 工具输出落盘的目录（这个任务的）；读写都不用确认，bash 沙箱里也能读
    tool_outputs_dir: Path | None = None
    # 取当前对话的消息（activate_skill 用来判断技能是不是已经加载过）
    history: Callable[[], list[dict[str, Any]]] = field(default=lambda: [])
    # 向用户提问（ask_user 用）；None 表示没人能回答（单次执行、测试）。返回 None 表示用户跳过
    ask_user: Callable[[list[Question]], Awaitable[list[QuestionAnswer] | None]] | None = None
    # 派子 agent（subagent 工具用）；子 agent 自己的工具上下文里是 None，不能再派
    spawn_subagent: Callable[[Any], Awaitable[Any]] | None = None
    # 任务计划清单（todo_write 用的），和会话落盘的是同一个 list，原地改
    todo_list: list[dict[str, Any]] = field(default_factory=list)
    # 工具之间传状态的共享字典（screenshot 记下换算比，computer 点击时用）；会话级，原地改
    tool_state: dict[str, Any] = field(default_factory=dict)
    # 清单变了通知界面（todo_write 用）；None 表示没人听（单次执行、测试）
    on_todos_changed: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        self.trusted_dirs = resolve_trusted_dirs(self.trusted_dirs, self.secret_dirs)

    def resolve_path(self, raw_path: str) -> Path:
        """相对路径按工作目录解析。工作目录外的路径也能用，要不要确认由权限审批决定（见 path_access）。"""
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def path_access(self, paths: list[Path], write: bool) -> ToolAccess:
        """哪些路径在工作目录外、哪些是密钥目录。

        免问目录和工作目录一样（不算工作目录外，改文件仍按审批模式走）；记忆目录读写都放行；
        附件、技能目录只对读放行；勾了「工作目录外不再询问」时，只有密钥目录还算。
        """
        free_roots = [self.workspace.resolve()] + self.trusted_dirs
        if not write:
            free_roots += [directory.resolve() for directory in self.extra_read_dirs]
        memory_root = self.memory_dir.resolve() if self.memory_dir is not None else None
        secret_roots = [directory.resolve() for directory in self.secret_dirs]
        access = ToolAccess(memory_only=bool(paths) and memory_root is not None)
        for path in paths:
            in_memory = memory_root is not None and is_within(path, memory_root)
            if not in_memory:
                access.memory_only = False
            if in_memory or any(is_within(path, root) for root in free_roots):
                continue
            is_secret = any(is_within(path, root) for root in secret_roots)
            if self.allow_outside_workspace and not is_secret:
                continue
            access.outside_paths.append(path)
            if is_secret:
                access.secret_paths.append(path)
        return access

    def is_secret(self, path: Path) -> bool:
        return any(is_within(path, directory.resolve()) for directory in self.secret_dirs)

    def display_path(self, path: Path) -> str:
        # 给模型和界面看的路径统一用 /：Windows 上 str(Path) 是反斜杠，和提示词里的例子不一致
        try:
            return path.relative_to(self.workspace.resolve()).as_posix()
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

    def kind_for(self, arguments: dict[str, Any]) -> ToolKind:
        """这次调用实际算哪一类，用来判断一批调用能不能并发跑（只读的才并发）。

        大多数工具固定是 kind；子 agent 这种看参数的覆盖它。审批仍按 kind 走。
        """
        return self.kind

    def files_written(self, arguments: dict[str, Any], context: "ToolContext") -> list[Path]:
        """这次调用会改哪些文件（它的预览也依赖这些文件的当前内容）。

        同一批里后一个调用要改前一个改过的文件时，得等前一个执行完再审（见 core/nodes.py split_into_stages）。
        说不准会改哪里的（bash 之类）留空，调度时按「可能改任何文件」处理。
        """
        return []

    def describe(self, arguments: dict[str, Any]) -> str:
        """一行摘要，显示在界面上，例如 bash 显示命令本身。"""
        text = json.dumps(arguments, ensure_ascii=False)
        return text if len(text) <= 120 else text[:117] + "..."

    def preview(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """请求用户批准时展示的内容，改文件的工具可以覆盖成 diff。"""
        return self.describe(arguments)

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        """这次调用要碰哪些超出常规的地方；按路径干活的工具覆盖它。"""
        return ToolAccess()

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
