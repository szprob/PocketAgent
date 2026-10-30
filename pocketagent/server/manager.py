"""SessionManager：这个服务里的所有任务、工作区、配置、用量，以及自动化的调度。

任务可以属于不同的工作区，各有一份 Settings；单个任务的运行时见 session.py（WebSession）。
「什么时候跑」交给 TriggerEngine（triggers/engine.py），这里只负责被它叫起来开任务。
"""

import asyncio
import copy
import functools
import shutil
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.account.usage import UsageLog
from pocketagent.config import AppConfig, ConfigError, ModelProfile, Settings
from pocketagent.core.agent import close_shared_mcp_managers, create_provider, shared_mcp_manager
from pocketagent.core.events import Event, EventType
from pocketagent.tools.mcp import McpError, McpManager, save_mcp_servers
from pocketagent.core.scratch import create_scratch_workspace, ensure_scratch_workspace
from pocketagent.core.session import Session, SessionStore
from pocketagent.core.tool_outputs import session_directory
from pocketagent.knowledge.frontmatter import split_frontmatter
from pocketagent.knowledge.memory import delete_memory, list_memories, memory_file_path, project_memory_dir, read_index
from pocketagent.knowledge.skills import discover_skills, skill_directories
from pocketagent.interaction.modes import parse_agent_mode
from pocketagent.llm.base import LLMProvider, SilentListener
from pocketagent.safety.permissions import ApprovalMode
from pocketagent.server.automations import Automation, AutomationStore
from pocketagent.server.session import (
    ProviderFactory,
    SessionBusyError,
    WebSession,
)
from pocketagent.server.triggers.engine import TriggerEngine
from pocketagent.server.triggers.model import Occurrence, SOURCE_TASK_END
from pocketagent.server.worktrees import create_worktree

CONNECTION_TEST_PROMPT = "这是一次连通性测试，请只回复两个字：收到"
CONNECTION_TEST_TIMEOUT_SECONDS = 60
# 自动化任务没人盯着，AI 审批不放行的操作最多等用户这么久，超时就拒绝
AUTOMATION_APPROVAL_TIMEOUT_SECONDS = 600
SCHEDULER_TICK_SECONDS = 20


