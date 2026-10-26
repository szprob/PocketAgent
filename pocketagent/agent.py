"""把节点连成主循环，并提供 Agent：一次 run_turn 就是用户说一句话、agent 干到停下。

主循环（PocketFlow 的图）：

    guard ──think──▶ think ──tool_calls──▶ run_tools ──next──▶ guard
      │                │  ├──continue（模型说完了但用户刚插了话）──▶ guard
      │                │  └──compact（接口说上下文超长）──▶ compact
      ├──compact（超过阈值）──▶ compact ──▶ think
      │                │                       │
      └──stop──▶ finish ◀──done────────────────┘ stop（用户拒绝）
"""

import asyncio
from typing import Any

from pocketflow import AsyncFlow

from pocketagent.attachments import DISPLAY_KEY, message_display
from pocketagent.checkpoints import TaskCheckpoints
from pocketagent.config import Settings
from pocketagent.context import AgentContext, TurnState
from pocketagent.events import EventBus, EventType
from pocketagent.llm.base import LLMProvider
from pocketagent.nodes import (
    CompactNode,
    FinishNode,
    GuardNode,
    RunToolsNode,
    ThinkNode,
    apply_finished_compaction,
    start_background_compaction,
    wait_for_compaction,
)
from pocketagent.permissions import Approver, PermissionGate
from pocketagent.prompts import build_system_prompt
from pocketagent.sandbox import SandboxPolicy, available_sandbox, default_secret_dirs
from pocketagent.session import Session, SessionStore
from pocketagent.skills import Skill, discover_skills, find_skill_command, skill_content, skill_directories
from pocketagent.tools import default_tool_registry
from pocketagent.tools.base import ToolContext, ToolRegistry
from pocketagent.trace import JsonlTracer
from pocketagent.usage import UsageLog, UsageMetadata, UsageRecordingProvider

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
    think - "continue" >> guard
    think - "compact" >> compact
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


def enabled_skills(settings: Settings) -> list[Skill]:
    if not settings.skills_enabled:
        return []
    directories = skill_directories(settings.workspace, settings.home_dir, settings.skill_dirs)
    return [skill for skill in discover_skills(directories).skills if skill.name not in settings.disabled_skills]


def sandbox_active(settings: Settings) -> bool:
    return settings.sandbox_enabled and available_sandbox() is not None


def build_tool_context(settings: Settings, session: Session, skills: list[Skill]) -> ToolContext:
    """附件目录、技能目录不用确认就能读，项目记忆目录读写都不用确认；
    PocketAgent 自己的目录（有密钥）和 ~/.ssh 这类算密钥目录。"""
    extra_read_dirs = [settings.attachments_dir / session.id] + [skill.directory for skill in skills]
    secret_dirs = default_secret_dirs() + [settings.home_dir]
    memory_dir = None
    if settings.memory_enabled:
        memory_dir = settings.memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)
    sandbox = None
    if sandbox_active(settings):
        sandbox = SandboxPolicy(
            workspace=settings.workspace,
            allow_network=settings.sandbox_network,
            secret_dirs=secret_dirs,
            readable_exceptions=extra_read_dirs,
        )
    return ToolContext(
        workspace=settings.workspace,
        shell_timeout_seconds=settings.shell_timeout_seconds,
        extra_read_dirs=extra_read_dirs,
        secret_dirs=secret_dirs,
        sandbox=sandbox,
        memory_dir=memory_dir,
        # 会话的消息列表压缩时会整个换掉，所以每次现取
        history=lambda: session.messages,
    )


def build_tool_registry(settings: Settings, skills: list[Skill]) -> ToolRegistry:
    return default_tool_registry(
        web_search=settings.web_search,
        web_fetch=settings.web_fetch,
        sandboxed=sandbox_active(settings),
        skills=skills,
    )


