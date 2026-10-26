"""web 端的会话运行时：每个会话一个 Agent，同一会话可以被多个浏览器标签页同时看。

agent 发出的事件、审批请求都广播给这个会话的所有连接；任何一个标签页点了审批按钮都算数。
每个会话各有一份 Settings，切模型只影响自己；保存配置后所有会话按新配置重建 provider。
"""

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web

from pocketagent.agent import Agent, create_agent, create_provider
from pocketagent.config import AppConfig, ModelProfile, Settings
from pocketagent.events import Event, json_default
from pocketagent.llm.base import LLMProvider, SilentListener
from pocketagent.permissions import ApprovalAnswer, ApprovalMode, ApprovalRequest
from pocketagent.session import Session, SessionStore
from pocketagent.web.history import history_items

ProviderFactory = Callable[[Settings], LLMProvider]

CONNECTION_TEST_PROMPT = "这是一次连通性测试，请只回复两个字：收到"
CONNECTION_TEST_TIMEOUT_SECONDS = 60


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

    @property
    def id(self) -> str:
        return self.agent.session.id

    @property
    def settings(self) -> Settings:
        return self.agent.context.settings

    @property
    def running(self) -> bool:
        return self.background_task is not None and not self.background_task.done()

    @property
    def mode(self) -> ApprovalMode:
        return self.agent.context.permissions.mode

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
        return {
            "running": self.running,
            "mode": self.mode.value,
            "model_id": self.settings.model_profile_id,
            "model": self.settings.display_model(),
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

    # ---- 浏览器发来的操作 ----

    def start_turn(self, user_text: str) -> None:
        self._start_background(self.agent.run_turn(user_text))

    def start_compaction(self) -> None:
        self._start_background(self._compact_and_resend_history())

    def _start_background(self, coroutine: Any) -> None:
        if self.running:
            coroutine.close()
            raise SessionBusyError("上一轮还没结束")
        self.background_task = asyncio.create_task(self._run_and_report(coroutine))

    async def _run_and_report(self, coroutine: Any) -> None:
        await self.broadcast(self.state_message())
        try:
            await coroutine
        except asyncio.CancelledError:
            # 用户点了停止；run_turn 已经把历史补齐、存好了
            pass
        except Exception as error:
            await self.broadcast({"type": "error", "message": f"{type(error).__name__}: {error}"})
        finally:
            self.background_task = None
            await self.broadcast(self.state_message())

    async def _compact_and_resend_history(self) -> None:
        await self.agent.compact()
        await self.broadcast(self.snapshot_message())

    def interrupt(self) -> None:
        if self.running:
            self.background_task.cancel()

    async def set_mode(self, mode_name: str) -> None:
        self.agent.context.permissions.mode = ApprovalMode(mode_name)
        await self.broadcast(self.state_message())

    async def use_model(self, profile: ModelProfile, build_provider: ProviderFactory) -> None:
        """换模型。跑到一半也能换，下一次调用模型时生效。"""
        self.settings.use_profile(profile)
        self.agent.context.provider = build_provider(self.settings)
        self.agent.session.model_profile = profile.id
        await self.broadcast(self.state_message())

    # ---- 审批 ----

    async def wait_for_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        pending = PendingApproval(
            request_id=uuid.uuid4().hex, request=request, answer=asyncio.get_running_loop().create_future()
        )
        self.pending_approvals[pending.request_id] = pending
        await self.broadcast(pending.to_message())
        try:
            return await pending.answer
        finally:
            # 答复了或者被中断了，都让各个标签页收起这张审批卡片
            self.pending_approvals.pop(pending.request_id, None)
            await self.broadcast({"type": "approval_resolved", "request_id": pending.request_id})

    def answer_approval(self, request_id: str, answer_name: str) -> None:
        pending = self.pending_approvals.get(request_id)
        if pending is None or pending.answer.done():
            return
        pending.answer.set_result(ApprovalAnswer(answer_name))


class SessionManager:
    """管理这个服务里的所有会话和配置：会话内存里有就直接用，没有就从磁盘加载。"""

    def __init__(
        self, base_settings: Settings, app_config: AppConfig, provider_factory: ProviderFactory | None = None
    ) -> None:
        self.base_settings = base_settings
        self.app_config = app_config
        self.provider_factory = provider_factory
        self.store = SessionStore(base_settings.sessions_dir)
        self.sessions: dict[str, WebSession] = {}

    # ---- provider 和设置 ----

    def build_provider(self, settings: Settings) -> LLMProvider:
        if self.provider_factory is not None:
            return self.provider_factory(settings)
        return create_provider(settings)

    def _settings_for(self, model_key: str | None) -> Settings:
        """以启动时的设置为底，套上指定的模型（找不到就用默认模型）。"""
        settings = copy.deepcopy(self.base_settings)
        profile = self.app_config.find_model(model_key) or self.app_config.default_profile()
        if profile is not None:
            settings.use_profile(profile)
        return settings

    def _new_web_session(self, settings: Settings, session: Session | None) -> WebSession:
        web_session = WebSession(settings, session=session, provider=self.build_provider(settings))
        self.sessions[web_session.id] = web_session
        return web_session

    # ---- 会话 ----

    def create(self) -> WebSession:
        return self._new_web_session(self._settings_for(None), session=None)

    def get(self, session_id: str) -> WebSession:
        """找不到时抛 KeyError。"""
        if session_id in self.sessions:
            return self.sessions[session_id]
        if not self.store.path_for(session_id).is_file():
            raise KeyError(session_id)
        session = self.store.load(session_id)
        return self._new_web_session(self._settings_for(session.model_profile), session=session)

    def list_saved(self) -> list[dict[str, Any]]:
        summaries = []
        for session in self.store.list_sessions(self.base_settings.workspace):
            first_text = session.first_user_text().strip()
            title = first_text.splitlines()[0] if first_text else "（空会话）"
            summaries.append({"id": session.id, "title": title[:80], "updated_at": session.updated_at})
        return summaries

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
        """把设置面板提交的内容转成 AppConfig，校验不过抛 ConfigError。"""
        data = dict(payload)
        data["models"] = self._fill_kept_api_keys(payload.get("models") or [])
        data["web_token"] = self.app_config.web_token
        return AppConfig.from_dict(data)

    async def apply_config(self, new_config: AppConfig) -> None:
        new_config.save(self.base_settings.config_path)
        self.app_config = new_config
        self.base_settings.approval_mode = new_config.approval_mode
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

    async def shutdown(self) -> None:
        for web_session in self.sessions.values():
            web_session.interrupt()
            for client in list(web_session.clients):
                await client.close()
