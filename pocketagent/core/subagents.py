"""子 agent：主 agent 用 subagent 工具派出去独立干一件事，跑的是同一张 PocketFlow 图，但有自己的一份上下文。

- 消息：主 agent 的系统提示 + 一段「你是子 agent」的说明 + 任务本身，看不到主对话
- 工具：explore 只给只读工具；general 给全部工具。都去掉 ask_user（不能问用户）和 subagent（不能再派）
- 权限：和主 agent 用同一个审批模式、同一组「本任务总是允许」；要审批时卡片上标明是哪个子 agent
- 模型：默认和主 agent 一样，设置里可以单独选模型 / 思考档位（subagent_model / subagent_reasoning）；
  用量记录、沙箱、记忆目录都和主 agent 共用；检查点不单独拍，主 agent 这一轮动手前已经拍过
- 时长：explore / general 各有上限（分钟，等用户审批的时间不算）。到点不直接丢掉：取消正在做的事（bash 连进程树一起杀）、
  补齐没结果的工具调用，再让它不带工具写一次总结交回来
- 过程：子 agent 每调用一个工具，就给主 agent 发一个 SUBAGENT_PROGRESS 事件，界面在子 agent 的工具卡片里实时显示
- 停止：主 agent 被中断时子 agent 一起停
"""

import asyncio
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from pocketagent.config import ConfigError, Settings
from pocketagent.core.context import AgentContext, TurnState
from pocketagent.core.events import Event, EventBus, EventType
from pocketagent.core.prompts import build_system_prompt
from pocketagent.core.session import Session
from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.llm.base import SilentListener
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalRequest, Approver, PermissionGate, ask_approvals
from pocketagent.safety.sandbox import SandboxPolicy
from pocketagent.server.worktrees import WorktreeInfo, commit_worktree, create_worktree
from pocketagent.tools.background import BackgroundTaskManager
from pocketagent.tools.base import ToolContext, ToolKind, ToolRegistry, current_tool_call_id
from pocketagent.tools.subagent import SubagentRequest, SubagentResult

# 子 agent 不能有的工具：不能问用户，不能再派子 agent
NOT_FOR_SUBAGENTS = {"ask_user", "subagent"}
PROGRESS_SUMMARY_LIMIT = 160
# 看一眼时长的间隔：到点最多晚这么久发现
TIME_LIMIT_POLL_SECONDS = 1.0
TIME_UP_PROMPT = (
    "时间到了（子 agent 的时长上限是 {minutes} 分钟），没跑完的操作已经停掉。不要再调用工具，"
    "现在根据已经查到、做完的内容写总结：做了什么、结论是什么、还有哪些没做完。"
)


class ActiveClock:
    """子 agent 跑了多久：等用户审批的时间不算（人去倒杯水，不该把子 agent 的时间用完）。"""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.paused_seconds = 0.0
        self.paused_since: float | None = None

    @property
    def paused(self) -> bool:
        return self.paused_since is not None

    def pause(self) -> None:
        if self.paused_since is None:
            self.paused_since = time.monotonic()

    def resume(self) -> None:
        if self.paused_since is not None:
            self.paused_seconds += time.monotonic() - self.paused_since
            self.paused_since = None

    def active_seconds(self) -> float:
        now = time.monotonic()
        current_pause = now - self.paused_since if self.paused_since is not None else 0.0
        return now - self.started_at - self.paused_seconds - current_pause


class LabeledApprover:
    """子 agent 的审批请求照常交给用户，摘要前面标上是哪个子 agent 发的；问题一律不问。"""

    def __init__(self, approver: Approver, label: str, clock: ActiveClock | None = None) -> None:
        self.approver = approver
        self.label = label
        # 等用户审批时暂停计时（子 agent 的时长上限不算这段）
        self.clock = clock

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        answers = await self.request_approvals([request])
        return answers[0]

    async def request_approvals(self, requests: list[ApprovalRequest]) -> list[ApprovalAnswer]:
        # 主 agent 的通道支持合并就照样合成一张卡
        if self.clock is not None:
            self.clock.pause()
        try:
            return await ask_approvals(self.approver, [self._labeled(request) for request in requests])
        finally:
            if self.clock is not None:
                self.clock.resume()

    def _labeled(self, request: ApprovalRequest) -> ApprovalRequest:
        return replace(request, summary=f"〔子 agent：{self.label}〕{request.summary}")

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


