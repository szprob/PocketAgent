"""AgentContext：PocketFlow 流里所有节点共享的状态（即 PocketFlow 说的 shared store）。

PocketFlow 只要求 shared 是个对象、原样传给每个节点；这里用 dataclass 代替裸 dict，
每个字段是谁读谁写一目了然，IDE 也能补全。
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pocketagent.config import Settings
from pocketagent.core.compaction import PendingCompaction
from pocketagent.core.events import EventBus
from pocketagent.core.session import Session, SessionJournal
from pocketagent.core.tokens import estimate_tokens, estimate_tools_tokens
from pocketagent.llm.base import LLMProvider, ToolCall, Usage
from pocketagent.safety.permissions import PermissionGate
from pocketagent.tools.base import ToolContext, ToolRegistry

# 占用超过上下文窗口的这个比例时，不能再边压缩边跑了，要等压缩做完
BLOCKING_COMPACTION_RATIO = 0.95


@dataclass
class TokenAnchor:
    """接口报的真实占用：历史的前 message_count 条一共 tokens 个（含系统提示和工具定义）。"""

    tokens: int
    message_count: int
    history_version: int


@dataclass
class TurnState:
    """一轮（用户说一句话到 agent 停下）内的临时状态，每轮开始时清空。"""

    step_count: int = 0
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    stop_reason: str = ""
    # 这一轮压缩了几次、下一次压缩的原因（auto / overflow / manual），给界面显示
    compactions: int = 0
    compact_reason: str = ""
    # 这一轮回复被截断了几次
    truncations: int = 0


@dataclass
class AgentContext:
    settings: Settings
    provider: LLMProvider
    tools: ToolRegistry
    tool_context: ToolContext
    permissions: PermissionGate
    events: EventBus
    session: Session
    # 用户上传的附件放在这里，发给模型前从这里读图片
    attachments_root: Path | None = None
    turn: TurnState = field(default_factory=TurnState)
    # 最近一次模型调用报的真实占用，记账以它为准（见 tokens.py）
    token_anchor: TokenAnchor | None = None
    # agent 干活时用户插的话（完整的 user 消息），下一次调用模型前由 GuardNode 放进对话
    pending_interjections: list[dict[str, Any]] = field(default_factory=list)
    # 后台正在做的压缩；历史每被压缩换掉一次版本号加一
    pending_compaction: PendingCompaction | None = None
    history_version: int = 0
    # 这一轮开始时拍的检查点：和第一次调用模型同时进行，执行第一个工具之前必须拍完
    pending_checkpoint: "asyncio.Task[None] | None" = None
    # 一轮进行中的消息日志；子 agent 不落盘，所以是 None
    journal: SessionJournal | None = None
    # AI 自动审批用的 provider（按设置里的审批模型和思考档位现取）；None 表示就用 provider
    review_provider: Callable[[], LLMProvider] | None = None

    async def wait_for_turn_checkpoint(self) -> None:
        """工具要动工作区了，等这一轮的检查点拍完（被中断也让它拍完，git 拍到一半被杀会留下锁文件）。"""
        task = self.pending_checkpoint
        if task is None:
            return
        await asyncio.shield(task)
        self.pending_checkpoint = None

    async def record_history(self) -> None:
        """节点边界把新增消息追加到会话日志；子 agent、没接日志的场景什么都不做。"""
        if self.journal is not None:
            await self.journal.record(self.messages, self.history_version)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.session.messages

    def record_usage(self, usage: Usage) -> None:
        """模型刚回复完（assistant 消息已经追加进历史）时调用：真实占用 = 这次的输入 + 留在历史里的输出。
        思考 token 不回传给模型，要减掉。"""
        kept_output = max(usage.completion_tokens - usage.reasoning_tokens, 0)
        self.token_anchor = TokenAnchor(
            tokens=usage.prompt_tokens + kept_output,
            message_count=len(self.messages),
            history_version=self.history_version,
        )

    def estimated_context_tokens(self) -> int:
        """锚点 + 锚点之后新加的消息的估算；没有可用的锚点就整段估算（再加上工具定义）。"""
        anchor = self.token_anchor
        if anchor is not None and anchor.history_version == self.history_version and anchor.message_count <= len(self.messages):
            return anchor.tokens + estimate_tokens(self.messages[anchor.message_count :])
        return estimate_tokens(self.messages) + estimate_tools_tokens(self.tools.openai_schemas())

    def context_limit_tokens(self) -> int:
        """超过它就开始后台压缩。"""
        return int(self.settings.context_window_tokens * self.settings.compact_threshold)

    def blocking_limit_tokens(self) -> int:
        """超过它就得停下来等压缩做完，再调模型就要超长了。"""
        return int(self.settings.context_window_tokens * BLOCKING_COMPACTION_RATIO)
