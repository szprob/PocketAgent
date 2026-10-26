"""工具调用的权限审批。

审批模式（只读工具任何模式下都直接放行）：
- ask（每次询问）：改文件和执行命令都要用户确认（默认）
- ai_auto（AI 自动审批）：先让模型判断这次调用是否安全、是否在任务范围内，判定安全就放行，否则照常问用户
- full_auto（完全访问）：全部放行
- read_only：只允许只读工具，其余一律拒绝（先看不改，做计划用）
- auto_edit：改文件放行，执行命令要确认
web 界面只给前三种，后两种命令行 --mode 可用。

模式之外，有三种情况要多问一句（PermissionGate.check）：
- 碰到工作目录以外的路径：完全访问放行；AI 自动审批下读交给 AI 判断、改要问用户；其余问用户
- bash 要在沙箱外执行：除了完全访问都问用户（不交给 AI）
- 碰到密钥、凭据目录：什么模式都问用户，也不给「总是允许」
改的全是项目记忆目录里的文件时直接放行（只读模式除外）。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from pocketagent.questions import Question, QuestionAnswer
from pocketagent.tools.base import Tool, ToolAccess, ToolKind


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


# 从松到严；几条规则同时生效时取最严的
_STRICTNESS = [Decision.ALLOW, Decision.AI_REVIEW, Decision.ASK, Decision.DENY]


def stricter(first: Decision, second: Decision) -> Decision:
    return first if _STRICTNESS.index(first) >= _STRICTNESS.index(second) else second


class ApprovalAnswer(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"


@dataclass
class ApprovalRequest:
    tool_name: str
    summary: str
    preview: str
    # 为什么要多问一句：工作目录外、沙箱外、密钥目录
    notes: list[str] = field(default_factory=list)
    # 「总是允许」按钮上的字；空的表示这次不给「总是允许」
    always_label: str = ""


class Approver(Protocol):
    """「问人」的通道：请求批准，以及 ask_user 工具的提问。CLI 用 input()，web 推给浏览器。

    ask_questions 返回每个问题的回答；返回 None 表示用户跳过（或者没人能回答）。
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer: ...

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None: ...


class DenyAllApprover:
    """非交互场景（-p 单次执行、测试）用：需要确认的一律拒绝，问题没人回答。"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer.DENY

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        return None


class AllowAllApprover:
    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer.ALLOW_ONCE

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        return None


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


def _directory_of(path: Path) -> Path:
    return path if path.is_dir() else path.parent


@dataclass
class PermissionCheck:
    """一次调用的审批结果，以及选「本任务总是允许」时要记住什么。"""

    decision: Decision
    notes: list[str] = field(default_factory=list)
    remember_tool: str = ""
    remember_dirs: list[Path] = field(default_factory=list)
    remember_command: str = ""
    # 碰到密钥目录时只能一次一批
    offer_always: bool = True

    def always_label(self) -> str:
        if not self.offer_always:
            return ""
        parts = []
        if self.remember_tool:
            parts.append(self.remember_tool)
        if self.remember_command:
            parts.append("在沙箱外执行这条命令")
        if self.remember_dirs:
            parts.append("访问 " + "、".join(str(directory) for directory in self.remember_dirs))
        return "本任务总是允许 " + "，".join(parts) if parts else ""


@dataclass
class PermissionGate:
    """先 check 看要不要问；要问时调用方再准备预览（diff 等）并 ask，免得放行的调用也去算 diff。"""

    mode: ApprovalMode
    approver: Approver
    # 用户选了「本任务总是允许」的工具名、工作目录外的目录、沙箱外的命令
    always_allowed_tools: set[str] = field(default_factory=set)
    always_allowed_dirs: set[Path] = field(default_factory=set)
    always_allowed_commands: set[str] = field(default_factory=set)

    def check(self, tool: Tool, access: ToolAccess | None = None) -> PermissionCheck:
        access = access or ToolAccess()
        by_mode = decide_by_mode(self.mode, tool.kind)
        if by_mode == Decision.DENY:
            return PermissionCheck(Decision.DENY)
        if access.memory_only:
            # 记项目记忆是 agent 自己的笔记，不打扰用户
            return PermissionCheck(Decision.ALLOW)
        check = PermissionCheck(Decision.ALLOW)
        if by_mode != Decision.ALLOW and tool.name not in self.always_allowed_tools:
            check.decision = by_mode
            check.remember_tool = tool.name
        if access.outside_sandbox and access.command not in self.always_allowed_commands:
            check.notes.append("这条命令要在沙箱外执行：能联网，也能写工作目录以外的文件")
            check.remember_command = access.command
            check.decision = stricter(check.decision, self._unless_full_auto(Decision.ASK))
        outside = [path for path in access.outside_paths if not self._directory_allowed(path)]
        if outside:
            check.notes.append("要访问工作目录以外的路径：" + "、".join(str(path) for path in outside))
            check.remember_dirs = sorted({_directory_of(path) for path in outside})
            wanted = Decision.AI_REVIEW if tool.kind == ToolKind.READ and self.mode == ApprovalMode.AI_AUTO else Decision.ASK
            check.decision = stricter(check.decision, self._unless_full_auto(wanted))
        if access.secret_paths:
            check.notes.append("要碰的是密钥 / 凭据：" + "、".join(str(path) for path in access.secret_paths))
            check.offer_always = False
            check.decision = stricter(check.decision, Decision.ASK)
        return check

    def decide(self, tool: Tool, access: ToolAccess | None = None) -> Decision:
        return self.check(tool, access).decision

    def _unless_full_auto(self, decision: Decision) -> Decision:
        return Decision.ALLOW if self.mode == ApprovalMode.FULL_AUTO else decision

    def _directory_allowed(self, path: Path) -> bool:
        return any(path == directory or directory in path.parents for directory in self.always_allowed_dirs)

    def denial_reason(self, tool: Tool) -> str:
        return f"当前是 {self.mode.value} 模式，不允许调用 {tool.kind.value} 类工具 {tool.name}"

    async def ask(self, request: ApprovalRequest, check: PermissionCheck | None = None) -> bool:
        answer = await self.approver.request_approval(request)
        if answer == ApprovalAnswer.ALLOW_ALWAYS:
            self.remember(check or PermissionCheck(Decision.ASK, remember_tool=request.tool_name))
        return answer in (ApprovalAnswer.ALLOW_ONCE, ApprovalAnswer.ALLOW_ALWAYS)

    def remember(self, check: PermissionCheck) -> None:
        if not check.offer_always:
            return
        if check.remember_tool:
            self.always_allowed_tools.add(check.remember_tool)
        if check.remember_command:
            self.always_allowed_commands.add(check.remember_command)
        self.always_allowed_dirs.update(check.remember_dirs)