def _isolated_worktree(parent: AgentContext, request: SubagentRequest) -> tuple[WorktreeInfo | None, str]:
    """会改文件的子 agent 按设置在自己的 worktree 里做。

    只读的 explore 不隔离（要读到前台任务刚改、还没提交的内容）；不是 git 仓库这类建不出来的情况
    退回共用工作区，把原因带回去，不影响子 agent 干活。
    """
    if request.kind != "general" or not parent.settings.worktree_background or parent.settings.ephemeral:
        return None, ""
    subagent_id = f"{parent.session.id}-sub-{uuid.uuid4().hex[:6]}"
    try:
        info = create_worktree(Path(parent.session.workspace), parent.settings.home_dir, subagent_id)
    except ConfigError as error:
        return None, str(error)
    return info, ""


def _worktree_sandbox(policy: SandboxPolicy | None, info: WorktreeInfo) -> SandboxPolicy | None:
    if policy is None:
        return None
    return replace(policy, workspace=Path(info.path), worktree_git_dirs=[Path(path) for path in info.git_dirs])


def _subagent_tool_context(parent: AgentContext, settings: Settings, session: Session, worktree: WorktreeInfo | None) -> ToolContext:
    if worktree is None:
        return replace(parent.tool_context, ask_user=None, spawn_subagent=None, history=lambda: session.messages)
    return replace(
        parent.tool_context,
        workspace=Path(worktree.path),
        sandbox=_worktree_sandbox(parent.tool_context.sandbox, worktree),
        # 后台命令记在子 agent 自己名下：它的工作区是 worktree，不能挂到主任务的目录上
        background_tasks=BackgroundTaskManager(settings.background_tasks_dir, Path(worktree.path), session.id),
        ask_user=None,
        spawn_subagent=None,
        history=lambda: session.messages,
    )


