"""把节点连成主循环，并提供 Agent：一次 run_turn 就是用户说一句话、agent 干到停下。

主循环（PocketFlow 的图）：

    guard ──think──▶ think ──tool_calls──▶ run_tools ──next──▶ guard
      │                │                       │
      ├──compact──▶ compact ──▶ think          │
      │                │                       │
      └──stop──▶ finish ◀──done────────────────┘ stop（用户拒绝）
"""

import asyncio
from typing import Any

from pocketflow import AsyncFlow

from pocketagent.config import Settings
from pocketagent.context import AgentContext, TurnState
from pocketagent.events import EventBus, EventType
from pocketagent.llm.base import LLMProvider
from pocketagent.nodes import CompactNode, FinishNode, GuardNode, RunToolsNode, ThinkNode
from pocketagent.permissions import Approver, PermissionGate
from pocketagent.prompts import build_system_prompt
from pocketagent.session import Session, SessionStore
from pocketagent.tools import default_tool_registry
from pocketagent.tools.base import ToolContext, ToolRegistry
from pocketagent.trace import JsonlTracer

INTERRUPTED_TOOL_RESULT = "用户中断了这一轮，这个调用没有执行完"


def build_agent_flow() -> AsyncFlow:
    guard = GuardNode()
    think = ThinkNode()
    run_tools = RunToolsNode()
    compact = CompactNode()
    finish = FinishNode()

    guard - "think" >> think
    guard - "compact" >> compact
    guard - "stop" >> finish
    compact >> think
    think - "tool_calls" >> run_tools
    think - "done" >> finish
    run_tools - "next" >> guard
    run_tools - "stop" >> finish
    return AsyncFlow(start=guard)


def close_dangling_tool_calls(messages: list[dict[str, Any]]) -> None:
    """一轮被中断或出错时，最后一条 assistant 消息里可能有还没拿到结果的 tool_calls。
    OpenAI 格式要求每个 tool_call 都有对应的 tool 消息，否则下一次请求直接 400，这里补上。"""
    last_assistant_index = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            last_assistant_index = index
            break
    if last_assistant_index is None:
        return
    tool_calls = messages[last_assistant_index].get("tool_calls") or []
    answered_ids = {
        message.get("tool_call_id") for message in messages[last_assistant_index + 1 :] if message.get("role") == "tool"
    }
    for tool_call in tool_calls:
        if tool_call["id"] not in answered_ids:
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": INTERRUPTED_TOOL_RESULT})


class Agent:
    def __init__(self, context: AgentContext, session_store: SessionStore) -> None:
        self.context = context
        self.session_store = session_store
        self.flow = build_agent_flow()
        self.refresh_system_prompt()

    @property
    def session(self) -> Session:
        return self.context.session

    def refresh_system_prompt(self) -> None:
        """续跑旧会话时也重建系统提示：日期、AGENTS.md 可能已经变了。"""
        settings = self.context.settings
        system_message = {
            "role": "system",
            "content": build_system_prompt(settings.workspace, settings.home_dir, self.context.tools),
        }
        messages = self.context.messages
        if messages and messages[0].get("role") == "system":
            messages[0] = system_message
        else:
            messages.insert(0, system_message)

    async def run_turn(self, user_text: str) -> str:
        context = self.context
        context.turn = TurnState()
        context.messages.append({"role": "user", "content": user_text})
        await context.events.emit(EventType.TURN_START, session_id=self.session.id, user_text=user_text)
        try:
            await self.flow.run_async(context)
        except (asyncio.CancelledError, KeyboardInterrupt):
            context.turn.stop_reason = "interrupted"
            close_dangling_tool_calls(context.messages)
            raise
        except Exception as error:
            context.turn.stop_reason = "error"
            close_dangling_tool_calls(context.messages)
            await context.events.emit(EventType.ERROR, message=f"{type(error).__name__}: {error}")
        finally:
            self.session_store.save(self.session)
            await context.events.emit(
                EventType.TURN_END, stop_reason=context.turn.stop_reason, final_text=context.turn.final_text
            )
        return context.turn.final_text

    async def compact(self) -> None:
        """手动压缩（/compact），复用 CompactNode 的重试和 trace。"""
        await AsyncFlow(start=CompactNode()).run_async(self.context)
        self.session_store.save(self.session)


def create_agent(
    settings: Settings,
    approver: Approver,
    provider: LLMProvider | None = None,
    tools: ToolRegistry | None = None,
    session: Session | None = None,
    enable_trace: bool = True,
) -> Agent:
    """组装一个 Agent。provider / tools 留空就用默认的（OpenAI 兼容接口 + 内置工具）。"""
    session_store = SessionStore(settings.sessions_dir)
    if session is None:
        session = session_store.new_session(settings.workspace)
    session.model_profile = settings.model_profile_id
    if provider is None:
        provider = create_provider(settings)

    events = EventBus()
    if enable_trace:
        events.subscribe(JsonlTracer(settings.traces_dir / f"{session.id}.jsonl"))

    context = AgentContext(
        settings=settings,
        provider=provider,
        tools=tools or default_tool_registry(),
        tool_context=ToolContext(workspace=settings.workspace, shell_timeout_seconds=settings.shell_timeout_seconds),
        permissions=PermissionGate(mode=settings.approval_mode, approver=approver),
        events=events,
        session=session,
    )
    return Agent(context, session_store)


def create_provider(settings: Settings) -> LLMProvider:
    """按设置里的模型、接口地址、密钥造一个 OpenAI 兼容接口的 provider。"""
    from pocketagent.llm.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
        reasoning_effort=settings.reasoning_effort,
        extra_body=settings.extra_body,
    )
