"""子 agent：主 agent 用 subagent 工具派出去独立干一件事，跑的是同一张 PocketFlow 图，但有自己的一份上下文。

- 消息：主 agent 的系统提示 + 一段「你是子 agent」的说明 + 任务本身，看不到主对话
- 工具：explore 只给只读工具；general 给全部工具。都去掉 ask_user（不能问用户）和 subagent（不能再派）
- 权限：和主 agent 用同一个审批模式、同一组「本任务总是允许」；要审批时卡片上标明是哪个子 agent
- 模型、用量记录、沙箱、记忆目录都和主 agent 共用；检查点不单独拍，主 agent 这一轮动手前已经拍过
- 过程：子 agent 每调用一个工具，就给主 agent 发一个 SUBAGENT_PROGRESS 事件，界面在子 agent 的工具卡片里实时显示
- 停止：主 agent 被中断时子 agent 一起停
"""

import asyncio
import time
from dataclasses import replace
from typing import Any

from pocketagent.context import AgentContext, TurnState
from pocketagent.events import Event, EventBus, EventType
from pocketagent.permissions import ApprovalAnswer, ApprovalRequest, Approver, PermissionGate
from pocketagent.questions import Question, QuestionAnswer
from pocketagent.session import Session
from pocketagent.tools.base import ToolKind, ToolRegistry, current_tool_call_id
from pocketagent.tools.subagent import SubagentRequest, SubagentResult

# 子 agent 不能有的工具：不能问用户，不能再派子 agent
NOT_FOR_SUBAGENTS = {"ask_user", "subagent"}
PROGRESS_SUMMARY_LIMIT = 160


class LabeledApprover:
    """子 agent 的审批请求照常交给用户，摘要前面标上是哪个子 agent 发的；问题一律不问。"""

    def __init__(self, approver: Approver, label: str) -> None:
        self.approver = approver
        self.label = label

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        labeled = replace(request, summary=f"〔子 agent：{self.label}〕{request.summary}")
        return await self.approver.request_approval(labeled)

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        return None


def subagent_tools(parent_tools: ToolRegistry, kind: str) -> ToolRegistry:
    tools = []
    for name in parent_tools.names():
        tool = parent_tools.get(name)
        if name in NOT_FOR_SUBAGENTS:
            continue
        if kind == "explore" and tool.kind != ToolKind.READ:
            continue
        tools.append(tool)
    return ToolRegistry(tools)


def subagent_system_prompt(parent_system_prompt: str, request: SubagentRequest, tools: ToolRegistry) -> str:
    read_only = "你是只读的：只能读文件、搜索、查资料，不要尝试修改任何东西。\n" if request.kind == "explore" else ""
    return f"""{parent_system_prompt}

# 你现在是子 agent
主 agent 派你去做一件事，标题是「{request.description}」，具体要求在下面的用户消息里。
- 只做这一件事，做完就停。最后用简洁的话交回结论：关键发现、相关文件路径（带行号）、做了哪些改动。主 agent 只看得到你最后这段话，看不到你中间的过程。
- 你不能向用户提问，也不能再派子 agent；拿不准的按最合理的方式做，并在结论里写明你的假设。
{read_only}- 你这次能用的工具只有：{"、".join(tools.names())}（上面列的其他工具这次用不了）。"""


class _ProgressForwarder:
    """把子 agent 的工具调用转成主 agent 的 SUBAGENT_PROGRESS 事件，顺便数一下调了几次工具。"""

    def __init__(self, parent: AgentContext, call_id: str) -> None:
        self.parent = parent
        self.call_id = call_id
        self.tool_calls = 0

    async def __call__(self, event: Event) -> None:
        if event.type == EventType.TOOL_CALL:
            self.tool_calls += 1
            await self._report("tool", event.data["name"], str(event.data.get("summary") or ""))
        elif event.type == EventType.TOOL_RESULT and (event.data.get("is_error") or event.data.get("denied")):
            label = "被拒绝" if event.data.get("denied") else "出错"
            await self._report("tool_error", event.data["name"], label)

    async def _report(self, step: str, name: str, summary: str) -> None:
        await self.parent.events.emit(
            EventType.SUBAGENT_PROGRESS,
            call_id=self.call_id,
            step=step,
            name=name,
            summary=summary[:PROGRESS_SUMMARY_LIMIT],
        )


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


async def run_subagent(parent: AgentContext, request: SubagentRequest, call_id: str) -> SubagentResult:
    # 这里才 import：agent.py 也要 import 本模块（给主 agent 接上 spawn_subagent）
    from pocketagent.agent import build_agent_flow, close_dangling_tool_calls

    tools = subagent_tools(parent.tools, request.kind)
    parent_system_prompt = str(parent.messages[0].get("content", "")) if parent.messages else ""
    session = Session(
        id=f"{parent.session.id}-sub",
        workspace=parent.session.workspace,
        messages=[
            {"role": "system", "content": subagent_system_prompt(parent_system_prompt, request, tools)},
            {"role": "user", "content": request.prompt},
        ],
    )
    gate = parent.permissions
    permissions = PermissionGate(
        mode=gate.mode,
        approver=LabeledApprover(gate.approver, request.description),
        # 同一组集合：在子 agent 里选了「本任务总是允许」，主 agent 也生效，反过来也一样
        always_allowed_tools=gate.always_allowed_tools,
        always_allowed_dirs=gate.always_allowed_dirs,
        always_allowed_commands=gate.always_allowed_commands,
    )
    forwarder = _ProgressForwarder(parent, call_id)
    events = EventBus()
    events.subscribe(forwarder)
    context = AgentContext(
        settings=parent.settings,
        provider=parent.provider,
        tools=tools,
        tool_context=replace(parent.tool_context, ask_user=None, spawn_subagent=None, history=lambda: session.messages),
        permissions=permissions,
        events=events,
        session=session,
        attachments_root=parent.attachments_root,
    )
    context.turn = TurnState()
    started = time.monotonic()
    try:
        await build_agent_flow().run_async(context)
    except asyncio.CancelledError:
        close_dangling_tool_calls(session.messages)
        raise
    except Exception as error:
        close_dangling_tool_calls(session.messages)
        context.turn.stop_reason = "error"
        context.turn.final_text = f"{type(error).__name__}: {error}"
    return SubagentResult(
        text=context.turn.final_text or _last_assistant_text(session.messages),
        stop_reason=context.turn.stop_reason or "done",
        tool_calls=forwarder.tool_calls,
        seconds=time.monotonic() - started,
    )


def attach_subagent_runner(context: AgentContext) -> None:
    """让主 agent 的 subagent 工具能派子 agent。工具上下文换过（reload_tools）之后要重新接一次。"""

    async def spawn(request: SubagentRequest) -> SubagentResult:
        return await run_subagent(context, request, current_tool_call_id.get())

    context.tool_context.spawn_subagent = spawn