def _commit_isolated(worktree: WorktreeInfo | None, request: SubagentRequest) -> bool:
    if worktree is None:
        return False
    try:
        return commit_worktree(worktree, f"子 agent：{request.description}")
    except ConfigError:
        return False


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
    from pocketagent.core.agent import build_agent_flow, close_dangling_tool_calls

    tools = subagent_tools(parent.tools, request.kind)
    parent_system_prompt = str(parent.messages[0].get("content", "")) if parent.messages else ""
    worktree, isolation_note = _isolated_worktree(parent, request)
    settings = parent.settings
    workspace = parent.session.workspace
    session_id = f"{parent.session.id}-sub"
    if worktree is not None:
        settings = replace(parent.settings, workspace=Path(worktree.path), worktree=worktree.to_dict(), ephemeral=False)
        workspace = worktree.path
        session_id = Path(worktree.root).name
    session = Session(
        id=session_id,
        workspace=workspace,
        messages=[
            {"role": "system", "content": ""},
            {"role": "user", "content": request.prompt},
        ],
    )
    tool_context = _subagent_tool_context(parent, settings, session, worktree)
    if worktree is None:
        system_prompt = parent_system_prompt
    else:
        # 主 agent 的提示词里工作目录还是来源目录，隔离后按 worktree 重写一遍，免得子 agent 去改原工作区
        system_prompt = build_system_prompt(
            Path(worktree.path),
            settings.home_dir,
            tools,
            skills=parent.skills,
            sandbox=tool_context.sandbox,
            memory_dir=tool_context.memory_dir,
            worktree=worktree.to_dict(),
        )
    session.messages[0]["content"] = subagent_system_prompt(system_prompt, request, tools)
    gate = parent.permissions
    clock = ActiveClock()
    permissions = PermissionGate(
        mode=gate.mode,
        approver=LabeledApprover(gate.approver, request.description, clock),
        # 同一组集合：在子 agent 里选了「本任务总是允许」，主 agent 也生效，反过来也一样
        always_allowed_tools=gate.always_allowed_tools,
        always_allowed_dirs=gate.always_allowed_dirs,
        always_allowed_commands=gate.always_allowed_commands,
    )
    forwarder = _ProgressForwarder(parent, call_id)
    events = EventBus()
    events.subscribe(forwarder)
    context = AgentContext(
        settings=settings,
        # 设置里可以给子 agent 单独选模型和思考档位
        provider=parent.subagent_provider() if parent.subagent_provider else parent.provider,
        tools=tools,
        tool_context=tool_context,
        permissions=permissions,
        events=events,
        session=session,
        attachments_root=parent.attachments_root,
        # 子 agent 里的调用也按设置里的审批模型 / 档位做 AI 审批
        review_provider=parent.review_provider,
        skills=parent.skills,
    )
    context.turn = TurnState()
    limit_minutes = time_limit_minutes(parent, request.kind)
    flow_task = asyncio.create_task(build_agent_flow().run_async(context))
    try:
        timed_out = await _wait_within_time_limit(flow_task, clock, limit_minutes * 60)
    except asyncio.CancelledError:
        # 主 agent 被中断：子 agent 一起停；隔离出来的改动照样提交，别把做到一半的成果丢了
        await _cancel_and_wait(flow_task)
        close_dangling_tool_calls(session.messages)
        _commit_isolated(worktree, request)
        raise
    except Exception as error:
        close_dangling_tool_calls(session.messages)
        context.turn.stop_reason = "error"
        context.turn.final_text = f"{type(error).__name__}: {error}"
        timed_out = False
    if timed_out:
        close_dangling_tool_calls(session.messages)
        context.turn.stop_reason = "time_limit"
        context.turn.final_text = await _summarize_after_time_up(context, limit_minutes)
    return SubagentResult(
        text=context.turn.final_text or _last_assistant_text(session.messages),
        stop_reason=context.turn.stop_reason or "done",
        tool_calls=forwarder.tool_calls,
        seconds=time.monotonic() - clock.started_at,
        worktree=worktree.to_dict() if worktree is not None else None,
        worktree_committed=_commit_isolated(worktree, request),
        isolation_note=isolation_note,
    )


def time_limit_minutes(parent: AgentContext, kind: str) -> int:
    if kind == "general":
        return parent.settings.subagent_general_minutes
    return parent.settings.subagent_explore_minutes


async def _wait_within_time_limit(flow_task: asyncio.Task, clock: ActiveClock, limit_seconds: float) -> bool:
    """等子 agent 跑完；到了时长上限（等审批的时间不算）就取消它正在做的事，返回 True。"""
    while not flow_task.done():
        remaining = limit_seconds - clock.active_seconds()
        if remaining <= 0 and not clock.paused:
            # 取消会一路传到正在跑的工具：bash 连进程树一起杀，已经做完的工具结果写进历史
            await _cancel_and_wait(flow_task)
            return True
        await asyncio.wait({flow_task}, timeout=min(max(remaining, 0.05), TIME_LIMIT_POLL_SECONDS))
    flow_task.result()
    return False


async def _cancel_and_wait(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _summarize_after_time_up(context: AgentContext, limit_minutes: int) -> str:
    """到点后再给一次不带工具的调用，让子 agent 把已有的结论写下来交回去。"""
    messages = context.session.messages
    messages.append({"role": "user", "content": TIME_UP_PROMPT.format(minutes=limit_minutes)})
    try:
        response = await context.provider.complete(messages, [], SilentListener())
    except Exception as error:
        return f"到了时长上限，写总结时也出错了（{type(error).__name__}: {error}）；下面是它停下前最后说的：\n{_last_assistant_text(messages)}"
    if response.text:
        messages.append({"role": "assistant", "content": response.text})
    return response.text


def attach_subagent_runner(context: AgentContext) -> None:
    """让主 agent 的 subagent 工具能派子 agent。工具上下文换过（reload_tools）之后要重新接一次。"""

    async def spawn(request: SubagentRequest) -> SubagentResult:
        return await run_subagent(context, request, current_tool_call_id.get())

    context.tool_context.spawn_subagent = spawn
