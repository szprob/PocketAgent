"""Textual 交互式终端：只负责渲染事件和收集输入，agent 与权限逻辑仍在核心层。"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, SelectionList, Static, TextArea
from textual.widgets.selection_list import Selection

from pocketagent.cli.controller import CliController
from pocketagent.config import Settings
from pocketagent.core.agent import Agent
from pocketagent.core.events import Event, EventType
from pocketagent.core.session import Session
from pocketagent.interaction.attachments import message_display
from pocketagent.interaction.commands import CommandResult
from pocketagent.interaction.modes import AgentMode, PlanApprovalRequest, PlanDecision, PlanSwitchRequest
from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalRequest


TOOL_OUTPUT_LIMIT = 12000
CUT_OFF_NOTES = {
    "filtered": "回复被上游内容审核掐断，上面是已经输出的部分",
    "incomplete": "连接中途断了，这次回复不完整",
    "truncated": "回复超出长度上限，没有写完",
}


@dataclass
class ToolDisplay:
    widget: Static
    name: str
    summary: str
    output: str = ""
    result: str = ""
    is_error: bool = False
    denied: bool = False
    finished: bool = False
    streamed: bool = False

    def render(self) -> str:
        mark = "✓" if self.finished and not self.is_error and not self.denied else "●"
        if self.is_error:
            mark = "✗"
        if self.denied:
            mark = "⊘"
        lines = [f"{mark} {self.name}  {self.summary}"]
        content = self.output if self.streamed else self.result
        if self.streamed and self.result:
            # 流式输出已经展示过，只补退出码等收尾信息，避免把整段命令输出再画一遍。
            exit_line = next((line for line in self.result.splitlines() if line.startswith("[exit code")), "")
            if exit_line and exit_line not in self.output:
                content = self.output.rstrip() + "\n" + exit_line
        if content:
            if len(content) > TOOL_OUTPUT_LIMIT:
                content = content[:TOOL_OUTPUT_LIMIT] + "\n… 输出过长，其余内容已省略"
            lines.extend("  │ " + line for line in content.splitlines())
        elif self.finished and not self.streamed:
            lines.append("  │ （无输出）")
        self.widget.set_classes(self._css_classes())
        return "\n".join(lines)

    def _css_classes(self) -> str:
        classes = "entry tool"
        if self.is_error:
            classes += " error"
        elif self.denied:
            classes += " warning"
        elif self.finished:
            classes += " done"
        else:
            classes += " running"
        return classes


class PromptArea(TextArea):
    """TextArea 的私有按键处理器先于冒泡执行，所以在这里拦截发送和换行。"""

    async def _on_key(self, event: Key) -> None:
        if event.key in ("shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key != "enter":
            await super()._on_key(event)
            return
        event.stop()
        event.prevent_default()
        text = self.text.strip()
        if not text:
            return
        if self.app.busy:
            self.app.notify("这一轮还在运行；Ctrl-C 可中断", severity="warning")
            return
        self.clear()
        await self.app.submit_text(text)


class TuiRenderer:
    """agent 和界面共用一个常驻事件循环，事件可以同步更新控件。"""

    def __init__(self, app: "PocketAgentTui") -> None:
        self.app = app

    def __call__(self, event: Event) -> None:
        self.app.render_event(event)


class TuiApprover:
    """用模态对话框实现核心层的 Approver，等待回答时界面仍能处理按键。"""

    def __init__(self, app: "PocketAgentTui") -> None:
        self.app = app
        self.pending: set[asyncio.Future[Any]] = set()

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        return await self._ask(ApprovalScreen(request), ApprovalAnswer.DENY)

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        return await self._ask(QuestionScreen(questions), None)

    async def request_plan_switch(self, request: PlanSwitchRequest) -> bool:
        return await self._ask(PlanSwitchScreen(request), False)

    async def request_plan_approval(self, request: PlanApprovalRequest) -> PlanDecision:
        return await self._ask(PlanApprovalScreen(request), PlanDecision(approve=False))

    async def _ask(self, screen: ModalScreen[Any], default: Any) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending.add(future)

        def completed(result: Any) -> None:
            if not future.done():
                future.set_result(result)

        try:
            self.app.push_screen(screen, callback=completed)
            return await future
        except asyncio.CancelledError:
            if self.app.screen is screen:
                screen.dismiss(default)
            return default
        finally:
            self.pending.discard(future)

    def cancel_pending(self) -> None:
        for future in list(self.pending):
            future.cancel()


class ApprovalScreen(ModalScreen[ApprovalAnswer]):
    """一次工具调用的审批；快捷键和控制台 REPL 保持一致。"""

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        request = self.request
        notes = "\n".join("! " + note for note in request.notes)
        with Container(classes="dialog approval-dialog"):
            yield Label(f"需要确认：{request.tool_name}", classes="dialog-title")
            yield Static(request.summary, classes="dialog-summary")
            if notes:
                yield Static(notes, classes="warning")
            yield VerticalScroll(Static(request.preview or "（没有预览）"), classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button("允许一次 [y]", id="allow", variant="primary")
                if request.always_label:
                    yield Button("本任务总是允许 [a]", id="always")
                if request.always_parents_label:
                    yield Button("允许上一级目录 [p]", id="parents")
                yield Button("拒绝 [n]", id="deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        answers = {
            "allow": ApprovalAnswer.ALLOW_ONCE,
            "always": ApprovalAnswer.ALLOW_ALWAYS,
            "parents": ApprovalAnswer.ALLOW_ALWAYS_PARENTS,
            "deny": ApprovalAnswer.DENY,
        }
        self.dismiss(answers[event.button.id])

    def on_key(self, event: Key) -> None:
        shortcuts = {"y": "allow", "a": "always", "p": "parents", "n": "deny", "escape": "deny"}
        button_id = shortcuts.get(event.key)
        if button_id is None:
            return
        try:
            self.query_one(f"#{button_id}", Button)
        except NoMatches:
            return
        event.stop()
        self.dismiss(
            {
                "allow": ApprovalAnswer.ALLOW_ONCE,
                "always": ApprovalAnswer.ALLOW_ALWAYS,
                "parents": ApprovalAnswer.ALLOW_ALWAYS_PARENTS,
                "deny": ApprovalAnswer.DENY,
            }[button_id]
        )


class QuestionScreen(ModalScreen[list[QuestionAnswer] | None]):
    """支持单选、多选和「其他」；空答案表示这一题交给 agent 自己判断。"""

    def __init__(self, questions: list[Question]) -> None:
        super().__init__()
        self.questions = questions

    def compose(self) -> ComposeResult:
        with Container(classes="dialog question-dialog"):
            yield Label("有几个问题想问你", classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                for index, question in enumerate(self.questions):
                    header = f"〔{question.header}〕" if question.header else ""
                    multiple = "（可多选）" if question.multi_select else ""
                    yield Label(f"{index + 1}. {header}{question.question}{multiple}", classes="question-title")
                    selections = [
                        Selection(
                            option.label + (f" — {option.description}" if option.description else ""),
                            option_index,
                        )
                        for option_index, option in enumerate(question.options)
                    ]
                    yield SelectionList(*selections, id=f"question-{index}")
                    yield Input(placeholder="其他（可选，自己写）", id=f"other-{index}")
            with Horizontal(classes="dialog-buttons"):
                yield Button("提交回答", id="submit-answers", variant="primary")
                yield Button("全部跳过", id="skip-answers")

    def on_selection_list_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        index = int(event.selection_list.id.rsplit("-", 1)[-1])
        if self.questions[index].multi_select or len(event.selection_list.selected) <= 1:
            return
        # SelectionList 默认多选；单选题只保留刚点的那项。
        selected = event.selection.value
        event.selection_list.deselect_all()
        event.selection_list.select(selected)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "skip-answers":
            self.dismiss(None)
            return
        answers = []
        for index, question in enumerate(self.questions):
            selected_indexes = self.query_one(f"#question-{index}", SelectionList).selected
            other = self.query_one(f"#other-{index}", Input).value.strip()
            selected = [question.options[option_index].label for option_index in selected_indexes]
            answers.append(QuestionAnswer(selected=selected, other=other))
        self.dismiss(answers)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class PlanSwitchScreen(ModalScreen[bool]):
    """普通模式下建议切到计划模式。"""

    def __init__(self, request: PlanSwitchRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Container(classes="dialog plan-dialog"):
            yield Label("这个任务也许适合先用计划模式", classes="dialog-title")
            yield Static(self.request.reason, classes="dialog-summary")
            with Horizontal(classes="dialog-buttons"):
                yield Button("用计划模式 [y]", id="use-plan", variant="primary")
                yield Button("直接开始 [n]", id="skip-plan")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "use-plan")

    def on_key(self, event: Key) -> None:
        if event.key in ("y", "enter"):
            event.stop()
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            event.stop()
            self.dismiss(False)


class PlanApprovalScreen(ModalScreen[PlanDecision]):
    """计划 / 设计模式下把方案交给用户批准；可以写意见让它继续完善。"""

    def __init__(self, request: PlanApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        design = self.request.mode == AgentMode.DESIGN.value
        with Container(classes="dialog plan-dialog"):
            yield Label("设计方案做好了" if design else "计划做好了", classes="dialog-title")
            yield VerticalScroll(Static(self.request.plan), classes="dialog-body")
            yield Input(placeholder="要继续完善的话，写下你的意见（可空）", id="plan-feedback")
            with Horizontal(classes="dialog-buttons"):
                yield Button("按设计开始实现 [e]" if design else "按计划执行 [e]", id="execute-plan", variant="primary")
                yield Button("继续完善 [r]", id="revise-plan")
                yield Button("先不做 [n]", id="cancel-plan")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._answer(event.button.id)

    def on_key(self, event: Key) -> None:
        # 正在意见框里打字时不要被 e / r / n 抢走
        if isinstance(self.focused, Input) and event.key != "escape":
            return
        shortcuts = {"e": "execute-plan", "r": "revise-plan", "n": "cancel-plan", "escape": "cancel-plan"}
        button_id = shortcuts.get(event.key)
        if button_id is None:
            return
        event.stop()
        self._answer(button_id)

    def _answer(self, button_id: str) -> None:
        feedback = self.query_one("#plan-feedback", Input).value.strip()
        if button_id == "execute-plan":
            self.dismiss(PlanDecision(approve=True))
        elif button_id == "revise-plan":
            self.dismiss(PlanDecision(approve=False, feedback=feedback))
        else:
            self.dismiss(PlanDecision(approve=False))


class PocketAgentTui(App[None]):
    """全屏交互终端；原有 line-based REPL 仍是无额外依赖的默认界面。"""

    TITLE = "PocketAgent"
    CSS = """
    Screen { background: $background; color: $text; }
    #status { height: 3; padding: 1 2; background: $panel; color: $text-muted; }
    #conversation { height: 1fr; padding: 1 2; scrollbar-gutter: stable; }
    .entry { width: 100%; margin-bottom: 1; }
    .user { padding: 1 2; background: $boost; color: $text; }
    .assistant { padding: 0 1; color: $text; }
    .reasoning, .system { color: $text-muted; }
    .tool { padding: 0 1; color: $text-muted; background: $panel; }
    .tool.error, .error { color: $error; }
    .warning { color: $warning; }
    .tool.done { color: $success; }
    #composer { height: auto; padding: 0 2; background: $panel; }
    #prompt { height: 5; border: tall $primary; background: $background; }
    #hint { height: 1; color: $text-muted; }
    Footer { background: $panel; }
    .dialog { width: 90%; max-width: 110; height: 85%; padding: 1 2; background: $surface; border: thick $primary; }
    .approval-dialog { align: center middle; }
    .question-dialog { align: center middle; }
    .plan-dialog { align: center middle; height: auto; max-height: 85%; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-summary { margin-bottom: 1; }
    .dialog-body { height: 1fr; margin: 1 0; }
    .dialog-buttons { height: 3; align: center middle; }
    .dialog-buttons Button { margin-right: 1; }
    .question-title { margin-top: 1; text-style: bold; }
    .question-dialog SelectionList { height: auto; max-height: 8; }
    ApprovalScreen, QuestionScreen, PlanSwitchScreen, PlanApprovalScreen { background: $background 70%; }
    """
    BINDINGS = [
        ("ctrl+c", "interrupt", "中断本轮"),
        ("ctrl+l", "clear", "清屏"),
        ("ctrl+q", "quit", "退出"),
    ]

    def __init__(
        self,
        settings: Settings,
        session: Session | None = None,
        first_prompt: str | None = None,
        show_reasoning: bool = False,
        agent: Agent | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.first_prompt = first_prompt
        self.show_reasoning = show_reasoning
        self.approver = TuiApprover(self)
        self.renderer = TuiRenderer(self)
        self.controller = CliController(settings, self.approver, self.renderer, session=session, agent=agent)
        self.busy = False
        self.turn_task: asyncio.Task[Any] | None = None
        self._closing = False
        self.current_assistant: Static | None = None
        self.current_assistant_text = ""
        self.current_reasoning: Static | None = None
        self.current_reasoning_text = ""
        self.tool_displays: dict[str, ToolDisplay] = {}

    @property
    def agent(self) -> Agent:
        return self.controller.agent

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield VerticalScroll(id="conversation")
        with Container(id="composer"):
            yield PromptArea(id="prompt", tab_behavior="focus", placeholder="给 PocketAgent 一句话，或输入 /help")
            yield Static("Enter 发送 · Shift+Enter / Ctrl+J 换行 · /help 命令 · Ctrl-C 中断 · Ctrl-Q 退出", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.render_history()
        self.update_status()
        self.query_one("#prompt", TextArea).focus()
        if self.first_prompt:
            self.call_after_refresh(self.submit_text, self.first_prompt)

    async def on_unmount(self) -> None:
        self._closing = True
        self.approver.cancel_pending()
        task = self.turn_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # 退出前跑一次 session_end 钩子（归档、清理临时文件这类收尾）
        await self.agent.close()

    async def submit_text(self, text: str) -> None:
        """普通消息启动后台轮次；斜杠命令先在 UI 事件循环里处理。"""
        if self.busy:
            self.notify("这一轮还在运行；Ctrl-C 可中断", severity="warning")
            return
        if not text.startswith("/"):
            self.start_turn(text)
            return
        self.set_busy(True)
        try:
            result = await self.controller.run_command(text)
        finally:
            self.set_busy(False)
        if result is None:
            self.start_turn(text)
            return
        self.add_entry(text, "user")
        self.show_command_result(result)
        await self.apply_command_action(result)

    def show_command_result(self, result: CommandResult) -> None:
        if result.text:
            classes = "entry error" if result.kind == "error" else f"entry {result.kind}"
            self.add_entry(result.text, classes)

    async def apply_command_action(self, result: CommandResult) -> None:
        if result.action == "exit":
            self.exit()
        elif result.action == "new_session":
            self.controller.start_new_session()
            await self.clear_conversation()
            self.add_entry(f"已开新会话 {self.agent.session.id}", "entry system")
            self.render_welcome()
        elif result.action == "compact":
            self.set_busy(True)
            try:
                await self.agent.compact()
            finally:
                self.set_busy(False)
        elif result.action == "switch_mode":
            self.controller.switch_mode(result.argument)
        elif result.action == "switch_agent_mode":
            await self.agent.set_agent_mode(result.argument)
        elif result.action == "switch_model":
            self.controller.switch_model(result.argument)
        elif result.action == "open_session":
            self.controller.open_session(result.argument)
            await self.clear_conversation()
            self.render_history()
        self.update_status()

    def start_turn(self, text: str) -> None:
        self.set_busy(True)
        self.turn_task = asyncio.create_task(self._execute_turn(text))

    async def _execute_turn(self, text: str) -> None:
        try:
            await self.agent.run_turn(text)
        except asyncio.CancelledError:
            if not self._closing:
                self.add_entry("已中断，这一轮的历史已保留，可以接着说", "entry warning")
        except Exception as error:
            if not self._closing:
                self.add_entry(f"运行失败：{type(error).__name__}: {error}", "entry error")
        finally:
            self.turn_task = None
            if not self._closing:
                self._finish_turn()

    def _finish_turn(self) -> None:
        self.current_assistant = None
        self.current_reasoning = None
        self.set_busy(False)
        self.query_one("#prompt", TextArea).focus()

    def action_interrupt(self) -> None:
        task = self.turn_task
        if task is not None and not task.done():
            task.cancel()
            self.update_status("正在中断当前轮…")

    async def action_clear(self) -> None:
        await self.clear_conversation()
        self.render_welcome()

    async def clear_conversation(self) -> None:
        self.current_assistant = None
        self.current_reasoning = None
        self.tool_displays.clear()
        await self.query_one("#conversation", VerticalScroll).remove_children()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.update_status()

    def update_status(self, state: str | None = None) -> None:
        try:
            status = self.query_one("#status", Static)
        except NoMatches:
            return
        mode = self.agent.context.permissions.mode.value
        agent_mode = f" · {self.agent.agent_mode.label} 模式"
        directory = "临时目录" if self.settings.ephemeral else str(Path(self.settings.workspace))
        current_state = state or ("正在运行 · Ctrl-C 中断" if self.busy else "就绪")
        text = (
            f"PocketAgent  ·  {current_state}\n"
            f"{self.settings.display_model()} · {mode}{agent_mode} · {directory} · 会话 {self.agent.session.id}"
        )
        status.update(text)
    def add_entry(self, text: str, classes: str = "entry system") -> Static:
        widget = Static(Text(text), classes=classes)
        conversation = self.query_one("#conversation", VerticalScroll)
        conversation.mount(widget)
        self.call_after_refresh(self._scroll_to_end)
        return widget

    def _scroll_to_end(self) -> None:
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    def _start_assistant(self, reasoning: bool = False) -> Static:
        if reasoning:
            self.current_reasoning_text = ""
            self.current_reasoning = self.add_entry("", "entry reasoning")
            return self.current_reasoning
        self.current_assistant_text = ""
        self.current_assistant = self.add_entry("", "entry assistant")
        self.current_reasoning = None
        return self.current_assistant

    def render_event(self, event: Event) -> None:
        """只在 Textual 主线程执行；事件类型和 ConsoleRenderer 共用核心协议。"""
        match event.type:
            case EventType.TURN_START:
                self.current_assistant = None
                self.current_reasoning = None
                self.tool_displays.clear()
                self.add_entry(event.data.get("user_text") or "", "entry user")
            case EventType.TEXT_DELTA:
                if self.current_assistant is None:
                    self._start_assistant()
                self.current_assistant_text += event.data["text"]
                self.current_assistant.update(Text(self.current_assistant_text))
                self._scroll_to_end()
            case EventType.REASONING_DELTA:
                if not self.show_reasoning:
                    return
                if self.current_reasoning is None:
                    self._start_assistant(reasoning=True)
                self.current_reasoning_text += event.data["text"]
                self.current_reasoning.update(Text(self.current_reasoning_text))
            case EventType.TOOL_CALL:
                self.current_assistant = None
                self.current_reasoning = None
                name = event.data["name"]
                summary = event.data.get("summary") or ""
                widget = self.add_entry("", "entry tool running")
                display = ToolDisplay(widget=widget, name=name, summary=summary)
                self.tool_displays[event.data["call_id"]] = display
                widget.update(Text(display.render()))
            case EventType.TOOL_OUTPUT_DELTA:
                display = self.tool_displays.get(event.data["call_id"])
                if display is not None:
                    display.streamed = True
                    display.output += event.data["text"]
                    display.widget.update(Text(display.render()))
                    self._scroll_to_end()
            case EventType.TOOL_RESULT:
                display = self.tool_displays.get(event.data["call_id"])
                if display is not None:
                    display.result = event.data.get("content") or ""
                    display.is_error = bool(event.data.get("is_error"))
                    display.denied = bool(event.data.get("denied"))
                    display.finished = True
                    display.widget.update(Text(display.render()))
            case EventType.SUBAGENT_PROGRESS:
                mark = "✗" if event.data.get("step") == "tool_error" else "↳"
                summary = event.data.get("summary", "")
                self.add_entry(f"{mark} 子 agent {event.data.get('name', '')} {summary}", "entry system")
            case EventType.LLM_RETRY:
                self.add_entry(f"模型调用失败，第 {event.data['attempt']} 次尝试：{event.data['error']}", "entry warning")
            case EventType.COMPACTION:
                self.add_entry(
                    f"上下文已压缩：约 {event.data['tokens_before']} → {event.data['tokens_after']} tokens",
                    "entry system",
                )
            case EventType.AGENT_MODE:
                mode = AgentMode(event.data["mode"])
                note = {
                    AgentMode.AGENT: "已切回 Agent 模式：可以动手了",
                    AgentMode.ASK: "进入 Ask 模式：只回答问题，不改动",
                    AgentMode.PLAN: "进入计划模式：只读调研，计划批准后再动手",
                    AgentMode.DESIGN: "进入设计模式：只读调研，设计方案批准后再实现",
                }[mode]
                self.add_entry(note, "entry system")
            case EventType.STEP_LIMIT:
                self.add_entry(f"这一轮已调用模型 {event.data['max_steps']} 次，达到上限，先停下", "entry warning")
            case EventType.ERROR:
                self.add_entry(f"出错：{event.data['message']}", "entry error")
            case EventType.TURN_END:
                self.current_assistant = None
                self.current_reasoning = None
                note = CUT_OFF_NOTES.get(event.data.get("stop_reason") or "")
                if note:
                    self.add_entry(note, "entry warning")
        self._scroll_to_end()

    def render_history(self) -> None:
        messages = [message for message in self.agent.session.messages if message.get("role") in ("user", "assistant")]
        for message in messages:
            role = message.get("role")
            if role == "user":
                if message.get("_kind"):
                    continue
                text = message_display(message)["text"]
                if text:
                    self.add_entry(text, "entry user")
            else:
                text = _assistant_text(message)
                if text:
                    self.add_entry(text, "entry assistant")
        if not messages:
            self.render_welcome()
        else:
            self.add_entry(f"已恢复会话 {self.agent.session.id}", "entry system")

    def render_welcome(self) -> None:
        self.add_entry("直接说要做什么；/help 查看命令，Enter 发送，Shift+Enter / Ctrl+J 换行。", "entry system")


def _assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(text_parts)
    return ""