class Agent:
    def __init__(
        self,
        context: AgentContext,
        session_store: SessionStore,
        usage_log: UsageLog | None = None,
        skills: list[Skill] | None = None,
    ) -> None:
        self.context = context
        self.session_store = session_store
        self.usage_log = usage_log
        self.skills = skills or []
        # 正在跑一轮；后台压缩做完时只有空闲才能直接换上摘要
        self.turn_running = False
        # 影子 git 检查点：每轮开始前给工作区拍一张
        self.checkpoints = TaskCheckpoints(context.settings.shadow_dir, context.settings.workspace, context.session)
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
            "content": build_system_prompt(
                settings.workspace,
                settings.home_dir,
                self.context.tools,
                skills=self.skills,
                sandbox=self.context.tool_context.sandbox,
                memory_dir=self.context.tool_context.memory_dir,
            ),
        }
        messages = self.context.messages
        if messages and messages[0].get("role") == "system":
            messages[0] = system_message
        else:
            messages.insert(0, system_message)

    async def run_turn(self, user_input: str | dict[str, Any]) -> str:
        """user_input 是一句话，或者 attachments.build_user_message 拼好的带附件的 user 消息。"""
        context = self.context
        context.turn = TurnState()
        message = self._expand_skill_command(_as_user_message(user_input))
        context.messages.append(message)
        display = message_display(message)
        await context.events.emit(
            EventType.TURN_START,
            session_id=self.session.id,
            user_text=display["text"],
            attachments=display["attachments"],
        )
        # 拍检查点要读一遍工作区（大工作区要几百毫秒），和第一次调用模型同时做，不让用户干等
        context.pending_checkpoint = asyncio.create_task(self._checkpoint_turn_start(display["text"]))
        self.turn_running = True
        try:
            await self.flow.run_async(context)
            await context.wait_for_turn_checkpoint()
            await apply_finished_compaction(context)
        except (asyncio.CancelledError, KeyboardInterrupt):
            context.turn.stop_reason = "interrupted"
            close_dangling_tool_calls(context.messages)
            raise
        except Exception as error:
            context.turn.stop_reason = "error"
            close_dangling_tool_calls(context.messages)
            await context.events.emit(EventType.ERROR, message=f"{type(error).__name__}: {error}")
        finally:
            self.turn_running = False
            self.session_store.save(self.session)
            self._apply_compaction_when_done()
            await context.events.emit(
                EventType.TURN_END, stop_reason=context.turn.stop_reason, final_text=context.turn.final_text
            )
        return context.turn.final_text

    async def _checkpoint_turn_start(self, user_text: str) -> None:
        """这一轮动手之前给工作区拍一张；第一张就是「任务开始时的样子」，变更都和它比。"""
        was_enabled = self.checkpoints.enabled
        is_first = self.checkpoints.baseline() is None
        label = "任务开始时" if is_first else f"这一轮之前：{user_text.strip()[:30]}"
        try:
            checkpoint = await self.checkpoints.create(label, kind="baseline" if is_first else "turn")
        except Exception as error:
            # 检查点只是辅助，出什么错都不能挡住这一轮
            self.checkpoints.enabled = False
            self.checkpoints.disabled_reason = f"检查点关掉了：{type(error).__name__}: {error}"
            checkpoint = None
        if checkpoint is not None:
            await self.context.events.emit(EventType.CHECKPOINT, commit=checkpoint.commit, label=checkpoint.label)
        elif was_enabled:
            await self.context.events.emit(EventType.ERROR, message=self.checkpoints.disabled_reason)

    async def interject(self, user_input: str | dict[str, Any]) -> None:
        """agent 干活时用户插话：下一次调用模型前放进对话，模型就能看到。"""
        message = self._expand_skill_command(_as_user_message(user_input))
        self.context.pending_interjections.append(message)
        display = message_display(message)
        await self.context.events.emit(
            EventType.INTERJECTION_QUEUED, user_text=display["text"], attachments=display["attachments"]
        )

    async def compact(self) -> None:
        """手动压缩并等它做完（命令行 /compact 用）。"""
        await start_background_compaction(self.context, reason="manual")
        await wait_for_compaction(self.context)
        self.session_store.save(self.session)

    async def start_compaction(self) -> bool:
        """手动压缩但不等（web 的「压缩」按钮）：后台做摘要，做完自动换上，对话照常进行。"""
        started = await start_background_compaction(self.context, reason="manual")
        if not self.turn_running:
            self._apply_compaction_when_done()
        return started

    def _apply_compaction_when_done(self) -> None:
        """一轮结束时后台压缩还没做完：等它做完、而且没在跑新的一轮时，直接换上摘要并存盘。"""
        pending = self.context.pending_compaction
        if pending is None or pending.task.done():
            return
        pending.task.add_done_callback(self._on_compaction_done)

    def _on_compaction_done(self, finished_task: asyncio.Task) -> None:
        finished_task.get_loop().create_task(self._apply_if_idle())

    async def _apply_if_idle(self) -> None:
        # 正在跑的一轮会在下一步的 GuardNode 里自己换上
        if self.turn_running:
            return
        if await apply_finished_compaction(self.context):
            self.session_store.save(self.session)

    def replace_provider(self, provider: LLMProvider) -> None:
        """换模型时用：新的 provider 同样包上用量记录。"""
        self.context.provider = _with_usage_recording(provider, self.usage_log, self.context.settings, self.session)

    def _expand_skill_command(self, message: dict[str, Any]) -> dict[str, Any]:
        """消息以 /技能名 开头：把技能的完整说明接在用户的话后面，界面上还是只显示原话。"""
        display = message_display(message)
        command = find_skill_command(display["text"], self.skills)
        if command is None:
            return message
        skill, _ = command
        try:
            content = skill_content(skill)
        except OSError:
            return message
        parts = message["content"] if isinstance(message["content"], list) else [{"type": "text", "text": message["content"]}]
        return {**message, "content": [*parts, {"type": "text", "text": content}], DISPLAY_KEY: display}

    def reload_tools(self) -> None:
        """设置变了（联网、沙箱、技能）：工具、工具环境、系统提示都重新装一遍，下一次调用模型时生效。"""
        settings = self.context.settings
        self.skills = enabled_skills(settings)
        self.context.tools = build_tool_registry(settings, self.skills)
        self.context.tool_context = build_tool_context(settings, self.session, self.skills)
        self.refresh_system_prompt()


