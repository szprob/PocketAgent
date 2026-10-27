"""agent 主循环的 PocketFlow 节点，连线在 agent.py 的 build_agent_flow。

图只描述循环的几个阶段，不为每个工具单独建节点：模型这一步要调哪些工具是运行时才知道的，
由 RunToolsNode 按工具注册表动态分发，所以加工具、接 MCP 都不用改图。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pocketflow import AsyncNode

from pocketagent.core.compaction import (
    PendingCompaction,
    rebuild_messages_with_summary,
    rebuild_with_summary_and_tail,
    shrink_for_summary,
    summarize_history,
)
from pocketagent.core.context import AgentContext
from pocketagent.core.events import EventBus, EventType
from pocketagent.core.tool_outputs import save_tool_output
from pocketagent.interaction.attachments import (
    AttachmentStore,
    materialize_messages,
    message_display,
    tool_message_content,
)
from pocketagent.llm.base import ContextOverflowError, LLMProvider, LLMResponse, ToolCall, build_assistant_message
from pocketagent.safety.permissions import ApprovalRequest, Decision, PermissionCheck
from pocketagent.safety.reviewer import ReviewResult, review_tool_call
from pocketagent.tools.base import (
    Tool,
    ToolKind,
    ToolResult,
    current_tool_call_id,
    current_tool_output,
    find_missing_arguments,
    truncate_middle,
)

# 一轮里最多压缩几次：系统提示本身就很长时，再压也压不下去，别一直压
MAX_COMPACTIONS_PER_TURN = 3
# 做摘要时给历史留的上下文比例（剩下的留给摘要指令和输出）
SUMMARY_INPUT_RATIO = 0.85

# 上游把生成掐掉的 finish_reason → 这一轮停下来时记的 stop_reason。
# content_filter：模型或网关的审核在句子中间掐断；incomplete：流没给结束标记就断了（多半是连接中途断）。
# 这两种都只收到半句话，重试没用（同样的内容还会被掐一次），但必须让用户看出「这不是答完了」。
CUT_OFF_FINISH_REASONS = {
    "content_filter": "filtered",
    "incomplete": "incomplete",
}


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
        # 节点边界存一次会话：一轮里工具跑了很多步、进程被杀时，历史不会停在上一轮结束
        await context.record_history()
        return action


class GuardNode(AgentNode):
    """每次调模型前的检查：
    1. 把用户插的话放进对话；后台压缩做好了就换上摘要
    2. 步数到上限就停
    3. 上下文超过阈值：开始后台压缩，这一轮照常往下跑；快顶到上限了才停下来等压缩
    """

    async def post_async(self, context: AgentContext, prep_result: Any, exec_result: Any) -> str:
        await _apply_interjections(context)
        await apply_finished_compaction(context)
        if context.turn.step_count >= context.settings.max_steps:
            context.turn.stop_reason = "step_limit"
            await context.events.emit(EventType.STEP_LIMIT, max_steps=context.settings.max_steps)
            return "stop"
        if not _can_compact(context):
            return "think"
        usage = context.estimated_context_tokens()
        if usage > context.blocking_limit_tokens():
            if context.pending_compaction is not None:
                await wait_for_compaction(context)
                if context.estimated_context_tokens() <= context.blocking_limit_tokens():
                    return "think"
            context.turn.compact_reason = "auto"
            return "compact"
        if usage > context.context_limit_tokens() and context.pending_compaction is None:
            await start_background_compaction(context, reason="auto")
        return "think"


def _can_compact(context: AgentContext) -> bool:
    if not context.settings.auto_compact or context.turn.compactions >= MAX_COMPACTIONS_PER_TURN:
        return False
    # 只剩系统提示和摘要两条时再压也没用
    return len(context.messages) > 2


# ---- 后台压缩 ----


# 后台压缩时，末尾这么多条消息不进摘要、原样保留：最近的工具输出和文件内容摘要容易走样
TAIL_KEEP_MESSAGES = 8


def _snapshot_length(messages: list[dict[str, Any]]) -> int:
    """摘要覆盖到哪里为止：前面换成摘要，末尾留几条原文。边界不落在 tool 结果上
    （快照之后的第一条消息不能是没有 assistant 开头的 tool 结果），
    也至少给摘要留一条真消息，不然等于白压一次。"""
    start = max(3, len(messages) - TAIL_KEEP_MESSAGES)
    while start < len(messages) and messages[start].get("role") == "tool":
        start -= 1
    return start


def _summary_request_messages(context: AgentContext) -> list[dict[str, Any]]:
    """做摘要用的历史：不发图片；本身超长时先截短、丢掉最早的对话。"""
    messages = materialize_messages(context.messages, context.attachments_root, supports_images=False)
    limit = int(context.settings.context_window_tokens * SUMMARY_INPUT_RATIO)
    return shrink_for_summary(messages, limit)


async def start_background_compaction(context: AgentContext, reason: str) -> bool:
    """拍下当前历史，在后台做摘要。已经有一个在做了就不再开。返回是否开了新的。"""
    if context.pending_compaction is not None or len(context.messages) <= 2:
        return False
    task = asyncio.create_task(summarize_history(context.provider, _summary_request_messages(context)))
    context.pending_compaction = PendingCompaction(
        task=task,
        snapshot_length=_snapshot_length(context.messages),
        history_version=context.history_version,
        reason=reason,
        tokens_before=context.estimated_context_tokens(),
    )
    await context.events.emit(EventType.COMPACTION_STARTED, reason=reason, tokens_before=context.estimated_context_tokens())
    return True


async def apply_finished_compaction(context: AgentContext) -> bool:
    """后台压缩做完了就换上「摘要 + 快照之后的消息」。没做完、做失败、或者已经过期就什么都不改。"""
    pending = context.pending_compaction
    if pending is None or not pending.task.done():
        return False
    context.pending_compaction = None
    if pending.task.cancelled():
        return False
    error = pending.task.exception()
    if error is not None:
        await context.events.emit(EventType.COMPACTION_FAILED, message=f"{type(error).__name__}: {error}")
        return False
    if pending.history_version != context.history_version:
        # 期间历史已经被别的压缩换掉了，这份摘要对不上
        return False
    tokens_before = context.estimated_context_tokens()
    _keep_title(context)
    context.session.messages = rebuild_with_summary_and_tail(context.messages, pending.snapshot_length, pending.task.result())
    _after_history_replaced(context)
    await context.events.emit(
        EventType.COMPACTION,
        reason=pending.reason,
        background=True,
        tokens_before=tokens_before,
        tokens_after=context.estimated_context_tokens(),
    )
    return True


async def wait_for_compaction(context: AgentContext) -> bool:
    """上下文快顶到上限了，停下来等后台压缩做完再换上。"""
    pending = context.pending_compaction
    if pending is None:
        return False
    if not pending.task.done():
        await context.events.emit(EventType.COMPACTION_WAITING)
        await asyncio.wait([pending.task])
    return await apply_finished_compaction(context)


def cancel_background_compaction(context: AgentContext) -> None:
    if context.pending_compaction is not None:
        context.pending_compaction.task.cancel()
        context.pending_compaction = None


def _keep_title(context: AgentContext) -> None:
    """压缩后第一条 user 消息就成了摘要，标题会跟着变；换之前把原来的标题定下来。"""
    if not context.session.title:
        context.session.title = context.session.display_title()


def _after_history_replaced(context: AgentContext) -> None:
    context.history_version += 1
    context.token_anchor = None
    context.turn.compactions += 1


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

    async def on_retry(self, attempt: int, error: Exception, wait_seconds: float) -> None:
        # 界面收到后丢掉已经流出来的半截输出
        await self.events.emit(
            EventType.LLM_RETRY,
            attempt=attempt,
            error=f"{type(error).__name__}: {error}",
            wait_seconds=round(wait_seconds, 1),
        )


@dataclass
class ThinkRequest:
    provider: LLMProvider
    messages: list[dict[str, Any]]
    tool_schemas: list[dict[str, Any]]
    events: EventBus


class ThinkNode(AgentNode):
    """调一次模型（流式），决定是调工具还是结束这一轮。
    重试在 provider 里做，只重试 429 / 5xx / 断线这类；这里不再用 PocketFlow 的「什么错都重试」。"""

    async def prep_async(self, context: AgentContext) -> ThinkRequest:
        context.turn.step_count += 1
        return ThinkRequest(
            provider=context.provider,
            messages=messages_for_model(context),
            tool_schemas=context.tools.openai_schemas(),
            events=context.events,
        )

    async def exec_async(self, request: ThinkRequest) -> LLMResponse | ContextOverflowError:
        listener = _EventStreamListener(request.events)
        try:
            return await request.provider.complete(request.messages, request.tool_schemas, listener)
        except ContextOverflowError as overflow:
            # 交给 post 去走压缩分支
            return overflow

    async def post_async(
        self, context: AgentContext, request: ThinkRequest, response: LLMResponse | ContextOverflowError
    ) -> str:
        if isinstance(response, ContextOverflowError):
            # 接口说上下文超长：不管开没开自动压缩，都先压缩再重试；压了几次还超就只能报错
            if context.turn.compactions >= MAX_COMPACTIONS_PER_TURN or len(context.messages) <= 2:
                raise response
            if await wait_for_compaction(context):
                return "continue"
            context.turn.compact_reason = "overflow"
            return "compact"
        await context.events.emit(
            EventType.LLM_RESPONSE,
            text=response.text,
            reasoning=response.reasoning,
            tool_calls=[{"name": call.name, "arguments": call.raw_arguments} for call in response.tool_calls],
            finish_reason=response.finish_reason,
            usage=response.usage,
        )
        if response.finish_reason == "length":
            return await _handle_truncated_response(context, response)
        cut_off_stop_reason = CUT_OFF_FINISH_REASONS.get(response.finish_reason or "")
        if cut_off_stop_reason:
            return _finish_cut_off(context, response, cut_off_stop_reason)
        context.messages.append(build_assistant_message(response))
        if response.usage and response.usage.prompt_tokens:
            context.record_usage(response.usage)
        if response.tool_calls:
            context.turn.pending_tool_calls = response.tool_calls
            return "tool_calls"
        if context.pending_interjections:
            # 模型以为做完了，但用户刚插了话：回到 guard 把话放进去，再想一轮
            return "continue"
        context.turn.final_text = response.text
        context.turn.stop_reason = "done"
        return "done"


# 回复超出输出上限被截断时，最多让模型补救几次
MAX_TRUNCATION_RECOVERIES = 2
TRUNCATED_TOOL_NOTE = (
    "（系统提示：你上一条回复超出了单次输出长度上限，被截断了，工具调用的参数不完整，所以没有执行。"
    "请把操作拆小：大文件分几次写，改已有文件用 edit_file 只替换需要改的那一段。）"
)
TRUNCATED_TEXT_NOTE = "（系统提示：你上一条回复超出了单次输出长度上限，被截断了。请从断开的地方接着写，不要重复已经写过的内容。）"


async def _handle_truncated_response(context: AgentContext, response: LLMResponse) -> str:
    """finish_reason=length：截断的工具调用 JSON 不完整，不能执行；纯文字就让模型接着写。"""
    context.turn.truncations += 1
    had_tool_calls = bool(response.tool_calls)
    gave_up = context.turn.truncations > MAX_TRUNCATION_RECOVERIES
    await context.events.emit(EventType.LLM_TRUNCATED, had_tool_calls=had_tool_calls, gave_up=gave_up)
    if gave_up:
        # _finish 记下断在哪：界面重放历史时还能在回复末尾标出来
        context.messages.append(
            {"role": "assistant", "content": response.text or "（回复多次超出长度上限，先停下）", "_finish": "truncated"}
        )
        context.turn.final_text = response.text
        context.turn.stop_reason = "truncated"
        return "done"
    if response.text:
        context.messages.append({"role": "assistant", "content": response.text})
    note = TRUNCATED_TOOL_NOTE if had_tool_calls else TRUNCATED_TEXT_NOTE
    # _kind 标成内部提示：界面重放历史时显示成提示行，也不当成用户的原话
    context.messages.append({"role": "user", "content": note, "_kind": "notice"})
    return "continue"


def _finish_cut_off(context: AgentContext, response: LLMResponse, stop_reason: str) -> str:
    """上游把生成掐断了：半截话留在对话里并标上原因，这一轮就停在这儿。

    不重试、也不让模型「接着写」：审核掐掉的内容再问一次还是会被掐，
    连接断掉的那半句也接不回来，硬续反而会让用户以为后面是完整内容。
    """
    if response.text:
        message = build_assistant_message(response)
        # _finish 记下断在哪：界面重放历史时还能在回复末尾标出来
        message["_finish"] = stop_reason
        context.messages.append(message)
    if response.usage and response.usage.prompt_tokens:
        context.record_usage(response.usage)
    context.turn.final_text = response.text
    context.turn.stop_reason = stop_reason
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
        # 这一轮的检查点和第一次调用模型同时在拍，动工作区之前必须拍完
        await context.wait_for_turn_checkpoint()
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


# 工具输出事件最多这么频繁地发，避免 `yes` 这类命令把 WebSocket 刷爆
OUTPUT_EVENT_INTERVAL_SECONDS = 0.1
OUTPUT_EVENT_MAX_CHARS = 8_000


class _ToolOutputEmitter:
    """把工具吐出来的输出合并成 TOOL_OUTPUT_DELTA 事件，界面只用来实时显示，不进模型上下文。"""

    def __init__(self, events: EventBus, call_id: str, name: str) -> None:
        self.events = events
        self.call_id = call_id
        self.name = name
        self.pending: list[str] = []
        self.pending_chars = 0
        self.last_emit = 0.0

    async def push(self, text: str) -> None:
        # 进度条用 \r 回到行首；在网页/终端里换成换行，保留历史样子
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self.pending.append(text)
        self.pending_chars += len(text)
        if (
            self.pending_chars >= OUTPUT_EVENT_MAX_CHARS
            or time.monotonic() - self.last_emit >= OUTPUT_EVENT_INTERVAL_SECONDS
        ):
            await self.flush()

    async def flush(self) -> None:
        if not self.pending:
            return
        text = "".join(self.pending)
        self.pending.clear()
        self.pending_chars = 0
        self.last_emit = time.monotonic()
        await self.events.emit(
            EventType.TOOL_OUTPUT_DELTA,
            call_id=self.call_id,
            name=self.name,
            text=text,
        )


def append_tool_messages(context: AgentContext, outcomes: list[ToolCallOutcome]) -> None:
    """下划线开头的字段只给界面重放历史用，发给模型前会去掉。"""
    for outcome in outcomes:
        content: str | list[dict[str, Any]] = outcome.result.content
        if outcome.result.images and context.attachments_root is not None:
            store = AttachmentStore(context.attachments_root, context.session.id)
            content = tool_message_content(content, outcome.result.images, outcome.call.name, store)
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
    emitter = _ToolOutputEmitter(context.events, call.id, call.name)
    token = current_tool_call_id.set(call.id)
    output_token = current_tool_output.set(emitter.push)
    try:
        outcome = await _check_and_execute(tool, call, summary, context)
    finally:
        current_tool_output.reset(output_token)
        current_tool_call_id.reset(token)
    await emitter.flush()
    outcome.result.content = await _render_tool_result(context, call, outcome.result.content)
    await context.events.emit(
        EventType.TOOL_RESULT,
        call_id=call.id,
        name=call.name,
        content=outcome.result.content,
        is_error=outcome.result.is_error,
        denied=outcome.denied,
    )
    return outcome


async def _render_tool_result(context: AgentContext, call: ToolCall, content: str) -> str:
    """太长的工具输出：头尾给模型，完整内容落盘，并告诉模型去哪读。"""
    limit = context.settings.max_tool_output_chars
    if len(content) <= limit:
        return content
    directory = context.tool_context.tool_outputs_dir
    path = None
    if directory is not None:
        path = await asyncio.to_thread(save_tool_output, directory, call.id, content)
    truncated = truncate_middle(content, limit)
    if path is None:
        return f"{truncated}\n\n（完整输出太长，已截断）"
    return (
        f"{truncated}\n\n（完整输出 {len(content)} 字符，已保存到 {path}；"
        "需要时用 grep 搜索或 read_file 带 offset 分段查看，不要整份读进上下文）"
    )


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

    try:
        access = tool.access(call.arguments, context.tool_context)
    except Exception as error:
        return _error_outcome(call, f"{type(error).__name__}: {error}")
    check = context.permissions.check(tool, access)
    if check.decision == Decision.DENY:
        return _denied_outcome(call, context.permissions.denial_reason(tool))
    if check.decision in (Decision.ASK, Decision.AI_REVIEW):
        try:
            preview = tool.preview(call.arguments, context.tool_context)
        except Exception as error:
            return _error_outcome(call, f"{type(error).__name__}: {error}")
        approval = await _approve(check, tool, call, summary, preview, context)
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
    check: PermissionCheck, tool: Tool, call: ToolCall, summary: str, preview: str, context: AgentContext
) -> ApprovalOutcome:
    """AI 审批模式先让模型判断，判定安全直接放行；否则（或本来就是询问模式）问用户。"""
    review = None
    if check.decision == Decision.AI_REVIEW:
        review = await review_tool_call(
            context.provider,
            context.messages,
            str(context.settings.workspace),
            tool.name,
            call.arguments,
            preview,
            notes=check.notes,
        )
        await context.events.emit(
            EventType.TOOL_REVIEW, call_id=call.id, name=tool.name, allow=review.allow, reason=review.reason
        )
        if review.allow:
            return ApprovalOutcome(approved=True, review=review)
        summary = f"{summary}\n（AI 审批没有放行：{review.reason}）"
    request = ApprovalRequest(
        tool_name=tool.name,
        summary=summary,
        preview=preview,
        notes=check.notes,
        always_label=check.always_label(),
        always_parents_label=check.always_parents_label(),
    )
    return ApprovalOutcome(approved=await context.permissions.ask(request, check), review=review)


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
    """让模型把历史总结成摘要，替换掉原来的消息（当场压缩；平时走后台压缩）。重试在 provider 里做。"""

    async def prep_async(self, context: AgentContext) -> CompactRequest:
        # 要当场压缩了，后台那份（如果有）不要了，免得两份摘要先后换掉历史
        cancel_background_compaction(context)
        return CompactRequest(provider=context.provider, messages=_summary_request_messages(context))

    async def exec_async(self, request: CompactRequest) -> str:
        return await summarize_history(request.provider, request.messages)

    async def post_async(self, context: AgentContext, request: CompactRequest, summary: str) -> None:
        tokens_before = context.estimated_context_tokens()
        _keep_title(context)
        context.session.messages = rebuild_messages_with_summary(context.messages, summary)
        _after_history_replaced(context)
        reason = context.turn.compact_reason or "manual"
        context.turn.compact_reason = ""
        await context.events.emit(
            EventType.COMPACTION,
            reason=reason,
            background=False,
            tokens_before=tokens_before,
            tokens_after=context.estimated_context_tokens(),
        )


class FinishNode(AgentNode):
    """终点。单独设一个节点是因为 PocketFlow 在「有后继、但返回的 action 没连线」时会告警，
    让 think / guard / run_tools 的结束分支都连到这里，流程就能干净地结束。"""

    async def post_async(self, context: AgentContext, prep_result: Any, exec_result: Any) -> None:
        return None
