"""工具调用的权限审批。

审批模式对应 Codex 的 suggest / auto-edit / full-auto 和 Claude Code 的 plan 模式：
- read_only：只允许只读工具，其余一律拒绝（先看不改，做计划用）
- ask：只读工具放行，改文件和执行命令都要用户确认（默认）
- auto_edit：改文件放行，执行命令要确认
- full_auto：全部放行
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from pocketagent.tools.base import Tool, ToolKind


class ApprovalMode(str, Enum):
    READ_ONLY = "read_only"
    ASK = "ask"
    AUTO_EDIT = "auto_edit"
    FULL_AUTO = "full_auto"


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ApprovalAnswer(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"


@dataclass
class ApprovalRequest:
    tool_name: str
    summary: str
    preview: str


class Approver(Protocol):
    """向用户请求批准。CLI 用 input()，以后的 IDE / Web 前端各自实现。"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer: ...


class DenyAllApprover:
    """非交互场景（-p 单次执行、测试）用：需要确认的一律拒绝。"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer.DENY


class AllowAllApprover:
    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer.ALLOW_ONCE


def decide_by_mode(mode: ApprovalMode, kind: ToolKind) -> Decision:
    if kind == ToolKind.READ:
        return Decision.ALLOW
    if mode == ApprovalMode.FULL_AUTO:
        return Decision.ALLOW
    if mode == ApprovalMode.READ_ONLY:
        return Decision.DENY
    if mode == ApprovalMode.AUTO_EDIT and kind == ToolKind.EDIT:
        return Decision.ALLOW
    return Decision.ASK


@dataclass
class PermissionGate:
    """先 decide 看要不要问；要问时调用方再准备预览（diff 等）并 ask，免得放行的调用也去算 diff。"""

    mode: ApprovalMode
    approver: Approver
    # 用户选了「本会话总是允许」的工具名
    always_allowed_tools: set[str] = field(default_factory=set)

    def decide(self, tool: Tool) -> Decision:
        decision = decide_by_mode(self.mode, tool.kind)
        if decision == Decision.ASK and tool.name in self.always_allowed_tools:
            return Decision.ALLOW
        return decision

    def denial_reason(self, tool: Tool) -> str:
        return f"当前是 {self.mode.value} 模式，不允许调用 {tool.kind.value} 类工具 {tool.name}"

    async def ask(self, request: ApprovalRequest) -> bool:
        answer = await self.approver.request_approval(request)
        if answer == ApprovalAnswer.ALLOW_ALWAYS:
            self.always_allowed_tools.add(request.tool_name)
        return answer in (ApprovalAnswer.ALLOW_ONCE, ApprovalAnswer.ALLOW_ALWAYS)
