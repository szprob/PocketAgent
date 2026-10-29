"""命令行界面：交互式 REPL 和 -p 单次执行。

界面只是事件的订阅者（ConsoleRenderer）+ 一个审批实现（ConsoleApprover），
TUI 在 cli/tui.py 里用同一套接口替换这两样，agent 本身不用动。
"""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from pocketagent.cli.controller import CliController
from pocketagent.config import AppConfig, ConfigError, Settings
from pocketagent.core.agent import Agent, create_agent, create_provider
from pocketagent.core.events import Event, EventType
from pocketagent.core.scratch import create_scratch_workspace
from pocketagent.core.session import Session, SessionStore
from pocketagent.interaction.commands import CommandContext, CommandResult, handle_command_line
from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.safety.permissions import (
    AllowAllApprover,
    ApprovalAnswer,
    ApprovalMode,
    ApprovalRequest,
    Approver,
    DenyAllApprover,
)

TOOL_RESULT_PREVIEW_LINES = 4
# 这一轮为什么没说完：界面上要在回复末尾说一句，不然用户以为模型只是话说一半
CUT_OFF_NOTES = {
    "filtered": "回复被上游内容审核掐断，上面是已经输出的部分",
    "incomplete": "连接中途断了，这次回复不完整",
    "truncated": "回复超出长度上限，没有写完",
}


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
        # 已经实时打印过输出的工具调用：最后的 TOOL_RESULT 只补一行退出码，不重复整段
        self.streamed_call_ids: set[str] = set()

    def __call__(self, event: Event) -> None:
        match event.type:
            case EventType.TEXT_DELTA:
                self._print_text(event.data["text"])
            case EventType.REASONING_DELTA:
                self._print_reasoning(event.data["text"])
            case EventType.TOOL_CALL:
                self._print_tool_call(event.data["name"], event.data["summary"])
            case EventType.TOOL_OUTPUT_DELTA:
                self._print_tool_output(event.data["call_id"], event.data["text"])
            case EventType.TOOL_RESULT:
                self._print_tool_result(
                    event.data["call_id"], event.data["content"], event.data["is_error"], event.data["denied"]
                )
            case EventType.LLM_RETRY:
                self._print_line(self.style.yellow(f"[模型调用失败，第 {event.data['attempt']} 次尝试：{event.data['error']}]"))
            case EventType.COMPACTION:
                tokens_before = event.data["tokens_before"]
                tokens_after = event.data["tokens_after"]
                self._print_line(self.style.dim(f"[上下文已压缩：约 {tokens_before} → {tokens_after} tokens]"))
            case EventType.SUBAGENT_PROGRESS:
                mark = "✗ " if event.data["step"] == "tool_error" else ""
                self._print_line(self.style.dim(f"    ↳ 子 agent {mark}{event.data['name']} {event.data['summary']}"))
            case EventType.STEP_LIMIT:
                self._print_line(self.style.yellow(f"[这一轮已调用模型 {event.data['max_steps']} 次，达到上限，先停下]"))
            case EventType.ERROR:
                self._print_line(self.style.red(f"[出错] {event.data['message']}"))
            case EventType.TURN_END:
                note = CUT_OFF_NOTES.get(event.data.get("stop_reason") or "")
                if note:
                    self._print_line(self.style.yellow(f"[{note}]"))
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

    def _print_tool_output(self, call_id: str, text: str) -> None:
        self.streamed_call_ids.add(call_id)
        self.in_reasoning = False
        for line in text.splitlines():
            self._print_line(self.style.dim(f"  │ {line}"))

    def _print_tool_result(self, call_id: str, content: str, is_error: bool, denied: bool) -> None:
        if call_id in self.streamed_call_ids:
            self.streamed_call_ids.discard(call_id)
            # 输出已经一条条打过了，这里只补退出码（或报错信息），不重复整段
            exit_line = next(
                (line for line in content.splitlines() if line.startswith("[exit code")),
                content.splitlines()[0] if content else "",
            )
            colorize = self.style.red if is_error else self.style.dim
            self._print_line(colorize(f"  ⎿ {exit_line}"))
            return
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
        parents = f"[p] {request.always_parents_label}  " if request.always_parents_label else ""
        prompt = f"[y] 允许  {always}{parents}[n] 拒绝（默认）> "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ApprovalAnswer.DENY
        if answer in ("y", "yes"):
            return ApprovalAnswer.ALLOW_ONCE
        if answer in ("a", "always") and request.always_label:
            return ApprovalAnswer.ALLOW_ALWAYS
        if answer in ("p", "parents") and request.always_parents_label:
            return ApprovalAnswer.ALLOW_ALWAYS_PARENTS
        return ApprovalAnswer.DENY

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        """一题一题问：输入编号（多选用逗号隔开），0 自己写，直接回车跳过这一题。"""
        print(self.style.yellow("\n有几个问题想问你（直接回车跳过这一题）："))
        answers = []
        try:
            for index, question in enumerate(questions, start=1):
                answers.append(self._ask_one(index, question))
        except (EOFError, KeyboardInterrupt):
            return None
        return answers

    def _ask_one(self, index: int, question: Question) -> QuestionAnswer:
        header = f"〔{question.header}〕" if question.header else ""
        print(f"\n{index}. {header}{question.question}" + ("（可多选，用逗号隔开）" if question.multi_select else ""))
        for number, option in enumerate(question.options, start=1):
            description = self.style.dim(f" — {option.description}") if option.description else ""
            print(f"   {number}. {option.label}{description}")
        print("   0. 其他（自己写）")
        while True:
            raw = input("> ").strip()
            if not raw:
                return QuestionAnswer()
            numbers = [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]
            if not all(part.isdigit() and 0 <= int(part) <= len(question.options) for part in numbers):
                print(self.style.yellow(f"输入 0–{len(question.options)} 的编号"))
                continue
            if len(numbers) > 1 and not question.multi_select:
                print(self.style.yellow("这一题只能选一个"))
                continue
            selected = [question.options[int(part) - 1].label for part in numbers if part != "0"]
            other = input("自己写：").strip() if "0" in numbers else ""
            return QuestionAnswer(selected=selected, other=other)

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
        self.controller = CliController(
            settings,
            approver=ConsoleApprover(self.style),
            renderer=self.renderer,
            session=session,
        )

    @property
    def agent(self) -> Agent:
        return self.controller.agent

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
        """和 asyncio.run 收尾一样：先回收没跑完的 async generator（如 HTTP 流），再关事件循环。

        后台命令由独立监督进程持有、跨重启持久，这里不用也不该去结束它们。
        """
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    def _print_banner(self) -> None:
        settings = self.settings
        print(self.style.bold("PocketAgent"))
        mode = self.agent.context.permissions.mode.value
        directory = f"临时目录 {settings.workspace}" if settings.ephemeral else f"目录 {settings.workspace}"
        print(self.style.dim(f"模型 {settings.display_model()} · {directory} · 审批 {mode}"))
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
        """返回 False 表示退出。命令本身交给 commands.py，换模型 / 开新会话这类动作在这里落地。"""
        result = self.loop.run_until_complete(self._run_command(command_line))
        if result is None:
            # 不是命令（技能命令或普通消息），交给 agent
            self._run_turn(command_line)
            return True
        self._print_command_result(result)
        return self._apply_command_action(result)

    async def _run_command(self, command_line: str) -> CommandResult | None:
        return await self.controller.run_command(command_line)

    def _print_command_result(self, result: CommandResult) -> None:
        if not result.text:
            return
        colorize = {"error": self.style.red, "warning": self.style.yellow}.get(result.kind)
        for line in result.text.splitlines():
            print(colorize(line) if colorize else line)

    def _apply_command_action(self, result: CommandResult) -> bool:
        if result.action == "exit":
            return False
        if result.action == "new_session":
            self._start_new_session()
        elif result.action == "compact":
            self.loop.run_until_complete(self.agent.compact())
        elif result.action == "switch_mode":
            self._switch_mode(result.argument)
        elif result.action == "switch_model":
            self._switch_model(result.argument)
        elif result.action == "open_session":
            self._open_session(result.argument)
        return True

    def _start_new_session(self) -> None:
        self.controller.start_new_session()
        print(self.style.dim(f"已开新会话 {self.agent.session.id}"))

    def _switch_mode(self, mode_name: str) -> None:
        self.controller.switch_mode(mode_name)

    def _switch_model(self, profile_id: str) -> None:
        self.controller.switch_model(profile_id)

    def _open_session(self, session_id: str) -> None:
        self.controller.open_session(session_id)
        session = self.agent.session
        print(self.style.dim(f"已续跑会话 {session.id}：{session.display_title().splitlines()[0][:60]}"))