def _as_user_message(user_input: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(user_input, str):
        return {"role": "user", "content": user_input}
    return user_input


def _with_usage_recording(
    provider: LLMProvider, usage_log: UsageLog | None, settings: Settings, session: Session
) -> LLMProvider:
    if usage_log is None:
        return provider
    metadata = UsageMetadata(
        model=settings.model,
        model_name=settings.model_profile_name,
        model_id=settings.model_profile_id,
        session_id=session.id,
        workspace=str(settings.workspace),
    )
    return UsageRecordingProvider(provider, usage_log, metadata)


def create_agent(
    settings: Settings,
    approver: Approver,
    provider: LLMProvider | None = None,
    tools: ToolRegistry | None = None,
    session: Session | None = None,
    enable_trace: bool = True,
    record_usage: bool = True,
) -> Agent:
    """组装一个 Agent。provider / tools 留空就用默认的（OpenAI 兼容接口 + 内置工具）。

    同时接好：trace（~/.pocketagent/traces）、用量记录（usage.jsonl）、影子 git 检查点（shadow/<工作区>）、
    附件目录和技能目录（只读工具不用确认就能读）、bash 沙箱。
    """
    session_store = SessionStore(settings.sessions_dir)
    if session is None:
        session = session_store.new_session(settings.workspace)
    session.model_profile = settings.model_profile_id
    session.approval_mode = settings.approval_mode.value
    if provider is None:
        provider = create_provider(settings)
    usage_log = UsageLog(settings.usage_log_path) if record_usage else None

    events = EventBus()
    if enable_trace:
        events.subscribe(JsonlTracer(settings.traces_dir / f"{session.id}.jsonl"))

    skills = enabled_skills(settings)
    context = AgentContext(
        settings=settings,
        provider=_with_usage_recording(provider, usage_log, settings, session),
        tools=tools or build_tool_registry(settings, skills),
        tool_context=build_tool_context(settings, session, skills),
        permissions=PermissionGate(mode=settings.approval_mode, approver=approver),
        events=events,
        session=session,
        attachments_root=settings.attachments_dir,
    )
    return Agent(context, session_store, usage_log, skills=skills)


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
        reasoning_style=settings.reasoning_style,
        max_output_tokens=settings.max_output_tokens,
    )
