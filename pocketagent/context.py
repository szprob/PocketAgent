"""AgentContext：PocketFlow 流里所有节点共享的状态（即 PocketFlow 说的 shared store）。

PocketFlow 只要求 shared 是个对象、原样传给每个节点；这里用 dataclass 代替裸 dict，
每个字段是谁读谁写一目了然，IDE 也能补全。
"""

from dataclasses import dataclass, field
from typing import Any

from pocketagent.compaction import estimate_tokens
from pocketagent.config import Settings
from pocketagent.events import EventBus
from pocketagent.llm.base import LLMProvider, ToolCall
from pocketagent.permissions import PermissionGate
from pocketagent.session import Session
from pocketagent.tools.base import ToolContext, ToolRegistry


@dataclass
class TurnState:
    """一轮（用户说一句话到 agent 停下）内的临时状态，每轮开始时清空。"""

    step_count: int = 0
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    stop_reason: str = ""


@dataclass
class AgentContext:
    settings: Settings
    provider: LLMProvider
    tools: ToolRegistry
    tool_context: ToolContext
    permissions: PermissionGate
    events: EventBus
    session: Session
    turn: TurnState = field(default_factory=TurnState)
    # 最近一次模型调用报告的 prompt token 数，比字符估算准
    last_prompt_tokens: int = 0

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.session.messages

    def estimated_context_tokens(self) -> int:
        # 消息只增不减（压缩时会把 last_prompt_tokens 清零），两者取大更稳妥
        return max(estimate_tokens(self.messages), self.last_prompt_tokens)

    def context_limit_tokens(self) -> int:
        return int(self.settings.context_window_tokens * self.settings.compact_threshold)
