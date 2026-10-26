"""工具调用的权限审批。

审批模式（只读工具任何模式下都直接放行）：
- ask（每次询问）：改文件和执行命令都要用户确认（默认）
- ai_auto（AI 自动审批）：先让模型判断这次调用是否安全、是否在任务范围内，判定安全就放行，否则照常问用户
- full_auto（完全访问）：全部放行
- read_only：只允许只读工具，其余一律拒绝（先看不改，做计划用）
- auto_edit：改文件放行，执行命令要确认
web 界面只给前三种，后两种命令行 --mode 可用。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from pocketagent.tools.base import Tool, ToolKind


class ApprovalMode(str, Enum):
    READ_ONLY = "read_only"
    ASK = "ask"
    AUTO_EDIT = "auto_edit"
    AI_AUTO = "ai_auto"
    FULL_AUTO = "full_auto"


# web 界面上可选的三种
WEB_APPROVAL_MODES = [ApprovalMode.ASK, ApprovalMode.AI_AUTO, ApprovalMode.FULL_AUTO]


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    # 先交给 AI 审查，不放心再问用户
    AI_REVIEW = "ai_review"


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
    if mode == ApprovalMode.AI_AUTO:
        return Decision.AI_REVIEW
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
        needs_someone = decision in (Decision.ASK, Decision.AI_REVIEW)
        if needs_someone and tool.name in self.always_allowed_tools:
            return Decision.ALLOW
        return decision

    def denial_reason(self, tool: Tool) -> str:
        return f"当前是 {self.mode.value} 模式，不允许调用 {tool.kind.value} 类工具 {tool.name}"

    async def ask(self, request: ApprovalRequest) -> bool:
        answer = await self.approver.request_approval(request)
        if answer == ApprovalAnswer.ALLOW_ALWAYS:
            self.always_allowed_tools.add(request.tool_name)
        return answer in (ApprovalAnswer.ALLOW_ONCE, ApprovalAnswer.ALLOW_ALWAYS)
