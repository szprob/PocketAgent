"""一个任务（会话）的运行时：WebSession = 一个 Agent。

可以被多个连接（浏览器标签页、桌面端窗口）同时看，事件和审批请求广播给所有连接。
agent 正在干活时用户又发了话，按设置插话（下一次调用模型时带上）或排队（这一轮结束后接着跑）。
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web

from pocketagent.config import BUSY_INPUT_INTERJECT, ModelProfile, Settings
from pocketagent.core.agent import Agent, create_agent
from pocketagent.core.events import Event, EventType, json_default
from pocketagent.core.session import Session
from pocketagent.interaction.attachments import AttachmentStore, build_user_message, message_display
from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.llm.base import LLMProvider
from pocketagent.safety.checkpoints import TaskCheckpoints
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalMode, ApprovalRequest
from pocketagent.server.history import history_items

ProviderFactory = Callable[[Settings], LLMProvider]


# 这些事件之后状态（state 消息里的字段）变了，要额外推一次 state，各个前端不用自己去改本地副本
STATE_CHANGING_EVENTS = {
    EventType.COMPACTION_STARTED,
    EventType.COMPACTION,
    EventType.COMPACTION_FAILED,
    EventType.INTERJECTION_APPLIED,
}


class SessionBusyError(RuntimeError):
    pass


@dataclass
class PendingApproval:
    """等用户答复的审批：一批调用里要问的合成一张卡，一项时消息和以前一样，多项时放在 items 里。"""

    request_id: str
    requests: list[ApprovalRequest]
    # 答复是每项一个 ApprovalAnswer 的列表
    answer: asyncio.Future

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"type": "approval_request", "request_id": self.request_id}
        if len(self.requests) == 1:
            message.update(_approval_item(self.requests[0]))
        else:
            message["items"] = [_approval_item(request) for request in self.requests]
        return message


def _parse_answer(name: Any) -> ApprovalAnswer:
    try:
        return ApprovalAnswer(str(name))
    except ValueError:
        return ApprovalAnswer.DENY


def _approval_item(request: ApprovalRequest) -> dict[str, Any]:
    return {
        "tool_name": request.tool_name,
        "summary": request.summary,
        "preview": request.preview,
        "notes": request.notes,
        "always_label": request.always_label,
        "always_parents_label": request.always_parents_label,
    }


@dataclass
class PendingQuestions:
    """ask_user 发出的一组问题，等浏览器那边选完提交。"""

    request_id: str
    questions: list[Question]
    answer: asyncio.Future

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "question_request",
            "request_id": self.request_id,
            "questions": [question.to_dict() for question in self.questions],
        }


class WebApprover:
    """Approver 的 web 实现：把审批请求和问题推给浏览器，等用户点按钮。"""

    def __init__(self, web_session: "WebSession") -> None:
        self.web_session = web_session

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        answers = await self.web_session.wait_for_approvals([request])
        return answers[0]

    async def request_approvals(self, requests: list[ApprovalRequest]) -> list[ApprovalAnswer]:
        return await self.web_session.wait_for_approvals(requests)

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        return await self.web_session.wait_for_answers(questions)


class WebSession:
    def __init__(
        self,
        settings: Settings,
        session: Session | None,
        provider: LLMProvider,
        provider_builder: ProviderFactory | None = None,
    ) -> None:
        # provider_builder：审批单独用别的模型时拿它造 provider（和任务的 provider 同一个来源）
        self.agent: Agent = create_agent(
            settings, approver=WebApprover(self), provider=provider, session=session, provider_builder=provider_builder
        )
        self.agent.context.events.subscribe(self._forward_event)
        self.clients: set[web.WebSocketResponse] = set()
        self.pending_approvals: dict[str, PendingApproval] = {}
        self.pending_questions: dict[str, PendingQuestions] = {}
        self.background_task: asyncio.Task | None = None
        # 排队等这一轮结束后再跑的 user 消息
        self.queued_messages: list[dict[str, Any]] = []
        # 自动化任务设一个超时，没人审批就拒绝
        self.approval_timeout_seconds: float | None = None
        self.attachments = AttachmentStore(settings.attachments_dir, self.id)

    @property
    def id(self) -> str:
        return self.agent.session.id

    @property
    def settings(self) -> Settings:
        return self.agent.context.settings

    @property
    def session(self) -> Session:
        return self.agent.session

    @property
    def running(self) -> bool:
        return self.background_task is not None and not self.background_task.done()

    @property
    def waiting_for(self) -> str | None:
        """在等用户什么：question（问了还没答）/ approval（要审批），都不是就是 None。

        这种时候后台任务其实没在干活，只是卡着等人，界面上不该显示成「运行中」。
        """
        if self.pending_questions:
            return "question"
        if self.pending_approvals:
            return "approval"
        return None

    @property
    def mode(self) -> ApprovalMode:
        return self.agent.context.permissions.mode

    @property
    def checkpoints(self) -> TaskCheckpoints:
        return self.agent.checkpoints

    # ---- 推给浏览器 ----

    async def send(self, client: web.WebSocketResponse, message: dict[str, Any]) -> None:
        try:
            await client.send_str(json.dumps(message, ensure_ascii=False, default=json_default))
        except (ConnectionResetError, RuntimeError):
            # 标签页已经关了
            self.clients.discard(client)

    async def broadcast(self, message: dict[str, Any]) -> None:
        for client in list(self.clients):
            await self.send(client, message)

    async def _forward_event(self, event: Event) -> None:
        await self.broadcast({"type": "event", "event": event.to_record()})
        if event.type in STATE_CHANGING_EVENTS:
            # 压缩开始 / 换上摘要会改变「压缩中」和上下文占用，空闲时也要让界面知道；
            # 插话交给模型后要从「待发」里拿掉
            await self.broadcast_state()

    def status_fields(self) -> dict[str, Any]:
        context = self.agent.context
        return {
            "running": self.running,
            "waiting": self.waiting_for,
            "mode": self.mode.value,
            "model_id": self.settings.model_profile_id,
            "model": self.settings.display_model(),
            "title": self.session.display_title(),
            "workspace": self.session.workspace,
            # 临时任务：工作区是 ~/.pocketagent/scratch 下自动建的目录，界面上显示成「临时任务」
            "ephemeral": self.session.ephemeral,
            "context_tokens": context.estimated_context_tokens(),
            "context_window": self.settings.context_window_tokens,
            "compact_threshold": self.settings.compact_threshold if self.settings.auto_compact else None,
            "compacting": context.pending_compaction is not None,
            "queued": [message_display(message) for message in self.queued_messages],
            "interjections": [message_display(message) for message in context.pending_interjections],
        }

    def state_message(self) -> dict[str, Any]:
        return {"type": "state", **self.status_fields()}

    def snapshot_message(self) -> dict[str, Any]:
        """新连接进来时发一次：完整历史 + 当前状态 + 还没答复的审批和问题。"""
        return {
            "type": "snapshot",
            "session_id": self.id,
            **self.status_fields(),
            "items": history_items(self.agent.context.messages, self.agent.context.tools),
            "todos": self.session.todos,
            "pending_approvals": [pending.to_message() for pending in self.pending_approvals.values()],
            "pending_questions": [pending.to_message() for pending in self.pending_questions.values()],
        }

    async def broadcast_state(self) -> None:
        await self.broadcast(self.state_message())

    # ---- 用户发消息：空闲就开一轮，忙着就插话或排队 ----

    def build_message(self, text: str, attachment_ids: list[str]) -> dict[str, Any]:
        attachments = [self.attachments.get(attachment_id) for attachment_id in attachment_ids]
        return build_user_message(text, attachments, self.attachments)

    async def submit(self, message: dict[str, Any], busy_input_mode: str) -> None:
        if not self.running:
            self.start_turn(message)
            return
        if busy_input_mode == BUSY_INPUT_INTERJECT:
            await self.agent.interject(message)
        else:
            self.queued_messages.append(message)
        await self.broadcast_state()

    def remove_queued(self, index: int) -> None:
        if 0 <= index < len(self.queued_messages):
            self.queued_messages.pop(index)

    def start_turn(self, message: dict[str, Any] | str) -> None:
        self._start_background(self.agent.run_turn(message))

    async def start_compaction(self) -> None:
        """「压缩」按钮：在后台做摘要，做完自动换上；不占着这个任务，照样可以接着聊。"""
        if not await self.agent.start_compaction():
            raise SessionBusyError("已经在压缩了，或者历史太短不用压缩")

    def _start_background(self, coroutine: Any) -> None:
        if self.running:
            coroutine.close()
            raise SessionBusyError("上一轮还没结束")
        self.background_task = asyncio.create_task(self._run_and_report(coroutine))

    async def _run_and_report(self, coroutine: Any) -> None:
        await self.broadcast_state()
        try:
            await coroutine
        except asyncio.CancelledError:
            # 用户点了停止；run_turn 已经把历史补齐、存好了
            pass
        except Exception as error:
            await self.broadcast({"type": "error", "message": f"{type(error).__name__}: {error}"})
        finally:
            self.background_task = None
            self._start_next_queued()
            await self.broadcast_state()

    def _start_next_queued(self) -> None:
        """一轮正常结束后接着跑排队的消息；被用户中断的不接着跑（排队的已经退回输入框了）。

        模型刚说完最后一句时用户插的话没赶上这一轮，也放到队首接着跑。
        """
        context = self.agent.context
        if context.pending_interjections:
            self.queued_messages[0:0] = context.pending_interjections
            context.pending_interjections.clear()
        if self.queued_messages and context.turn.stop_reason != "interrupted":
            self.background_task = asyncio.create_task(
                self._run_and_report(self.agent.run_turn(self.queued_messages.pop(0)))
            )

    def cancel_background_work(self) -> None:
        if self.agent.context.pending_compaction is not None:
            self.agent.context.pending_compaction.task.cancel()
            self.agent.context.pending_compaction = None

    async def interrupt(self) -> None:
        """停止这一轮。还没用上的插话和排队的消息退回给浏览器，放回输入框。"""
        returned = [message_display(message) for message in self.queued_messages]
        returned += [message_display(message) for message in self.agent.context.pending_interjections]
        self.queued_messages.clear()
        self.agent.context.pending_interjections.clear()
        if returned:
            await self.broadcast({"type": "returned_inputs", "inputs": returned})
        if self.running:
            self.background_task.cancel()

    # ---- 设置 ----

    async def set_mode(self, mode_name: str) -> None:
        mode = ApprovalMode(mode_name)
        self.agent.context.permissions.mode = mode
        self.settings.approval_mode = mode
        self.session.approval_mode = mode.value
        await self.broadcast_state()

    async def use_model(self, profile: ModelProfile, build_provider: ProviderFactory) -> None:
        """换模型。跑到一半也能换，下一次调用模型时生效。"""
        self.settings.use_profile(profile)
        self.agent.replace_provider(build_provider(self.settings))
        self.session.model_profile = profile.id
        await self.broadcast_state()

    async def set_title(self, title: str) -> None:
        self.session.title = title.strip()[:100] or None
        self.agent.session_store.save(self.session)
        await self.broadcast_state()

    # ---- 审批 ----

    async def wait_for_approvals(self, requests: list[ApprovalRequest]) -> list[ApprovalAnswer]:
        """推一张审批卡给所有标签页，等任意一个答复；自动化任务有超时，没人答就全部拒绝。"""
        pending = PendingApproval(
            request_id=uuid.uuid4().hex, requests=requests, answer=asyncio.get_running_loop().create_future()
        )
        self.pending_approvals[pending.request_id] = pending
        await self.broadcast(pending.to_message())
        try:
            if self.approval_timeout_seconds is None:
                return await pending.answer
            return await asyncio.wait_for(pending.answer, timeout=self.approval_timeout_seconds)
        except asyncio.TimeoutError:
            return [ApprovalAnswer.DENY] * len(requests)
        finally:
            # 答复了、超时了或者被中断了，都让各个标签页收起这张审批卡片
            self.pending_approvals.pop(pending.request_id, None)
            await self.broadcast({"type": "approval_resolved", "request_id": pending.request_id})

    def answer_approval(self, request_id: str, answer: str | None = None, answers: list[str] | None = None) -> None:
        """一项的卡片回 answer，合并的卡片回 answers（每项一个）；对不上的项按拒绝算。"""
        pending = self.pending_approvals.get(request_id)
        if pending is None or pending.answer.done():
            return
        names = answers if answers is not None else [answer or ApprovalAnswer.DENY.value]
        parsed = [_parse_answer(name) for name in names]
        parsed += [ApprovalAnswer.DENY] * (len(pending.requests) - len(parsed))
        pending.answer.set_result(parsed[: len(pending.requests)])

    async def wait_for_answers(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        """问题推给所有标签页，等任意一个提交；自动化任务有超时，没人答就当跳过。"""
        pending = PendingQuestions(
            request_id=uuid.uuid4().hex, questions=questions, answer=asyncio.get_running_loop().create_future()
        )
        self.pending_questions[pending.request_id] = pending
        await self.broadcast(pending.to_message())
        try:
            if self.approval_timeout_seconds is None:
                return await pending.answer
            return await asyncio.wait_for(pending.answer, timeout=self.approval_timeout_seconds)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending_questions.pop(pending.request_id, None)
            await self.broadcast({"type": "question_resolved", "request_id": pending.request_id})

    def answer_questions(self, request_id: str, answers: Any) -> None:
        """answers 是每个问题的 {selected: [...], other: "..."}；None 表示跳过。"""
        pending = self.pending_questions.get(request_id)
        if pending is None or pending.answer.done():
            return
        if answers is None:
            pending.answer.set_result(None)
            return
        pending.answer.set_result([QuestionAnswer.from_dict(item) for item in answers])
