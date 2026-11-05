"""无人值守地跑一组用户的话：回放和评测共用。

安全约定（为什么这样设）：
- 工作区永远是临时副本（见 workspace.py），用户的真实工作区一个字节都不碰
- 审批模式用 auto_edit：改文件直接放行——工作区就是副本，改的只能是副本；
  其余要审批的交给 UnattendedApprover：只放行沙箱里跑的 bash（沙箱只让它写副本和临时目录），
  碰工作区以外的路径、要跳出沙箱、碰密钥的一律拒绝。这台机器没有沙箱时 bash 也拒绝：命令没有隔离，
  可能改到副本以外的东西。和 -p 单次执行一样，被拒绝时这一轮就停下，表里记为失败
- 不用 AI 审批（自动化用的那档）：审批模型的判断有随机性、还要多花钱，会让模型之间的对比不干净
- 关掉会把副本以外的目录当工作区的设置（免问目录、工作目录外不再询问），关掉钩子（用户脚本可能发通知、写日志）、
  MCP（外部服务器可能改真实系统）、computer use（操作整台电脑）、项目记忆（副本路径对应一份没用的空记忆）、
  检查点（副本跑完就删，不用再拍进影子仓库）；子 agent 不另开 worktree、也不用单独配的模型——对比要公平，
  子 agent 也用被测的模型
- 每次运行照常写自己的 trace（~/.pocketagent/traces），跑完删掉会话文件，免得任务列表里堆满回放出来的任务
"""

import dataclasses
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pocketagent.config import AppConfig, Settings, config_path, default_home_dir
from pocketagent.core.agent import Agent, create_agent, sandbox_active
from pocketagent.core.events import EventHandler
from pocketagent.core.session import SessionStore
from pocketagent.evaluation.trace_reader import TraceSummary, read_trace
from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.llm.base import LLMProvider
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalMode, ApprovalRequest
from pocketagent.safety.sandbox import SandboxPolicy, available_sandbox
from pocketagent.tools.background import ACTIVE_STATUSES

ProviderFactory = Callable[[Settings], LLMProvider]
# 跑完收尾时停掉模型留在后台的命令：副本马上要删，不用给它们多少时间体面退出
BACKGROUND_STOP_GRACE_SECONDS = 2


class UnattendedApprover:
    """没人在场时的审批：只放行沙箱里的 bash，其余一律拒绝；提问没人回答。

    请求带 notes 说明这次调用碰到了工作区以外、沙箱以外或者密钥（见 PermissionGate.check），这类都拒绝。
    """

    def __init__(self, allow_sandboxed_bash: bool) -> None:
        self.allow_sandboxed_bash = allow_sandboxed_bash

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        if self.allow_sandboxed_bash and request.tool_name == "bash" and not request.notes:
            return ApprovalAnswer.ALLOW_ONCE
        return ApprovalAnswer.DENY

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        return None


@dataclass
class ModelChoice:
    """命令行里的一个 --model：读好的设置、表头上的名字，以及配置里找不到时的提醒。"""

    key: str | None
    settings: Settings
    label: str
    note: str = ""


def resolve_models(model_keys: list[str | None]) -> list[ModelChoice]:
    """和 -m 一样按配置里的 id、名称、模型名找；都找不到就当模型名直接用默认模型的接口。同一个模型给两次就加个序号。"""
    app_config = AppConfig.load(config_path(default_home_dir()))
    choices: list[ModelChoice] = []
    for key in model_keys or [None]:
        settings = Settings.load(model=key)
        note = ""
        if key and app_config.find_model(key) is None:
            note = f"配置里没有叫「{key}」的模型，按模型名直接用默认模型的接口"
        label = settings.display_model()
        existing_labels = [choice.label for choice in choices]
        if label in existing_labels:
            label = f"{label} #{existing_labels.count(label) + 1}"
        choices.append(ModelChoice(key=key, settings=settings, label=label, note=note))
    return choices


def sandbox_warning() -> str:
    """没有可用沙箱时给用户的提醒；有沙箱返回空串。"""
    if available_sandbox() is None:
        return "这台机器没有可用的沙箱（macOS 要 sandbox-exec，Linux 要 bubblewrap）：agent 的 bash 调用都会被拒绝"
    return ""


