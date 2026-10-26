"""agent 主循环的 PocketFlow 节点，连线在 agent.py 的 build_agent_flow。

图只描述循环的几个阶段，不为每个工具单独建节点：模型这一步要调哪些工具是运行时才知道的，
由 RunToolsNode 按工具注册表动态分发，所以加工具、接 MCP 都不用改图。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pocketflow import AsyncNode

from pocketagent.attachments import materialize_messages, message_display
from pocketagent.compaction import rebuild_messages_with_summary, summarize_history
from pocketagent.context import AgentContext
from pocketagent.events import EventBus, EventType
from pocketagent.llm.base import LLMProvider, LLMResponse, ToolCall, build_assistant_message
from pocketagent.permissions import ApprovalRequest, Decision
from pocketagent.reviewer import ReviewResult, review_tool_call
from pocketagent.tools.base import Tool, ToolKind, ToolResult, find_missing_arguments, truncate_middle


def messages_for_model(context: AgentContext) -> list[dict[str, Any]]:
    """发给模型的消息：去掉内部字段，展开附件引用（不能看图的模型拿到的是文字说明）。"""
    return materialize_messages(context.messages, context.attachments_root, context.settings.supports_images)


class AgentNode(AsyncNode):
    """所有节点的基类：进出节点时发事件，trace 就靠这个。

    覆盖的是 PocketFlow 的 _run_async（prep → exec → post 的总入口），不改上游代码。
    """

    async def _run_async(self, context: AgentContext) -> Any:
        node_name = type(self).__name__
        await context.events.emit(EventType.NODE_START, node=node_name)
        started_at = time.monotonic()
        action = await super()._run_async(context)
        elapsed_seconds = round(time.monotonic() - started_at, 3)
        await context.events.emit(EventType.NODE_END, node=node_name, action=action, seconds=elapsed_seconds)
        return action


class GuardNode(AgentNode):
    """每次调模型前的检查：先把用户插的话放进对话，步数到上限就停，上下文快满了就先压缩。"""

    async def post_async(self, context: AgentContext, prep_result: Any, exec_result: Any) -> str:
        await _apply_interjections(context)
        if context.turn.step_count >= context.settings.max_steps:
            context.turn.stop_reason = "step_limit"
            await context.events.emit(EventType.STEP_LIMIT, max_steps=context.settings.max_steps)
            return "stop"
        if context.estimated_context_tokens() > context.context_limit_tokens():
            return "compact"
        return "think"


async def _apply_interjections(context: AgentContext) -> None:
    while context.pending_interjections:
        message = context.pending_interjections.pop(0)
        context.messages.append(message)
        display = message_display(message)
        await context.events.emit(
            EventType.INTERJECTION_APPLIED, user_text=display["text"], attachments=display["attachments"]
        )


class _EventStreamListener:
    """把 provider 的流式增量转成事件。"""

    def __init__(self, events: EventBus) -> None:
        self.events = events

    async def on_text(self, text: str) -> None:
        await self.events.emit(EventType.TEXT_DELTA, text=text)

    async def on_reasoning(self, text: str) -> None:
        await self.events.emit(EventType.REASONING_DELTA, text=text)


@dataclass
class ThinkRequest:
    provider: LLMProvider
    messages: list[dict[str, Any]]
    tool_schemas: list[dict[str, Any]]
    events: EventBus


class ThinkNode(AgentNode):
    """调一次模型（流式），决定是调工具还是结束这一轮。网络错误按 PocketFlow 的重试机制重试。"""

    def __init__(self, max_retries: int = 3, wait: float = 2) -> None:
        super().__init__(max_retries=max_retries, wait=wait)

    async def prep_async(self, context: AgentContext) -> ThinkRequest:
        context.turn.step_count += 1
        return ThinkRequest(
            provider=context.provider,
            messages=messages_for_model(context),
            tool_schemas=context.tools.openai_schemas(),
            events=context.events,
        )

    async def exec_async(self, request: ThinkRequest) -> LLMResponse:
        listener = _EventStreamListener(request.events)
        try:
            return await request.provider.complete(request.messages, request.tool_schemas, listener)
        except Exception as error:
            # cur_retry 由 PocketFlow 的重试循环维护；还有重试机会时告诉界面丢掉已经流出来的半截输出
            if self.cur_retry < self.max_retries - 1:
                await request.events.emit(
                    EventType.LLM_RETRY, attempt=self.cur_retry + 2, error=f"{type(error).__name__}: {error}"
                )
            raise

    async def post_async(self, context: AgentContext, request: ThinkRequest, response: LLMResponse) -> str:
        if response.usage:
            context.last_prompt_tokens = response.usage.prompt_tokens
        await context.events.emit(
            EventType.LLM_RESPONSE,
            text=response.text,
            reasoning=response.reasoning,
            tool_calls=[{"name": call.name, "arguments": call.raw_arguments} for call in response.tool_calls],
            finish_reason=response.finish_reason,
            usage=response.usage,
        )
        context.messages.append(build_assistant_message(response))
        if response.tool_calls:
            context.turn.pending_tool_calls = response.tool_calls
            return "tool_calls"
        if context.pending_interjections:
            # 模型以为做完了，但用户刚插了话：回到 guard 把话放进去，再想一轮
            return "continue"
        context.turn.final_text = response.text
        context.turn.stop_reason = "done"
        return "done"


@dataclass
class ToolCallOutcome:
    call: ToolCall
    result: ToolResult
    denied: bool = False
    review: ReviewResult | None = None


@dataclass
class ToolBatch:
    calls: list[ToolCall]
    context: AgentContext
    finished: list[ToolCallOutcome] = field(default_factory=list)


class RunToolsNode(AgentNode):
    """执行模型这一步要求的全部工具调用。

    全是只读工具时并发执行；只要有写文件或执行命令的就按顺序来，
    其中一个被用户拒绝，后面的都不再执行，这一轮停下等用户发话（和 Claude Code 一致）。
    """

    async def prep_async(self, context: AgentContext) -> ToolBatch:
        return ToolBatch(calls=list(context.turn.pending_tool_calls), context=context)

    async def exec_async(self, batch: ToolBatch) -> list[ToolCallOutcome]:
        if _all_read_only(batch):
            return list(await asyncio.gather(*(run_tool_call(call, batch.context) for call in batch.calls)))
        try:
            await self._run_sequentially(batch)
        except asyncio.CancelledError:
            # 被中断时先把已经执行完的结果写进历史，模型下一轮才知道哪些操作真的做了
            append_tool_messages(batch.context, batch.finished)
            raise
        return batch.finished

    async def _run_sequentially(self, batch: ToolBatch) -> None:
        for call in batch.calls:
            if any(outcome.denied for outcome in batch.finished):
                skipped = ToolResult("前面的调用被用户拒绝了，这个调用没有执行", is_error=True)
                batch.finished.append(ToolCallOutcome(call=call, result=skipped, denied=True))
                continue
            batch.finished.append(await run_tool_call(call, batch.context))

    async def post_async(self, context: AgentContext, batch: ToolBatch, outcomes: list[ToolCallOutcome]) -> str:
        append_tool_messages(context, outcomes)
        context.turn.pending_tool_calls = []
        if any(outcome.denied for outcome in outcomes):
            context.turn.stop_reason = "denied"
            return "stop"
        return "next"


def _all_read_only(batch: ToolBatch) -> bool:
    for call in batch.calls:
        tool = batch.context.tools.get(call.name)
        if tool is None or tool.kind != ToolKind.READ:
            return False
    return True


def append_tool_messages(context: AgentContext, outcomes: list[ToolCallOutcome]) -> None:
    """下划线开头的字段只给界面重放历史用，发给模型前会去掉。"""
    for outcome in outcomes:
        message = {
            "role": "tool",
            "tool_call_id": outcome.call.id,
            "content": outcome.result.content,
            "_is_error": outcome.result.is_error,
            "_denied": outcome.denied,
        }
        if outcome.review is not None:
            message["_review"] = {"allow": outcome.review.allow, "reason": outcome.review.reason}
        context.messages.append(message)


async def run_tool_call(call: ToolCall, context: AgentContext) -> ToolCallOutcome:
    """单个工具调用：发事件 → 检查 → 审批 → 执行 → 截断输出 → 发事件。错误都变成结果回给模型。"""
    tool = context.tools.get(call.name)
    summary = tool.describe(call.arguments) if tool else call.raw_arguments
    await context.events.emit(
        EventType.TOOL_CALL, call_id=call.id, name=call.name, summary=summary, arguments=call.arguments
    )
    outcome = await _check_and_execute(tool, call, summary, context)
    outcome.result.content = truncate_middle(outcome.result.content, context.settings.max_tool_output_chars)
    await context.events.emit(
        EventType.TOOL_RESULT,
        call_id=call.id,
        name=call.name,
        content=outcome.result.content,
        is_error=outcome.result.is_error,
        denied=outcome.denied,
    )
    return outcome


@dataclass
class ApprovalOutcome:
    approved: bool
    review: ReviewResult | None = None


async def _check_and_execute(tool: Tool | None, call: ToolCall, summary: str, context: AgentContext) -> ToolCallOutcome:
    if tool is None:
        available = "、".join(context.tools.names())
        return _error_outcome(call, f"没有叫 {call.name} 的工具，可用的有：{available}")
    if call.arguments_error:
        return _error_outcome(call, call.arguments_error)
    missing = find_missing_arguments(tool, call.arguments)
    if missing:
        return _error_outcome(call, f"缺少必填参数：{', '.join(missing)}")

    decision = context.permissions.decide(tool)
    if decision == Decision.DENY:
        return _denied_outcome(call, context.permissions.denial_reason(tool))
    if decision in (Decision.ASK, Decision.AI_REVIEW):
        try:
            preview = tool.preview(call.arguments, context.tool_context)
        except Exception as error:
            return _error_outcome(call, f"{type(error).__name__}: {error}")
        approval = await _approve(decision, tool, call, summary, preview, context)
        if not approval.approved:
            denied = _denied_outcome(call, "用户拒绝了这次调用")
            denied.review = approval.review
            return denied
        review = approval.review
    else:
        review = None

    try:
        result = await tool.run(call.arguments, context.tool_context)
    except Exception as error:
        result = ToolResult(f"{type(error).__name__}: {error}", is_error=True)
    return ToolCallOutcome(call=call, result=result, review=review)


async def _approve(
    decision: Decision, tool: Tool, call: ToolCall, summary: str, preview: str, context: AgentContext
) -> ApprovalOutcome:
    """AI 审批模式先让模型判断，判定安全直接放行；否则（或本来就是询问模式）问用户。"""
    review = None
    if decision == Decision.AI_REVIEW:
        review = await review_tool_call(
            context.provider,
            context.messages,
            str(context.settings.workspace),
            tool.name,
            call.arguments,
            preview,
        )
        await context.events.emit(
            EventType.TOOL_REVIEW, call_id=call.id, name=tool.name, allow=review.allow, reason=review.reason
        )
        if review.allow:
            return ApprovalOutcome(approved=True, review=review)
        summary = f"{summary}\n（AI 审批没有放行：{review.reason}）"
    request = ApprovalRequest(tool_name=tool.name, summary=summary, preview=preview)
    return ApprovalOutcome(approved=await context.permissions.ask(request), review=review)


def _error_outcome(call: ToolCall, message: str) -> ToolCallOutcome:
    return ToolCallOutcome(call=call, result=ToolResult(message, is_error=True))


def _denied_outcome(call: ToolCall, reason: str) -> ToolCallOutcome:
    message = f"{reason}。不要原样重试，先停下来向用户说明你想做什么。"
    return ToolCallOutcome(call=call, result=ToolResult(message, is_error=True), denied=True)


@dataclass
class CompactRequest:
    provider: LLMProvider
    messages: list[dict[str, Any]]


class CompactNode(AgentNode):
    """让模型把历史总结成摘要，替换掉原来的消息。"""

    def __init__(self, max_retries: int = 2, wait: float = 2) -> None:
        super().__init__(max_retries=max_retries, wait=wait)

    async def prep_async(self, context: AgentContext) -> CompactRequest:
        return CompactRequest(provider=context.provider, messages=messages_for_model(context))

    async def exec_async(self, request: CompactRequest) -> str:
        return await summarize_history(request.provider, request.messages)

    async def post_async(self, context: AgentContext, request: CompactRequest, summary: str) -> None:
        tokens_before = context.estimated_context_tokens()
        context.session.messages = rebuild_messages_with_summary(context.messages, summary)
        context.last_prompt_tokens = 0
        await context.events.emit(
            EventType.COMPACTION, tokens_before=tokens_before, tokens_after=context.estimated_context_tokens()
        )


class FinishNode(AgentNode):
    """终点。单独设一个节点是因为 PocketFlow 在「有后继、但返回的 action 没连线」时会告警，
    让 think / guard / run_tools 的结束分支都连到这里，流程就能干净地结束。"""

    async def post_async(self, context: AgentContext, prep_result: Any, exec_result: Any) -> None:
        return None
