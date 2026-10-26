"""web 端的会话运行时。

- WebSession：一个任务（会话）= 一个 Agent。可以被多个浏览器标签页同时看，事件和审批请求广播给所有连接。
  agent 正在干活时用户又发了话，按设置插话（下一次调用模型时带上）或排队（这一轮结束后接着跑）。
- SessionManager：所有任务、工作区、配置、用量；任务可以属于不同的工作区，各有一份 Settings。
- AutomationScheduler：定时任务的调度。
"""

import asyncio
import copy
import functools
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from pocketagent.agent import Agent, create_agent, create_provider
from pocketagent.attachments import AttachmentStore, build_user_message, message_display
from pocketagent.automations import Automation, AutomationStore
from pocketagent.changes import ChangeTracker
from pocketagent.compaction import estimate_tokens
from pocketagent.config import BUSY_INPUT_INTERJECT, AppConfig, ConfigError, ModelProfile, Settings
from pocketagent.events import Event, json_default
from pocketagent.llm.base import LLMProvider, SilentListener
from pocketagent.permissions import ApprovalAnswer, ApprovalMode, ApprovalRequest
from pocketagent.session import Session, SessionStore
from pocketagent.usage import UsageLog
from pocketagent.web.history import history_items

ProviderFactory = Callable[[Settings], LLMProvider]

CONNECTION_TEST_PROMPT = "这是一次连通性测试，请只回复两个字：收到"
CONNECTION_TEST_TIMEOUT_SECONDS = 60
# 自动化任务没人盯着，AI 审批不放行的操作最多等用户这么久，超时就拒绝
AUTOMATION_APPROVAL_TIMEOUT_SECONDS = 600
SCHEDULER_TICK_SECONDS = 20


class SessionBusyError(RuntimeError):
    pass


@dataclass
class PendingApproval:
    request_id: str
    request: ApprovalRequest
    answer: asyncio.Future

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "approval_request",
            "request_id": self.request_id,
            "tool_name": self.request.tool_name,
            "summary": self.request.summary,
            "preview": self.request.preview,
        }


class WebApprover:
    """Approver 的 web 实现：把请求推给浏览器，等用户点按钮。"""

    def __init__(self, web_session: "WebSession") -> None:
        self.web_session = web_session

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        return await self.web_session.wait_for_approval(request)