def unattended_settings(base: Settings, workspace: Path) -> Settings:
    """在 base（某个模型的设置）上套用无人值守的约束，原对象不动。理由见模块说明。"""
    return dataclasses.replace(
        base,
        workspace=workspace,
        approval_mode=ApprovalMode.AUTO_EDIT,
        trusted_dirs=[],
        allow_outside_workspace=False,
        hooks=[],
        computer_use=False,
        memory_enabled=False,
        sandbox_enabled=True,
        worktree_foreground=False,
        worktree_background=False,
        worktree=None,
        ephemeral=False,
        subagent_profile=None,
        review_profile=None,
    )


@dataclass
class RunRequest:
    label: str
    # 已经套过 unattended_settings 的设置
    settings: Settings
    prompts: list[str]
    provider_factory: ProviderFactory
    system_prompt_template: str | None = None
    # 想看过程时（-v）把事件交给命令行的渲染器
    event_handler: EventHandler | None = None
    # 写进会话的 source：replay / eval
    source: str = "replay"


@dataclass
class RunOutcome:
    label: str
    model: str
    trace_path: Path | None
    summary: TraceSummary | None
    final_text: str
    # 跑完以后评测检查要在同样的沙箱里跑命令
    sandbox: SandboxPolicy | None
    # 根本没跑起来（造 provider 就出错了）的原因
    crash: str | None = None


async def run_prompts(request: RunRequest) -> RunOutcome:
    """按顺序把每句话交给一个新 agent，跑完收尾；返回这次运行写下的 trace 的摘要。"""
    settings = request.settings
    # 表头已经是配置里的名称，这里记接口上的模型名
    model = settings.model
    try:
        provider = request.provider_factory(settings)
    except Exception as error:
        return RunOutcome(
            label=request.label,
            model=model,
            trace_path=None,
            summary=None,
            final_text="",
            sandbox=None,
            crash=f"{type(error).__name__}: {error}",
        )
    agent = create_agent(
        settings,
        approver=UnattendedApprover(allow_sandboxed_bash=sandbox_active(settings)),
        provider=provider,
        provider_builder=request.provider_factory,
    )
    _make_unattended(agent, request)
    trace_path = settings.traces_dir / f"{agent.session.id}.jsonl"
    final_text = ""
    try:
        for prompt in request.prompts:
            final_text = await agent.run_turn(prompt)
    finally:
        await agent.close()
        await _stop_background_commands(agent)
        _discard_session_files(agent)
    return RunOutcome(
        label=request.label,
        model=model,
        trace_path=trace_path,
        summary=read_trace(trace_path) if trace_path.is_file() else None,
        final_text=final_text,
        sandbox=agent.context.tool_context.sandbox,
    )


def _make_unattended(agent: Agent, request: RunRequest) -> None:
    # MCP 由每一轮开头按需连接；没有 manager 就整个跳过，外部服务器不会被拉起来
    agent.context.mcp_manager = None
    agent.checkpoints.enabled = False
    agent.session.source = request.source
    if request.system_prompt_template is not None:
        agent.use_system_prompt_template(request.system_prompt_template)
    if request.event_handler is not None:
        agent.context.events.subscribe(request.event_handler)


async def _stop_background_commands(agent: Agent) -> None:
    """模型放到后台的命令（dev server 之类）由独立进程托管，不停掉的话副本删了它们还在跑。

    list() 只列这个工作区（副本）里的命令，副本只有这一次运行在用，所以不再按会话筛：子 agent 起的也一起停。
    """
    manager = agent.context.tool_context.background_tasks
    for record in manager.list():
        if record.get("status") in ACTIVE_STATUSES:
            await manager.stop(record["id"], grace_seconds=BACKGROUND_STOP_GRACE_SECONDS)


def _discard_session_files(agent: Agent) -> None:
    """trace 才是这次运行的记录；会话文件留着只会让任务列表里多出一堆指向已删副本的任务。"""
    store = SessionStore(agent.context.settings.sessions_dir)
    store.path_for(agent.session.id).unlink(missing_ok=True)
    store.journal_path_for(agent.session.id).unlink(missing_ok=True)
    # 只删空目录：落了盘的长工具输出在 trace 里还被引用着，留给 7 天的自动清理
    tool_outputs_dir = agent.context.tool_context.tool_outputs_dir
    if tool_outputs_dir is not None:
        with suppress(OSError):
            tool_outputs_dir.rmdir()
