"""命令行各前端共用的会话操作：REPL 和 TUI 只在渲染、输入和审批上不同。"""

from pathlib import Path

from pocketagent.config import AppConfig, ConfigError, ModelProfile, Settings
from pocketagent.core.agent import Agent, create_agent, create_provider
from pocketagent.core.events import EventHandler
from pocketagent.core.scratch import create_scratch_workspace
from pocketagent.core.session import Session, SessionStore
from pocketagent.interaction.commands import CommandContext, CommandResult, handle_command_line
from pocketagent.safety.permissions import ApprovalMode, Approver


class CliController:
    """持有当前 agent，并落地斜杠命令里需要替换会话或模型的动作。"""

    def __init__(
        self,
        settings: Settings,
        approver: Approver,
        renderer: EventHandler | None,
        session: Session | None = None,
        agent: Agent | None = None,
    ) -> None:
        self.settings = settings
        self.approver = approver
        self.renderer = renderer
        self.agent = agent if agent is not None else self._create_agent(session)
        if agent is not None and renderer is not None:
            agent.context.events.subscribe(renderer)

    def _create_agent(self, session: Session | None) -> Agent:
        agent = create_agent(self.settings, approver=self.approver, session=session)
        if self.renderer is not None:
            agent.context.events.subscribe(self.renderer)
        return agent

    async def run_command(self, command_line: str) -> CommandResult | None:
        context = CommandContext(
            settings=self.settings,
            agent=self.agent,
            models=self.configured_models(),
            surface="cli",
        )
        return await handle_command_line(command_line, context)

    def configured_models(self) -> list[ModelProfile]:
        """配置损坏时仍保留基本对话能力，只是不提供模型切换。"""
        try:
            return AppConfig.load(self.settings.config_path).models
        except ConfigError:
            return []

    def start_new_session(self) -> None:
        if self.settings.ephemeral:
            # 临时任务的 /new 也要一个干净目录，不能继续写上一个临时工作区。
            self.settings.workspace = create_scratch_workspace(self.settings.home_dir)
        self.agent = self._create_agent(session=None)

    def switch_mode(self, mode_name: str) -> None:
        mode = ApprovalMode(mode_name)
        self.agent.context.permissions.mode = mode
        self.settings.approval_mode = mode

    def switch_model(self, profile_id: str) -> None:
        profile = next((each for each in self.configured_models() if each.id == profile_id), None)
        if profile is None:
            return
        self.settings.use_profile(profile)
        self.agent.replace_provider(create_provider(self.settings))

    def open_session(self, session_id: str) -> None:
        session = SessionStore(self.settings.sessions_dir).load(session_id)
        self.settings.workspace = Path(session.workspace)
        self.settings.ephemeral = session.ephemeral
        self.agent = self._create_agent(session=session)