class WebSession:
    def __init__(self, settings: Settings, session: Session | None, provider: LLMProvider) -> None:
        self.agent: Agent = create_agent(settings, approver=WebApprover(self), provider=provider, session=session)
        self.agent.context.events.subscribe(self._forward_event)
        self.clients: set[web.WebSocketResponse] = set()
        self.pending_approvals: dict[str, PendingApproval] = {}
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
    def mode(self) -> ApprovalMode:
        return self.agent.context.permissions.mode

    @property
    def change_tracker(self) -> ChangeTracker:
        return self.agent.context.tool_context.write_observer

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

    def _status_fields(self) -> dict[str, Any]:
        context = self.agent.context
        return {
            "running": self.running,
            "mode": self.mode.value,
            "model_id": self.settings.model_profile_id,
            "model": self.settings.display_model(),
            "title": self.session.display_title(),
            "workspace": self.session.workspace,
            "context_tokens": max(estimate_tokens(context.messages), context.last_prompt_tokens),
            "context_window": self.settings.context_window_tokens,
            "queued": [message_display(message) for message in self.queued_messages],
            "interjections": [message_display(message) for message in context.pending_interjections],
        }

    def state_message(self) -> dict[str, Any]:
        return {"type": "state", **self._status_fields()}

    def snapshot_message(self) -> dict[str, Any]:
        """新连接进来时发一次：完整历史 + 当前状态 + 还没答复的审批。"""
        return {
            "type": "snapshot",
            "session_id": self.id,
            **self._status_fields(),
            "items": history_items(self.agent.context.messages, self.agent.context.tools),
            "pending_approvals": [pending.to_message() for pending in self.pending_approvals.values()],
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

    def start_compaction(self) -> None:
        self._start_background(self._compact_and_resend_history())

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

    async def _compact_and_resend_history(self) -> None:
        await self.agent.compact()
        await self.broadcast(self.snapshot_message())

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

    async def wait_for_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        pending = PendingApproval(
            request_id=uuid.uuid4().hex, request=request, answer=asyncio.get_running_loop().create_future()
        )
        self.pending_approvals[pending.request_id] = pending
        await self.broadcast(pending.to_message())
        try:
            if self.approval_timeout_seconds is None:
                return await pending.answer
            return await asyncio.wait_for(pending.answer, timeout=self.approval_timeout_seconds)
        except asyncio.TimeoutError:
            return ApprovalAnswer.DENY
        finally:
            # 答复了、超时了或者被中断了，都让各个标签页收起这张审批卡片
            self.pending_approvals.pop(pending.request_id, None)
            await self.broadcast({"type": "approval_resolved", "request_id": pending.request_id})

    def answer_approval(self, request_id: str, answer_name: str) -> None:
        pending = self.pending_approvals.get(request_id)
        if pending is None or pending.answer.done():
            return
        pending.answer.set_result(ApprovalAnswer(answer_name))


class SessionManager:
    """这个服务里的所有任务、工作区、配置和用量。任务在内存里有就直接用，没有就从磁盘加载。"""

    def __init__(
        self, base_settings: Settings, app_config: AppConfig, provider_factory: ProviderFactory | None = None
    ) -> None:
        self.base_settings = base_settings
        self.app_config = app_config
        self.provider_factory = provider_factory
        self.store = SessionStore(base_settings.sessions_dir)
        self.usage_log = UsageLog(base_settings.usage_log_path)
        self.automations = AutomationStore(base_settings.home_dir / "automations.json")
        self.sessions: dict[str, WebSession] = {}

    # ---- provider 和设置 ----

    def build_provider(self, settings: Settings) -> LLMProvider:
        if self.provider_factory is not None:
            return self.provider_factory(settings)
        return create_provider(settings)

    def _settings_for(self, workspace: Path, model_key: str | None, mode_name: str | None) -> Settings:
        """以启动时的设置为底，换上这个任务的工作区、模型（找不到就用默认模型）和审批模式。"""
        settings = copy.deepcopy(self.base_settings)
        settings.workspace = workspace
        settings.max_steps = self.app_config.max_steps
        settings.approval_mode = ApprovalMode(mode_name) if mode_name else self.app_config.approval_mode
        profile = self.app_config.find_model(model_key) or self.app_config.default_profile()
        if profile is not None:
            settings.use_profile(profile)
        return settings

    def _register(self, settings: Settings, session: Session | None) -> WebSession:
        web_session = WebSession(settings, session=session, provider=self.build_provider(settings))
        self.sessions[web_session.id] = web_session
        return web_session

    # ---- 工作区 ----

    def default_workspace(self) -> Path:
        if self.app_config.workspaces:
            return Path(self.app_config.workspaces[0])
        return self.base_settings.workspace

    def workspace_list(self) -> list[dict[str, Any]]:
        return [
            {"path": path, "name": Path(path).name or path, "exists": Path(path).is_dir()}
            for path in self.app_config.workspaces
        ]

    def add_workspace(self, raw_path: str) -> None:
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            raise ConfigError(f"目录不存在：{raw_path}")
        if self.app_config.add_workspace(path):
            self.app_config.save(self.base_settings.config_path)

    def remove_workspace(self, raw_path: str) -> None:
        """只从列表里拿掉，这个工作区的历史任务不删。"""
        resolved = str(Path(raw_path).expanduser().resolve())
        self.app_config.workspaces = [path for path in self.app_config.workspaces if path != resolved]
        self.app_config.save(self.base_settings.config_path)

    # ---- 任务 ----

    def create(
        self,
        workspace: Path,
        model_id: str | None = None,
        mode_name: str | None = None,
        title: str | None = None,
        source: str | None = None,
    ) -> WebSession:
        if not workspace.is_dir():
            raise ConfigError(f"工作区不存在：{workspace}")
        web_session = self._register(self._settings_for(workspace.resolve(), model_id, mode_name), session=None)
        web_session.session.title = title
        web_session.session.source = source
        return web_session

    def get(self, session_id: str) -> WebSession:
        """找不到时抛 KeyError。"""
        if session_id in self.sessions:
            return self.sessions[session_id]
        if not self.store.exists(session_id):
            raise KeyError(session_id)
        session = self.store.load(session_id)
        settings = self._settings_for(Path(session.workspace), session.model_profile, session.approval_mode)
        return self._register(settings, session=session)

    def list_tasks(self) -> list[dict[str, Any]]:
        """所有存过盘的任务（按最近更新排序），加上内存里正在跑但还没存盘的。"""
        tasks = []
        seen_ids = set()
        for session in self.store.list_sessions():
            seen_ids.add(session.id)
            live = self.sessions.get(session.id)
            tasks.append(self._task_summary(live.session if live else session, live))
        for live in self.sessions.values():
            if live.id not in seen_ids and live.running:
                tasks.insert(0, self._task_summary(live.session, live))
        return tasks

    @staticmethod
    def _task_summary(session: Session, live: WebSession | None) -> dict[str, Any]:
        return {
            "id": session.id,
            "title": session.display_title(),
            "workspace": session.workspace,
            "updated_at": session.updated_at,
            "source": session.source,
            "running": bool(live and live.running),
        }

    def delete_task(self, session_id: str) -> None:
        live = self.sessions.get(session_id)
        if live is not None and live.running:
            raise SessionBusyError("任务还在运行，先停止再删除")
        self.sessions.pop(session_id, None)
        self.store.delete(session_id)
        settings = self.base_settings
        (settings.traces_dir / f"{session_id}.jsonl").unlink(missing_ok=True)
        shutil.rmtree(settings.checkpoints_dir / session_id, ignore_errors=True)
        shutil.rmtree(settings.attachments_dir / session_id, ignore_errors=True)

    async def switch_model(self, web_session: WebSession, model_id: str) -> None:
        profile = self.app_config.find_model(model_id)
        if profile is None:
            raise ValueError(f"没有这个模型：{model_id}")
        await web_session.use_model(profile, self.build_provider)

    # ---- 配置 ----

    def _fill_kept_api_keys(self, model_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """网页上密钥框填了就用新的；留空表示「不改」，从现有配置里按 id 补回来；点了清除就清空。"""
        existing_keys = {profile.id: profile.api_key for profile in self.app_config.models}
        filled = []
        for item in model_items:
            item = dict(item)
            if item.get("api_key"):
                pass
            elif item.get("clear_api_key"):
                item["api_key"] = None
            else:
                item["api_key"] = existing_keys.get(item.get("id"))
            filled.append(item)
        return filled

    def config_from_form(self, payload: dict[str, Any]) -> AppConfig:
        """把设置面板提交的内容转成 AppConfig，校验不过抛 ConfigError。表单里没有的字段沿用现有配置。"""
        data = self.app_config.to_dict()
        data.update(payload)
        data["models"] = self._fill_kept_api_keys(payload.get("models", data["models"]))
        data["web_token"] = self.app_config.web_token
        data["workspaces"] = self.app_config.workspaces
        return AppConfig.from_dict(data)

    async def apply_config(self, new_config: AppConfig) -> None:
        new_config.save(self.base_settings.config_path)
        self.app_config = new_config
        self.base_settings.max_steps = new_config.max_steps
        for web_session in self.sessions.values():
            web_session.settings.max_steps = new_config.max_steps
            profile = new_config.find_model(web_session.settings.model_profile_id) or new_config.default_profile()
            if profile is not None:
                await web_session.use_model(profile, self.build_provider)

    async def test_model(self, model_item: dict[str, Any]) -> dict[str, Any]:
        """用表单里（还没保存）的模型配置真的调一次模型。"""
        profile = ModelProfile.from_dict(self._fill_kept_api_keys([model_item])[0])
        settings = copy.deepcopy(self.base_settings)
        settings.use_profile(profile)
        provider = self.build_provider(settings)
        messages = [{"role": "user", "content": CONNECTION_TEST_PROMPT}]
        started_at = time.monotonic()
        try:
            response = await asyncio.wait_for(
                provider.complete(messages, [], SilentListener()), timeout=CONNECTION_TEST_TIMEOUT_SECONDS
            )
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}
        reply = (response.text or response.reasoning).strip()
        return {"ok": True, "reply": reply[:200], "seconds": round(time.monotonic() - started_at, 1)}

    # ---- 自动化 ----

    def automation_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for automation in self.automations.list_all():
            status = automation.last_status
            live = self.sessions.get(automation.last_session_id or "")
            if live is not None and live.running:
                status = "running"
            summaries.append(
                {
                    **automation.to_dict(),
                    "schedule_text": automation.describe_schedule(),
                    "next_run_at": automation.next_run_at().isoformat(timespec="minutes") if automation.enabled else None,
                    "last_status": status,
                }
            )
        return summaries

    def run_automation(self, automation_id: str) -> WebSession:
        automation = self.automations.get(automation_id)
        web_session = self.create(
            Path(automation.workspace),
            model_id=automation.model_id,
            mode_name=automation.approval_mode,
            title=automation.name,
            source=f"automation:{automation.id}",
        )
        web_session.approval_timeout_seconds = AUTOMATION_APPROVAL_TIMEOUT_SECONDS
        web_session.start_turn(automation.prompt)
        automation.last_run_at = datetime.now().isoformat(timespec="seconds")
        automation.last_session_id = web_session.id
        automation.last_status = "running"
        self.automations.save(automation)
        web_session.background_task.add_done_callback(
            functools.partial(self._record_automation_result, automation.id, web_session)
        )
        return web_session

    def _record_automation_result(self, automation_id: str, web_session: WebSession, task: asyncio.Task) -> None:
        try:
            automation = self.automations.get(automation_id)
        except KeyError:
            return
        automation.last_status = web_session.agent.context.turn.stop_reason or "done"
        self.automations.save(automation)

    def _automation_is_running(self, automation: Automation) -> bool:
        live = self.sessions.get(automation.last_session_id or "")
        return live is not None and live.running

    async def run_due_automations(self, now: datetime) -> None:
        for automation in self.automations.list_all():
            if automation.is_due(now) and not self._automation_is_running(automation):
                try:
                    self.run_automation(automation.id)
                except (ConfigError, KeyError) as error:
                    automation.last_run_at = now.isoformat(timespec="seconds")
                    automation.last_status = f"error: {error}"
                    self.automations.save(automation)

    async def run_scheduler(self) -> None:
        while True:
            await self.run_due_automations(datetime.now())
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    async def shutdown(self) -> None:
        for web_session in self.sessions.values():
            await web_session.interrupt()
            for client in list(web_session.clients):
                await client.close()