def run_once(settings: Settings, session: Session | None, prompt: str, show_reasoning: bool) -> int:
    """-p 模式：跑完一轮就退出。没人能点确认，需要审批的调用一律拒绝（full_auto 除外）。"""
    approver: Approver = AllowAllApprover() if settings.approval_mode == ApprovalMode.FULL_AUTO else DenyAllApprover()
    agent = create_agent(settings, approver=approver, session=session)
    agent.context.events.subscribe(ConsoleRenderer(Style(enabled=sys.stdout.isatty()), show_reasoning=show_reasoning))
    command_context = CommandContext(settings=settings, agent=agent, surface="cli")
    outcome = asyncio.run(handle_command_line(prompt, command_context))
    if outcome is not None:
        # 命令（/help、/status 这些）：只打印结果，不进对话；换模式、换模型顺手落一下
        if outcome.text:
            print(outcome.text)
        if outcome.action == "switch_mode":
            mode = ApprovalMode(outcome.argument)
            agent.context.permissions.mode = mode
            settings.approval_mode = mode
        elif outcome.action == "switch_model":
            profile = AppConfig.load(settings.config_path).find_model(outcome.argument)
            if profile is not None:
                settings.use_profile(profile)
                agent.replace_provider(create_provider(settings))
        return 0
    asyncio.run(agent.run_turn(prompt))
    return 1 if agent.context.turn.stop_reason == "error" else 0


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pocketagent", description="基于 PocketFlow 的编程 agent")
    parser.add_argument("prompt", nargs="*", help="第一句话；配合 -p 时执行完就退出")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true", help="非交互：执行 prompt 后退出")
    parser.add_argument("-C", "--workspace", help="工作目录，默认当前目录")
    parser.add_argument("--scratch", action="store_true", help="不指定工作区：在 ~/.pocketagent/scratch 下建一个临时目录")
    parser.add_argument("-m", "--model", help="配置里的模型名称 / id，或者直接写模型名；默认用配置里的默认模型")
    parser.add_argument("--mode", choices=[mode.value for mode in ApprovalMode], help="审批模式，默认 ask")
    parser.add_argument("-c", "--continue", dest="continue_latest", action="store_true", help="继续这个目录最近的会话")
    parser.add_argument("-r", "--resume", help="按 id 续跑会话")
    parser.add_argument("--sessions", action="store_true", help="列出这个目录的会话后退出")
    parser.add_argument("--show-reasoning", action="store_true", help="显示模型的思考过程")
    parser.add_argument("--tui", action="store_true", help="启动全屏交互式终端界面（需要 pocketagent[tui]）")
    web_group = parser.add_argument_group("web 界面")
    web_group.add_argument("--web", action="store_true", help="启动 web 界面，不进命令行交互")
    web_group.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    web_group.add_argument("--port", type=int, help="端口，默认 8765，被占用就往后找空闲的")
    web_group.add_argument("--token", help="访问 token，默认第一次启动时生成并存进配置文件（也可设 POCKETAGENT_WEB_TOKEN）")
    web_group.add_argument("--no-token", action="store_true", help="不要 token（只允许监听本机时用）")
    web_group.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args(argv)