def _overview_with_problems(overview: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """连接问题的追加到配置问题后面，界面上一并显示。"""
    if problems:
        overview["problems"] = list(overview.get("problems") or []) + problems
    return overview


def _automation_id_from_source(source: str | None) -> str:
    """任务的 source 是 automation:<id> 时把 id 取出来；普通任务是空串。"""
    if not source or not source.startswith("automation:"):
        return ""
    return source.split(":", 1)[1]


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
        self.triggers = TriggerEngine(
            automations=self.automations,
            start_run=self._start_triggered_run,
            is_running=self._automation_is_running,
        )
        self.sessions: dict[str, WebSession] = {}
        # 触发器开出来的后台任务：留着引用，别让它们在半路被回收
        self.trigger_tasks: set[asyncio.Task] = set()

    # ---- provider 和设置 ----

    def build_provider(self, settings: Settings) -> LLMProvider:
        if self.provider_factory is not None:
            return self.provider_factory(settings)
        return create_provider(settings)

    def _settings_for(
        self,
        workspace: Path,
        model_key: str | None,
        mode_name: str | None,
        ephemeral: bool = False,
        worktree: dict[str, Any] | None = None,
    ) -> Settings:
        """以启动时的设置为底，换上这个任务的工作区、模型（找不到就用默认模型）和审批模式。"""
        settings = copy.deepcopy(self.base_settings)
        settings.workspace = workspace
        settings.ephemeral = ephemeral
        settings.worktree = copy.deepcopy(worktree)
        settings.apply_app_config(self.app_config)
        settings.approval_mode = ApprovalMode(mode_name) if mode_name else self.app_config.approval_mode
        profile = self.app_config.find_model(model_key) or self.app_config.default_profile()
        if profile is not None:
            settings.use_profile(profile)
        return settings

    def _register(self, settings: Settings, session: Session | None) -> WebSession:
        web_session = WebSession(
            settings, session=session, provider=self.build_provider(settings), provider_builder=self.build_provider
        )
        self.sessions[web_session.id] = web_session
        # 每一轮结束时问一次触发器：有人配了「某个任务跑完就跑另一个」，就在这里接上
        web_session.agent.context.events.subscribe(functools.partial(self._on_agent_event, web_session))
        return web_session

    # ---- 工作区 ----

    def default_workspace(self) -> Path:
        configured = self.app_config.resolved_default_workspace()
        return Path(configured) if configured else self.base_settings.workspace

    def workspace_list(self) -> list[dict[str, Any]]:
        return [
            {"path": path, "name": Path(path).name or path, "exists": Path(path).is_dir()}
            for path in self.app_config.workspaces
        ]

    def file_workspaces(self) -> list[str]:
        """文件树还能读已有任务的 worktree，但不把它们加进全局工作区列表。"""
        paths = list(self.app_config.workspaces)
        for summary in self.store.summaries():
            if summary.worktree and summary.workspace not in paths:
                paths.append(summary.workspace)
        for session_id, live in self.sessions.items():
            worktree = live.session.worktree
            if worktree and live.session.workspace not in paths:
                paths.append(live.session.workspace)
        return paths

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
        if self.app_config.default_workspace == resolved:
            self.app_config.default_workspace = None
        self.app_config.save(self.base_settings.config_path)

    def set_default_workspace(self, raw_path: str) -> None:
        """把列表里的某个目录设成新任务的默认工作区，存进配置。"""
        self.app_config.set_default_workspace(raw_path)
        self.app_config.save(self.base_settings.config_path)

    # ---- 任务 ----

    def create(
        self,
        workspace: Path | None = None,
        model_id: str | None = None,
        mode_name: str | None = None,
        title: str | None = None,
        source: str | None = None,
        ephemeral: bool = False,
        agent_mode: str | None = None,
    ) -> WebSession:
        """ephemeral=True 表示不指定工作区；要不要独立 worktree 按设置自动决定（worktree_foreground）。"""
        if ephemeral:
            workspace = create_scratch_workspace(self.base_settings.home_dir)
        if workspace is None:
            raise ConfigError("没有指定工作区")
        if not workspace.is_dir():
            raise ConfigError(f"工作区不存在：{workspace}")
        workspace = workspace.resolve()
        session = None
        if not ephemeral and self.app_config.worktree_foreground:
            session = self.store.new_session(workspace)
            try:
                worktree_info = create_worktree(workspace, self.base_settings.home_dir, session.id)
            except ConfigError as error:
                # 自动隔离失败不该让任务开不出来：退回就地编辑，概要里标一下原因
                session.worktree_fallback = str(error)
            else:
                session.worktree = worktree_info.to_dict()
                session.workspace = worktree_info.path
                workspace = Path(worktree_info.path)
        if session is None:
            session = self.store.new_session(workspace, ephemeral=ephemeral)
        try:
            session.agent_mode = parse_agent_mode(agent_mode).value
        except ValueError as error:
            raise ConfigError(str(error))
        settings = self._settings_for(
            workspace, model_id, mode_name, ephemeral=ephemeral, worktree=session.worktree
        )
        web_session = self._register(settings, session=session)
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
        workspace = Path(session.workspace)
        if session.ephemeral:
            # 用户可能已经把 scratch 清掉了，重建一个空的，别让工具全报「目录不存在」
            workspace = ensure_scratch_workspace(workspace)
        settings = self._settings_for(
            workspace, session.model_profile, session.approval_mode, session.ephemeral, worktree=session.worktree
        )
        return self._register(settings, session=session)

    def list_tasks(self) -> list[dict[str, Any]]:
        """所有存过盘的任务（按最近更新排序），加上内存里正在跑但还没存盘的。"""
        tasks = []
        seen_ids = set()
        for summary in self.store.summaries():
            seen_ids.add(summary.id)
            live = self.sessions.get(summary.id)
            task = {
                **asdict(summary),
                "running": bool(live and live.running),
                # 侧栏靠这个把「在跑」和「卡在你这儿」画成两种标记
                "waiting": live.waiting_for if live is not None else None,
            }
            if live is not None:
                # 内存里的标题可能刚改过还没存盘
                task["title"] = live.session.display_title()
            tasks.append(task)
        for live in self.sessions.values():
            if live.id not in seen_ids and live.running:
                session = live.session
                tasks.insert(
                    0,
                    {
                        "id": session.id,
                        "title": session.display_title(),
                        "workspace": session.workspace,
                        "ephemeral": session.ephemeral,
                        "updated_at": session.updated_at,
                        "source": session.source,
                        "worktree": session.worktree,
                        "running": True,
                        "waiting": live.waiting_for,
                    },
                )
        return tasks

    async def delete_task(self, session_id: str) -> None:
        live = self.sessions.get(session_id)
        if live is not None and live.running:
            raise SessionBusyError("任务还在运行，先停止再删除")
        web_session = live or (self.get(session_id) if self.store.exists(session_id) else None)
        if web_session is not None:
            await web_session.close()
            await web_session.checkpoints.forget()
        self.sessions.pop(session_id, None)
        self.store.delete(session_id)
        settings = self.base_settings
        (settings.traces_dir / f"{session_id}.jsonl").unlink(missing_ok=True)
        shutil.rmtree(settings.attachments_dir / session_id, ignore_errors=True)
        shutil.rmtree(session_directory(settings.tool_outputs_root, session_id), ignore_errors=True)

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
        # 搜索密钥同模型密钥：留空表示不改，勾了清除才清空
        if payload.get("clear_web_search_api_key"):
            data["web_search_api_key"] = None
        elif not payload.get("web_search_api_key"):
            data["web_search_api_key"] = self.app_config.web_search_api_key
        if payload.get("clear_image_gen_api_key"):
            data["image_gen_api_key"] = None
        elif not payload.get("image_gen_api_key"):
            data["image_gen_api_key"] = self.app_config.image_gen_api_key
        data["web_token"] = self.app_config.web_token
        data["workspaces"] = self.app_config.workspaces
        return AppConfig.from_dict(data)

    async def apply_config(self, new_config: AppConfig) -> None:
        new_config.save(self.base_settings.config_path)
        self.app_config = new_config
        self.base_settings.apply_app_config(new_config)
        for web_session in self.sessions.values():
            settings = web_session.settings
            settings.apply_app_config(new_config)
            # 联网、沙箱、技能可能变了，工具重新装一遍（下一次调用模型时生效）
            web_session.agent.reload_tools()
            profile = new_config.find_model(settings.model_profile_id) or new_config.default_profile()
            if profile is not None:
                await web_session.use_model(profile, self.build_provider)

    async def update_model_options(self, model_id: str, options: dict[str, Any]) -> ModelProfile:
        """选模型面板里直接改思考档位、上下文长度：存进配置，用这个模型的任务马上换上。"""
        profile = self.app_config.find_model(model_id)
        if profile is None:
            raise KeyError(model_id)
        data = profile.to_dict()
        if "reasoning_effort" in options:
            data["reasoning_effort"] = options["reasoning_effort"] or None
        if "context_window_tokens" in options:
            data["context_window_tokens"] = options["context_window_tokens"]
        updated = ModelProfile.from_dict(data)
        self.app_config.models = [updated if each.id == model_id else each for each in self.app_config.models]
        self.app_config.save(self.base_settings.config_path)
        for web_session in self.sessions.values():
            if web_session.settings.model_profile_id == model_id:
                await web_session.use_model(updated, self.build_provider)
        return updated

    def skills_overview(self, workspace: str | None) -> dict[str, Any]:
        """设置里「技能」页：找技能的目录、找到的技能。给了工作区就带上那个工作区自己的技能。"""
        config = self.app_config
        root = Path(workspace) if workspace else None
        directories = skill_directories(root, self.base_settings.home_dir, config.skill_dirs)
        catalog = discover_skills(directories)
        return {
            "enabled": config.skills_enabled,
            "directories": [
                {"path": str(directory.path), "source": directory.source, "exists": directory.path.is_dir()}
                for directory in directories
            ],
            "skills": [skill.public_dict(disabled=skill.name in config.disabled_skills) for skill in catalog.skills],
            "problems": catalog.problems,
        }

    def skill_file_content(self, raw_path: str) -> dict[str, Any]:
        """设置里点「查看」：只给找技能时真找到了的 SKILL.md，不当通用的读文件接口用。正文和元数据分开给。"""
        target = Path(raw_path).expanduser().resolve()
        roots = [Path(workspace) for workspace in self.app_config.workspaces] + [None]
        for root in roots:
            directories = skill_directories(root, self.base_settings.home_dir, self.app_config.skill_dirs)
            for skill in discover_skills(directories).skills:
                if skill.path.resolve() == target:
                    metadata, body = split_frontmatter(target.read_text(encoding="utf-8", errors="replace"))
                    return {"content": body.strip(), "metadata": metadata}
        raise KeyError(raw_path)

    # ---- MCP ----

    def mcp_manager(self) -> McpManager:
        """MCP 连接按 PocketAgent 目录共享，和各个任务里 agent 用的是同一个。"""
        return shared_mcp_manager(self.base_settings.home_dir)

    def mcp_overview(self) -> dict[str, Any]:
        return self.mcp_manager().public_overview()

    async def save_mcp_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """设置里保存 MCP：写 mcp.json 后按新配置重连，返回最新状态。"""
        manager = self.mcp_manager()
        entries = payload.get("servers") if isinstance(payload, dict) else None
        save_mcp_servers(self.base_settings.home_dir, entries or [], manager.load())
        problems = await manager.reload()
        return _overview_with_problems(manager.public_overview(), problems)

    async def reload_mcp(self) -> dict[str, Any]:
        """设置里点「重新连接」：连不上的重连，已经连着的也重连一遍。"""
        manager = self.mcp_manager()
        problems = await manager.reload(force=True)
        return _overview_with_problems(manager.public_overview(), problems)

    # ---- 项目记忆 ----

    def _memory_dir(self, workspace: str) -> Path:
        root = Path(workspace).expanduser().resolve()
        if str(root) not in self.app_config.workspaces:
            raise ConfigError(f"不是已添加的工作区：{workspace}")
        return project_memory_dir(self.base_settings.home_dir, root)

    def memory_overview(self, workspace: str) -> dict[str, Any]:
        """设置里「记忆」页：这个工作区的记忆目录、索引、每条记忆。"""
        memory_dir = self._memory_dir(workspace)
        return {
            "enabled": self.app_config.memory_enabled,
            "directory": str(memory_dir),
            "index": read_index(memory_dir),
            "memories": [memory.to_dict() for memory in list_memories(memory_dir)],
        }

    def read_memory(self, workspace: str, file_name: str) -> dict[str, Any]:
        """一条记忆的正文（开头的元数据单独给）。"""
        try:
            path = memory_file_path(self._memory_dir(workspace), file_name)
        except ValueError as error:
            raise ConfigError(str(error))
        if not path.is_file():
            raise KeyError(file_name)
        metadata, body = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        return {"content": body.strip(), "metadata": metadata}

    def delete_memory(self, workspace: str, file_name: str) -> dict[str, Any]:
        try:
            delete_memory(self._memory_dir(workspace), file_name)
        except ValueError as error:
            raise ConfigError(str(error))
        return self.memory_overview(workspace)

    def task_overview(self, session_id: str) -> dict[str, Any]:
        """右侧栏要的：本任务用量（含缓存命中）、最新一条回复。比整份快照轻得多。"""
        web_session = self.get(session_id)
        latest_reply = ""
        for message in reversed(web_session.agent.context.messages):
            if message.get("role") == "assistant" and message.get("content"):
                latest_reply = message["content"]
                break
        return {
            "usage": self.usage_log.session_total(session_id),
            "turns": web_session.agent.session.turns,
            "tool_calls": web_session.agent.session.tool_calls,
            "latest_reply": latest_reply,
            "updated_at": web_session.session.updated_at,
            **web_session.status_fields(),
        }

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
        automation_names = self.automations.name_map()
        summaries = []
        for automation in self.automations.list_all():
            status = automation.last_status
            live = self.sessions.get(automation.last_session_id or "")
            if live is not None and live.running:
                status = "running"
            next_run_at = automation.next_run_at() if automation.enabled else None
            summaries.append(
                {
                    **automation.to_dict(),
                    "schedule_text": automation.describe_schedule(automation_names),
                    "trigger_kind": automation.trigger.source.kind,
                    "next_run_at": next_run_at.isoformat(timespec="minutes") if next_run_at else None,
                    "last_status": status,
                    "problem": self.triggers.problem_for(automation.id),
                }
            )
        return summaries

    def run_automation(self, automation_id: str, occurrence: Occurrence | None = None) -> WebSession:
        """开一个任务把这条自动化跑起来。occurrence 为空表示用户手点的「立即运行」。"""
        return self._start_triggered_run(self.automations.get(automation_id), occurrence)

    def _start_triggered_run(self, automation: Automation, occurrence: Occurrence | None) -> WebSession:
        web_session = self.create(
            Path(automation.workspace),
            model_id=automation.model_id,
            mode_name=automation.approval_mode,
            title=automation.name,
            source=f"automation:{automation.id}",
        )
        web_session.approval_timeout_seconds = AUTOMATION_APPROVAL_TIMEOUT_SECONDS
        # 这次任务是被触发的第几层：它自己再触发别人的时候，深度接着往下传
        web_session.trigger_depth = occurrence.depth + 1 if occurrence is not None else 0
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

    # ---- 触发器的两个入口 ----

    def _on_agent_event(self, web_session: WebSession, event: Event) -> None:
        """每一轮结束时问一次触发器。扔到后台任务里，别让条件脚本拖慢这一轮的收尾。"""
        if event.type != EventType.TURN_END:
            return
        if not self.automations.path.is_file():
            # 一条自动化都没配：别为了每一轮去读一次文件
            return
        occurrence = self._task_end_occurrence(web_session, event.data)
        task = asyncio.create_task(self.triggers.fire(occurrence))
        self.trigger_tasks.add(task)
        task.add_done_callback(self.trigger_tasks.discard)

    def _task_end_occurrence(self, web_session: WebSession, data: dict[str, Any]) -> Occurrence:
        automation_id = _automation_id_from_source(web_session.session.source)
        return Occurrence(
            kind=SOURCE_TASK_END,
            # 同一个任务的第几轮：轮数在会话上，重启也接着数
            key=f"task_end:{web_session.id}:{web_session.session.turns}",
            at=datetime.now(),
            automation_id=automation_id,
            automation_name=self._automation_name(automation_id),
            session_id=web_session.id,
            status=str(data.get("stop_reason") or ""),
            text=str(data.get("final_text") or ""),
            depth=web_session.trigger_depth,
        )

    def _automation_name(self, automation_id: str) -> str:
        if not automation_id:
            return ""
        return self.automations.name_map().get(automation_id, "")

    async def run_due_automations(self, now: datetime) -> list[Any]:
        return await self.triggers.tick(now)

    async def run_scheduler(self) -> None:
        while True:
            await self.run_due_automations(datetime.now())
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    async def shutdown(self) -> None:
        for web_session in self.sessions.values():
            await web_session.interrupt()
            await web_session.close()
            for client in list(web_session.clients):
                await client.close()
        # 服务停了就把 MCP 服务器进程也结束掉（连接是同 home_dir 的会话共用的）
        await close_shared_mcp_managers()
