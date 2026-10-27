"""斜杠命令：web 和命令行共用的一套 /命令。

用户输入以 / 开头时先按命令解析：
- 命中内置命令 → 直接执行，结果给界面显示，不惊动模型
- 命中技能名（/技能名）→ 返回 None，调用方当普通消息发给 agent（agent 会把技能正文接在后面）
- 都不是、但长得像命令（/xxx）→ 报「不认识的命令」
- 不是命令（普通文字、/usr/bin 这种路径）→ 返回 None，当普通消息

换模型、开新会话这类要动界面的动作不在这里做：CommandResult.action 描述意图，
命令行和 web 各自执行自己那套（见 cli.py / web/server.py）。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pocketagent.agent import Agent, enabled_skills
from pocketagent.config import ModelProfile, Settings
from pocketagent.memory import list_memories, read_index
from pocketagent.permissions import ApprovalMode
from pocketagent.session import SessionStore
from pocketagent.skills import Skill
from pocketagent.usage import UsageLog

# /后面跟命令名（不能带 / 和空白），名字后面是空格或行尾才算命令。
# /usr/bin 这种路径里还有 /，匹配不上，不会被误当成命令；/不存在的命令 则认成打错的命令。
COMMAND_PATTERN = re.compile(r"^/([^/\s]+)(?:\s+(.*))?$", re.DOTALL)


@dataclass(frozen=True)
class CommandSpec:
    """一条内置命令的说明，帮助文本和 web 自动补全都从这里出。"""

    name: str
    summary: str
    # 参数说明，空表示不带参数；给了参数还多打字会被提醒用法
    usage: str = ""
    aliases: tuple[str, ...] = ()
    # 没有会话时（新任务页）用不了的命令
    needs_session: bool = False
    # 只在命令行有意义
    cli_only: bool = False


COMMANDS: list[CommandSpec] = [
    CommandSpec("help", "显示命令列表", usage="[命令]"),
    CommandSpec("new", "开新会话", aliases=("clear",)),
    CommandSpec("compact", "压缩上下文", needs_session=True),
    CommandSpec("mode", "查看或切换审批模式", usage="[模式]"),
    CommandSpec("model", "查看或切换模型", usage="[模型]"),
    CommandSpec("skills", "列出可用的技能", usage="[技能名]"),
    CommandSpec("status", "查看模型、目录、会话、上下文占用"),
    CommandSpec("usage", "查看 token 用量", needs_session=True),
    CommandSpec("sessions", "列出这个目录的会话"),
    CommandSpec("resume", "按 id 续跑会话", usage="<id>"),
    CommandSpec("memory", "查看项目记忆", usage="[记忆文件]"),
    CommandSpec("undo", "撤销文件改动", usage="[路径|all]", needs_session=True),
    CommandSpec("exit", "退出命令行", aliases=("quit",), cli_only=True),
]


@dataclass
class ParsedCommand:
    """解析出来的命令：规范名 + 后面的参数原文。"""

    name: str
    argument: str
    raw: str


@dataclass
class CommandResult:
    """命令的执行结果：给用户看的文字 + 界面要执行的动作。"""

    text: str = ""
    kind: str = "info"  # info / warning / error
    action: str | None = None  # exit / new_session / compact / switch_mode / switch_model / open_session
    argument: str = ""


@dataclass
class CommandContext:
    """命令跑起来要用的东西。agent 是当前会话，models 是配置里可切换的模型。

    换模型、开新会话这类动作只在结果里描述意图（action），由界面执行自己那套。
    """

    settings: Settings
    agent: Agent | None = None
    models: list[ModelProfile] = field(default_factory=list)
    surface: str = "cli"  # cli / web：/exit 只在命令行有意义


Handler = Callable[[str, CommandContext], Awaitable[CommandResult]]


def parse_command(text: str) -> ParsedCommand | None:
    """长得像 /命令 的输入返回解析结果，否则 None（普通消息、路径等）。"""
    match = COMMAND_PATTERN.match(text.strip())
    if match is None:
        return None
    argument = (match.group(2) or "").strip()
    return ParsedCommand(name=match.group(1), argument=argument, raw=text.strip())


def command_specs_for(surface: str) -> list[CommandSpec]:
    """这个界面上能用的命令。"""
    return [spec for spec in COMMANDS if not (spec.cli_only and surface != "cli")]


def _canonical(name: str) -> str | None:
    for spec in COMMANDS:
        if name == spec.name or name in spec.aliases:
            return spec.name
    return None


def _spec(name: str) -> CommandSpec:
    return next(spec for spec in COMMANDS if spec.name == name)


def _available_skills(context: CommandContext) -> list[Skill]:
    if context.agent is not None:
        return context.agent.skills
    return enabled_skills(context.settings)


async def handle_command_line(text: str, context: CommandContext) -> CommandResult | None:
    """处理一条用户输入。返回 None 表示这不是命令，原样交给 agent。

    技能命令（/技能名）也返回 None：agent.run_turn 里会展开技能正文。
    """
    parsed = parse_command(text)
    if parsed is None:
        return None
    name = _canonical(parsed.name)
    if name is None:
        # 技能名放行给 agent；其余 /xxx 当打错的命令
        skills = _available_skills(context)
        if any(skill.name == parsed.name for skill in skills):
            return None
        return CommandResult(f"不认识的命令 /{parsed.name}，/help 查看全部命令", kind="error")
    spec = _spec(name)
    if spec.cli_only and context.surface != "cli":
        return CommandResult(f"网页上没有 /{name}，关掉标签页就退出了", kind="warning")
    if spec.usage == "" and parsed.argument:
        return CommandResult(f"用法：/{name}（不带参数）", kind="error")
    if spec.needs_session and context.agent is None:
        return CommandResult(f"先开个任务再用 /{name}", kind="warning")
    return await _HANDLERS[name](parsed.argument, context)


def help_text(context: CommandContext, argument: str = "") -> str:
    """/help 的输出；给了命令名就只讲这一条。"""
    if argument:
        name = _canonical(argument.split()[0])
        if name is None:
            return f"没有 /{argument.split()[0]} 这个命令"
        spec = _spec(name)
        usage = f" {spec.usage}" if spec.usage else ""
        aliases = f"（别名：{'、'.join('/' + item for item in spec.aliases)}）" if spec.aliases else ""
        return f"用法：/{name}{usage}\n{spec.summary}{aliases}"
    lines = ["命令："]
    for spec in command_specs_for(context.surface):
        usage = f" {spec.usage}" if spec.usage else ""
        lines.append(f"  /{spec.name}{usage}   {spec.summary}")
    lines.append("  /技能名 [要求]   启用这个技能并把要求交给 agent")
    return "\n".join(lines)


async def _run_help(argument: str, context: CommandContext) -> CommandResult:
    return CommandResult(help_text(context, argument))


async def _run_new(argument: str, context: CommandContext) -> CommandResult:
    if context.agent is None:
        return CommandResult("已经在新任务页了，直接输入要做的事就行", kind="warning")
    return CommandResult(action="new_session")


async def _run_compact(argument: str, context: CommandContext) -> CommandResult:
    return CommandResult(action="compact")


async def _run_mode(argument: str, context: CommandContext) -> CommandResult:
    current = context.settings.approval_mode
    if context.agent is not None:
        current = context.agent.context.permissions.mode
    if not argument:
        choices = " / ".join(mode.value for mode in ApprovalMode)
        return CommandResult(f"当前审批模式：{current.value}\n可选：{choices}")
    try:
        mode = ApprovalMode(argument)
    except ValueError:
        choices = " / ".join(mode.value for mode in ApprovalMode)
        return CommandResult(f"没有这个模式，可选：{choices}", kind="error")
    return CommandResult(f"审批模式已切换为 {mode.value}", action="switch_mode", argument=mode.value)


async def _run_model(argument: str, context: CommandContext) -> CommandResult:
    if not argument:
        return CommandResult(_model_listing(context))
    profile = _find_model(context.models, argument)
    if profile is None:
        return CommandResult(f"没有这个模型：{argument}，/model 查看可选的", kind="error")
    return CommandResult(
        f"模型已切换为 {profile.name}（{profile.model}）", action="switch_model", argument=profile.id
    )


async def _run_skills(argument: str, context: CommandContext) -> CommandResult:
    skills = _available_skills(context)
    if argument:
        skill = next((each for each in skills if each.name == argument), None)
        if skill is None:
            return CommandResult(f"没有 {argument} 这个技能", kind="error")
        return CommandResult(f"/{skill.name}  {skill.description}\n{skill.path}")
    if not skills:
        return CommandResult("没有可用的技能（~/.pocketagent/skills、~/.agents/skills、~/.claude/skills、工作目录下的 .agents/skills 等）", kind="warning")
    lines = []
    for skill in skills:
        lines.append(f"  /{skill.name}  {skill.description[:80]}")
    return CommandResult("\n".join(lines))


async def _run_status(argument: str, context: CommandContext) -> CommandResult:
    settings = context.settings
    lines = [f"模型  {settings.display_model()}"]
    directory = f"临时目录 {settings.workspace}" if settings.ephemeral else f"目录 {settings.workspace}"
    lines.append(directory)
    if context.agent is not None:
        session = context.agent.session
        lines.append(f"会话  {session.id}")
        lines.append(f"审批  {context.agent.context.permissions.mode.value}")
        tokens = context.agent.context.estimated_context_tokens()
        lines.append(f"上下文 约 {tokens} / {settings.context_window_tokens} tokens")
    else:
        lines.append(f"审批  {settings.approval_mode.value}")
    if settings.trusted_dirs:
        lines.append(f"免问目录  {'、'.join(settings.trusted_dirs)}")
    if settings.allow_outside_workspace:
        lines.append("工作目录外  不再询问（密钥目录照样问）")
    if settings.computer_use:
        lines.append("操作电脑  已开启（screenshot / computer）")
    return CommandResult("\n".join(lines))


async def _run_usage(argument: str, context: CommandContext) -> CommandResult:
    usage_log = UsageLog(context.settings.usage_log_path)
    total = usage_log.session_total(context.agent.session.id)
    summary = usage_log.summarize()
    today = next((item for item in summary["periods"] if item["label"] == "今天"), None)
    lines = [f"本会话：{_format_tokens(total)}"]
    if today is not None:
        lines.append(f"今天：{_format_tokens(today)}")
    return CommandResult("\n".join(lines))


async def _run_sessions(argument: str, context: CommandContext) -> CommandResult:
    store = SessionStore(context.settings.sessions_dir)
    sessions = store.list_sessions(context.settings.workspace)
    if not sessions:
        return CommandResult("这个目录还没有会话", kind="warning")
    current_id = context.agent.session.id if context.agent is not None else ""
    lines = []
    for session in sessions:
        title = session.display_title().splitlines()[0][:50]
        marker = "（当前）" if session.id == current_id else ""
        lines.append(f"  {session.id}  {session.updated_at}  {title}{marker}")
    return CommandResult("\n".join(lines))


async def _run_resume(argument: str, context: CommandContext) -> CommandResult:
    if not argument:
        return CommandResult("用法：/resume <会话 id>，/sessions 查看有哪些", kind="error")
    store = SessionStore(context.settings.sessions_dir)
    if not store.exists(argument):
        return CommandResult(f"找不到会话 {argument}，/sessions 查看有哪些", kind="error")
    return CommandResult(action="open_session", argument=argument)


async def _run_memory(argument: str, context: CommandContext) -> CommandResult:
    memory_dir = context.settings.memory_dir
    if not context.settings.memory_enabled:
        return CommandResult("项目记忆没开（设置里可以打开）", kind="warning")
    memories = list_memories(memory_dir)
    if argument:
        wanted = next((each for each in memories if each.file == argument or each.name == argument), None)
        if wanted is None:
            return CommandResult(f"没有 {argument} 这条记忆", kind="error")
        path = memory_dir / wanted.file
        body = path.read_text(encoding="utf-8", errors="replace")
        return CommandResult(f"{path}\n{body.strip()}")
    if not memories:
        index = read_index(memory_dir).strip()
        if index:
            return CommandResult(f"索引：\n{index}")
        return CommandResult(f"还没有项目记忆（存这里：{memory_dir}）", kind="warning")
    lines = ["项目记忆："]
    for memory in memories:
        lines.append(f"  {memory.file}  {memory.description[:60]}")
    lines.append("看正文：/memory <文件名>")
    return CommandResult("\n".join(lines))


async def _run_undo(argument: str, context: CommandContext) -> CommandResult:
    agent = context.agent
    if agent.turn_running:
        return CommandResult("任务还在运行，等它停下再撤销", kind="error")
    changes = await agent.checkpoints.changes()
    if not changes:
        return CommandResult("没有要撤销的文件改动", kind="warning")
    if not argument:
        lines = ["本任务改过的文件（撤销：/undo <路径>，全部：/undo all）："]
        for change in changes:
            lines.append(f"  {change.status} {change.path}  +{change.additions} −{change.deletions}")
        return CommandResult("\n".join(lines))
    if argument == "all":
        restored = []
        for change in changes:
            await agent.checkpoints.revert_file(change.path)
            restored.append(change.path)
        agent.session_store.save(agent.session)
        return CommandResult("已撤销：" + "、".join(restored))
    wanted = next((change for change in changes if change.path == argument), None)
    if wanted is None:
        return CommandResult(f"改动里没有 {argument}，/undo 查看列表", kind="error")
    await agent.checkpoints.revert_file(wanted.path)
    agent.session_store.save(agent.session)
    return CommandResult(f"已撤销 {wanted.path}")


async def _run_exit(argument: str, context: CommandContext) -> CommandResult:
    return CommandResult(action="exit")


def _find_model(models: list[ModelProfile], key: str) -> ModelProfile | None:
    """按 id、名称、模型名找，和配置里挑模型的顺序一致。"""
    for attribute in ("id", "name", "model"):
        for profile in models:
            if getattr(profile, attribute) == key:
                return profile
    return None


def _model_listing(context: CommandContext) -> str:
    if not context.models:
        return f"还没配置模型，当前用的是 {context.settings.display_model()}"
    current_id = context.settings.model_profile_id
    lines = ["模型："]
    for profile in context.models:
        marker = "（当前）" if profile.id == current_id else ""
        lines.append(f"  {profile.name}（{profile.model}）{marker}")
    lines.append("切换：/model <名称或 id>")
    return "\n".join(lines)


def _format_tokens(count: dict[str, Any]) -> str:
    parts = [
        f"输入 {count.get('prompt', 0)}（缓存 {count.get('cached', 0)}）",
        f"输出 {count.get('completion', 0)}",
        f"调用 {count.get('calls', 0)} 次",
    ]
    cost = float(count.get("cost") or 0)
    if cost:
        parts.append(f"费用 ${cost:.4f}")
    return " · ".join(parts)


_HANDLERS: dict[str, Handler] = {
    "help": _run_help,
    "new": _run_new,
    "compact": _run_compact,
    "mode": _run_mode,
    "model": _run_model,
    "skills": _run_skills,
    "status": _run_status,
    "usage": _run_usage,
    "sessions": _run_sessions,
    "resume": _run_resume,
    "memory": _run_memory,
    "undo": _run_undo,
    "exit": _run_exit,
}