def _settings_from_arguments(arguments: argparse.Namespace) -> Settings:
    if arguments.scratch and arguments.workspace:
        sys.exit("--scratch 和 -C 只能给一个：临时任务不指定工作区")
    workspace = Path(arguments.workspace).expanduser().resolve() if arguments.workspace else Path.cwd()
    approval_mode = ApprovalMode(arguments.mode) if arguments.mode else None
    try:
        settings = Settings.load(
            model=arguments.model, workspace=workspace, approval_mode=approval_mode, ephemeral=arguments.scratch
        )
    except ConfigError as error:
        sys.exit(f"配置有误：{error}")
    return settings


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
        from pocketagent.server.app import run_web
    except ImportError:
        sys.exit("web 界面需要 aiohttp：pip install 'pocketagent[web]'")
    run_web(
        settings,
        host=arguments.host,
        port=arguments.port,
        token=arguments.token or os.environ.get("POCKETAGENT_WEB_TOKEN"),
        require_token=not arguments.no_token,
        open_browser=not arguments.no_browser,
        explicit_workspace=arguments.workspace is not None,
    )


def _run_tui(settings: Settings, session: Session | None, prompt: str, show_reasoning: bool) -> None:
    try:
        from pocketagent.cli.tui import PocketAgentTui
    except ImportError:
        sys.exit("交互式终端需要 textual：pip install 'pocketagent[tui]'")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("--tui 需要在交互式终端里运行；管道或单次执行请用原来的 REPL / -p")
    PocketAgentTui(settings, session=session, first_prompt=prompt or None, show_reasoning=show_reasoning).run()


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
        if arguments.tui:
            sys.exit("--web 和 --tui 只能选一个")
        _run_web(arguments, settings)
        return
    if arguments.tui and arguments.print_mode:
        sys.exit("--tui 和 -p 只能选一个：TUI 是持续交互界面")
    if settings.ephemeral:
        # 真正要开跑了才建目录：--web / --sessions 用不上临时工作区，别在磁盘上留空目录
        settings.workspace = create_scratch_workspace(settings.home_dir)
    session = _pick_session(arguments, settings)
    prompt = " ".join(arguments.prompt).strip()
    if arguments.print_mode:
        if not prompt:
            sys.exit("-p 需要给一句 prompt")
        sys.exit(run_once(settings, session, prompt, arguments.show_reasoning))
    if arguments.tui:
        _run_tui(settings, session, prompt, arguments.show_reasoning)
        return
    Repl(settings, session, show_reasoning=arguments.show_reasoning).run(prompt or None)
