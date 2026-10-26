"""命令行界面：交互式 REPL 和 -p 单次执行。

界面只是事件的订阅者（ConsoleRenderer）+ 一个审批实现（ConsoleApprover），
以后做 TUI / IDE 插件 / Web 时照着替换这两样就行，agent 本身不用动。
"""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from pocketagent.agent import Agent, create_agent
from pocketagent.config import ConfigError, Settings
from pocketagent.events import Event, EventType
from pocketagent.permissions import (
    AllowAllApprover,
    ApprovalAnswer,
    ApprovalMode,
    ApprovalRequest,
    Approver,
    DenyAllApprover,
)
from pocketagent.session import Session, SessionStore

TOOL_RESULT_PREVIEW_LINES = 4

HELP_TEXT = """命令：
  /help            显示这段帮助
  /new             开一个新会话
  /compact         手动压缩上下文
  /mode [模式]     查看或切换审批模式：read_only / ask / auto_edit / full_auto
  /skills          列出可用的技能
  /技能名 [要求]   启用这个技能并把要求交给 agent
  /exit            退出（也可以按 Ctrl-D）
运行中按 Ctrl-C 中断当前这一轮，会话保留，可以接着说。"""


class Style:
    """ANSI 颜色，输出不是终端时自动关掉。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


class ConsoleRenderer:
    """事件订阅者：把流式文本、工具调用和结果打印到终端。"""

    def __init__(self, style: Style, show_reasoning: bool = False) -> None:
        self.style = style
        self.show_reasoning = show_reasoning
        self.at_line_start = True
        self.in_reasoning = False

    def __call__(self, event: Event) -> None:
        match event.type:
            case EventType.TEXT_DELTA:
                self._print_text(event.data["text"])
            case EventType.REASONING_DELTA:
                self._print_reasoning(event.data["text"])
            case EventType.TOOL_CALL:
                self._print_tool_call(event.data["name"], event.data["summary"])
            case EventType.TOOL_RESULT:
                self._print_tool_result(event.data["content"], event.data["is_error"], event.data["denied"])
            case EventType.LLM_RETRY:
                self._print_line(self.style.yellow(f"[模型调用失败，第 {event.data['attempt']} 次尝试：{event.data['error']}]"))
            case EventType.COMPACTION:
                tokens_before = event.data["tokens_before"]
                tokens_after = event.data["tokens_after"]
                self._print_line(self.style.dim(f"[上下文已压缩：约 {tokens_before} → {tokens_after} tokens]"))
            case EventType.STEP_LIMIT:
                self._print_line(self.style.yellow(f"[这一轮已调用模型 {event.data['max_steps']} 次，达到上限，先停下]"))
            case EventType.ERROR:
                self._print_line(self.style.red(f"[出错] {event.data['message']}"))
            case EventType.TURN_END:
                self._end_line()

    def _write(self, text: str) -> None:
        if not text:
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self.at_line_start = text.endswith("\n")

    def _end_line(self) -> None:
        if not self.at_line_start:
            self._write("\n")

    def _print_line(self, text: str) -> None:
        self._end_line()
        self._write(text + "\n")

    def _print_text(self, text: str) -> None:
        if self.in_reasoning:
            self.in_reasoning = False
            self._end_line()
        self._write(text)

    def _print_reasoning(self, text: str) -> None:
        if not self.show_reasoning:
            return
        self.in_reasoning = True
        self._write(self.style.dim(text))

    def _print_tool_call(self, name: str, summary: str) -> None:
        self.in_reasoning = False
        self._print_line(f"{self.style.cyan('⏺')} {self.style.bold(name)}({summary})")

    def _print_tool_result(self, content: str, is_error: bool, denied: bool) -> None:
        if denied:
            self._print_line(self.style.yellow(f"  ⎿ {content.splitlines()[0] if content else '已拒绝'}"))
            return
        lines = content.splitlines() or ["（无输出）"]
        shown = lines[:TOOL_RESULT_PREVIEW_LINES]
        if len(lines) > TOOL_RESULT_PREVIEW_LINES:
            shown.append(f"…（共 {len(lines)} 行）")
        colorize = self.style.red if is_error else self.style.dim
        for index, line in enumerate(shown):
            prefix = "  ⎿ " if index == 0 else "    "
            self._print_line(colorize(prefix + line))


class ConsoleApprover:
    """在终端里问用户要不要放行。用的是阻塞的 input()：等待确认时 agent 本来也停着。"""

    def __init__(self, style: Style) -> None:
        self.style = style

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        print(self.style.yellow(f"\n需要确认：{request.tool_name}"))
        for note in request.notes:
            print(self.style.yellow(f"  ! {note}"))
        print(self._colorize_preview(request.preview))
        always = f"[a] {request.always_label}  " if request.always_label else ""
        prompt = f"[y] 允许  {always}[n] 拒绝（默认）> "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ApprovalAnswer.DENY
        if answer in ("y", "yes"):
            return ApprovalAnswer.ALLOW_ONCE
        if answer in ("a", "always") and request.always_label:
            return ApprovalAnswer.ALLOW_ALWAYS
        return ApprovalAnswer.DENY

    def _colorize_preview(self, preview: str) -> str:
        colored_lines = []
        for line in preview.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                colored_lines.append(self.style.green(line))
            elif line.startswith("-") and not line.startswith("---"):
                colored_lines.append(self.style.red(line))
            else:
                colored_lines.append(line)
        return "\n".join(colored_lines)


class Repl:
    def __init__(self, settings: Settings, session: Session | None, show_reasoning: bool) -> None:
        self.settings = settings
        self.style = Style(enabled=sys.stdout.isatty())
        self.renderer = ConsoleRenderer(self.style, show_reasoning=show_reasoning)
        self.loop = asyncio.new_event_loop()
        self.agent = self._create_agent(session)

    def _create_agent(self, session: Session | None) -> Agent:
        agent = create_agent(self.settings, approver=ConsoleApprover(self.style), session=session)
        agent.context.events.subscribe(self.renderer)
        return agent

    def run(self, first_prompt: str | None) -> None:
        self._print_banner()
        try:
            if first_prompt:
                self._run_turn(first_prompt)
            while True:
                user_text = self._read_input()
                if user_text is None:
                    break
                if not user_text.strip():
                    continue
                if user_text.startswith("/"):
                    if not self._handle_command(user_text.strip()):
                        break
                    continue
                self._run_turn(user_text)
        finally:
            self._close_loop()

    def _close_loop(self) -> None:
        """和 asyncio.run 收尾一样：先回收没跑完的 async generator（如 HTTP 流），再关事件循环。"""
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    def _print_banner(self) -> None:
        settings = self.settings
        print(self.style.bold("PocketAgent"))
        mode = self.agent.context.permissions.mode.value
        print(self.style.dim(f"模型 {settings.display_model()} · 目录 {settings.workspace} · 审批 {mode}"))
        print(self.style.dim(f"会话 {self.agent.session.id} · /help 查看命令 · Ctrl-C 中断 · Ctrl-D 退出"))

    def _read_input(self) -> str | None:
        try:
            return input(self.style.bold("\n› "))
        except EOFError:
            return None
        except KeyboardInterrupt:
            print()
            return ""

    def _run_turn(self, user_text: str) -> None:
        task = self.loop.create_task(self.agent.run_turn(user_text))
        interrupt_installed = self._install_interrupt_handler(task)
        try:
            self.loop.run_until_complete(task)
        except (asyncio.CancelledError, KeyboardInterrupt):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, KeyboardInterrupt):
                    self.loop.run_until_complete(task)
            print(self.style.yellow("\n[已中断，可以接着说]"))
        finally:
            if interrupt_installed:
                self.loop.remove_signal_handler(signal.SIGINT)

    def _install_interrupt_handler(self, task: asyncio.Task) -> bool:
        """Ctrl-C 只取消当前这一轮，不退出程序。不支持的平台退回默认的 KeyboardInterrupt。"""
        try:
            self.loop.add_signal_handler(signal.SIGINT, task.cancel)
        except (NotImplementedError, RuntimeError):
            return False
        return True

    def _handle_command(self, command_line: str) -> bool:
        """返回 False 表示退出。"""
        command, _, argument = command_line.partition(" ")
        argument = argument.strip()
        if command in ("/exit", "/quit"):
            return False
        if command == "/help":
            print(HELP_TEXT)
        elif command == "/new":
            self.agent = self._create_agent(session=None)
            print(self.style.dim(f"已开新会话 {self.agent.session.id}"))
        elif command == "/compact":
            self.loop.run_until_complete(self.agent.compact())
        elif command == "/mode":
            self._switch_mode(argument)
        elif command == "/skills":
            self._list_skills()
        elif any(skill.name == command[1:] for skill in self.agent.skills):
            # 交给 agent：它会把技能的完整说明接在这句话后面
            self._run_turn(command_line)
        else:
            print(self.style.yellow(f"不认识的命令 {command}，/help 查看全部命令"))
        return True

    def _list_skills(self) -> None:
        if not self.agent.skills:
            print(self.style.dim("没有可用的技能（~/.pocketagent/skills、~/.agents/skills、~/.claude/skills、工作目录下的 .agents/skills 等）"))
            return
        for skill in self.agent.skills:
            print(f"  /{skill.name}  {self.style.dim(skill.description[:80])}")

    def _switch_mode(self, mode_name: str) -> None:
        permissions = self.agent.context.permissions
        if not mode_name:
            print(f"当前审批模式：{permissions.mode.value}")
            return
        try:
            permissions.mode = ApprovalMode(mode_name)
        except ValueError:
            choices = " / ".join(mode.value for mode in ApprovalMode)
            print(self.style.yellow(f"没有这个模式，可选：{choices}"))
            return
        self.settings.approval_mode = permissions.mode
        print(f"审批模式已切换为 {permissions.mode.value}")


def run_once(settings: Settings, session: Session | None, prompt: str, show_reasoning: bool) -> int:
    """-p 模式：跑完一轮就退出。没人能点确认，需要审批的调用一律拒绝（full_auto 除外）。"""
    approver: Approver = AllowAllApprover() if settings.approval_mode == ApprovalMode.FULL_AUTO else DenyAllApprover()
    agent = create_agent(settings, approver=approver, session=session)
    agent.context.events.subscribe(ConsoleRenderer(Style(enabled=sys.stdout.isatty()), show_reasoning=show_reasoning))
    asyncio.run(agent.run_turn(prompt))
    return 1 if agent.context.turn.stop_reason == "error" else 0


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pocketagent", description="基于 PocketFlow 的编程 agent")
    parser.add_argument("prompt", nargs="*", help="第一句话；配合 -p 时执行完就退出")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true", help="非交互：执行 prompt 后退出")
    parser.add_argument("-C", "--workspace", help="工作目录，默认当前目录")
    parser.add_argument("-m", "--model", help="配置里的模型名称 / id，或者直接写模型名；默认用配置里的默认模型")
    parser.add_argument("--mode", choices=[mode.value for mode in ApprovalMode], help="审批模式，默认 ask")
    parser.add_argument("-c", "--continue", dest="continue_latest", action="store_true", help="继续这个目录最近的会话")
    parser.add_argument("-r", "--resume", help="按 id 续跑会话")
    parser.add_argument("--sessions", action="store_true", help="列出这个目录的会话后退出")
    parser.add_argument("--show-reasoning", action="store_true", help="显示模型的思考过程")
    web_group = parser.add_argument_group("web 界面")
    web_group.add_argument("--web", action="store_true", help="启动 web 界面，不进命令行交互")
    web_group.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    web_group.add_argument("--port", type=int, help="端口，默认 8765，被占用就往后找空闲的")
    web_group.add_argument("--token", help="访问 token，默认第一次启动时生成并存进配置文件（也可设 POCKETAGENT_WEB_TOKEN）")
    web_group.add_argument("--no-token", action="store_true", help="不要 token（只允许监听本机时用）")
    web_group.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args(argv)


def _settings_from_arguments(arguments: argparse.Namespace) -> Settings:
    workspace = Path(arguments.workspace).expanduser().resolve() if arguments.workspace else Path.cwd()
    approval_mode = ApprovalMode(arguments.mode) if arguments.mode else None
    try:
        return Settings.load(model=arguments.model, workspace=workspace, approval_mode=approval_mode)
    except ConfigError as error:
        sys.exit(f"配置有误：{error}")


def _pick_session(arguments: argparse.Namespace, settings: Settings) -> Session | None:
    store = SessionStore(settings.sessions_dir)
    if arguments.resume:
        return store.load(arguments.resume)
    if arguments.continue_latest:
        latest = store.latest(settings.workspace)
        if latest is None:
            print("这个目录还没有会话，开新会话", file=sys.stderr)
        return latest
    return None


def _print_sessions(settings: Settings) -> None:
    sessions = SessionStore(settings.sessions_dir).list_sessions(settings.workspace)
    if not sessions:
        print("这个目录还没有会话")
        return
    for session in sessions:
        first_line = session.first_user_text().splitlines()[0] if session.first_user_text() else ""
        print(f"{session.id}  {session.updated_at}  {first_line[:60]}")


def _run_web(arguments: argparse.Namespace, settings: Settings) -> None:
    try:
        from pocketagent.web.server import run_web
    except ImportError:
        sys.exit("web 界面需要 aiohttp：pip install 'pocketagent[web]'")
    run_web(
        settings,
        host=arguments.host,
        port=arguments.port,
        token=arguments.token or os.environ.get("POCKETAGENT_WEB_TOKEN"),
        require_token=not arguments.no_token,
        open_browser=not arguments.no_browser,
    )


def main(argv: list[str] | None = None) -> None:
    try:
        import readline  # noqa: F401  让 input() 支持方向键和历史
    except ImportError:
        pass
    arguments = _parse_arguments(argv)
    settings = _settings_from_arguments(arguments)
    if arguments.sessions:
        _print_sessions(settings)
        return
    if arguments.web:
        _run_web(arguments, settings)
        return
    session = _pick_session(arguments, settings)
    prompt = " ".join(arguments.prompt).strip()
    if arguments.print_mode:
        if not prompt:
            sys.exit("-p 需要给一句 prompt")
        sys.exit(run_once(settings, session, prompt, arguments.show_reasoning))
    Repl(settings, session, show_reasoning=arguments.show_reasoning).run(prompt or None)
