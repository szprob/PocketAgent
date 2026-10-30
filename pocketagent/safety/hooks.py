"""钩子：在几个固定时机跑用户自己的脚本。

时机（配置里的 event）：
- session_start：一个任务的第一轮开始之前（续跑不算）
- user_prompt_submit：用户发来一句话，这一轮开始之前；可以补上下文，也可以拦住这一轮
- pre_tool_use：工具调用审批通过、真正执行之前；可以拦住这次调用
- post_tool_use：工具执行完、结果截断之后；可以给模型补一句话，或者指出问题
- subagent_stop：子 agent 干完、把结论交回主 agent 之前
- pre_compact：上下文要压缩之前；脚本可以先把重要结论落盘
- notification：agent 要请你审批、或者要你回答问题之前；适合推手机、响一声
- turn_end：这一轮结束（答完 / 被拒绝 / 出错 / 达到步数上限）
- session_end：任务结束（删任务、服务退出、命令行退出）

脚本从 stdin 拿到一份 JSON（event、session_id、workspace 和各时机自己的字段），环境变量里也有常用的几个，
工作目录就是任务的工作区。退出码 0 表示通过，stdout 是 JSON 时读 decision / reason / additional_context；
退出码 2 表示拦住，stderr 是原因；其他值表示钩子自己出错，只在界面上提示，不拦。

钩子是用户自己配的，和 MCP 服务器一样按用户的权限跑：不进沙箱、不要审批。
它本来就是用来看住 agent 的，再让 agent 去批准它就说不通了。
"""

import asyncio
import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pocketagent.core.events import EventBus, EventType
from pocketagent.interaction.modes import PlanApprovalRequest, PlanDecision, PlanSwitchRequest
from pocketagent.interaction.questions import Question, QuestionAnswer
from pocketagent.safety.permissions import ApprovalAnswer, ApprovalRequest, ask_approvals
from pocketagent.safety.processes import bash_executable, terminate_process_tree

if TYPE_CHECKING:
    from pocketagent.config import Settings

HOOK_EVENTS = (
    "session_start",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "subagent_stop",
    "pre_compact",
    "notification",
    "turn_end",
    "session_end",
)
# 只有这两个时机按工具名匹配；其余时机 matcher 没有意义
TOOL_EVENTS = ("pre_tool_use", "post_tool_use")
DEFAULT_HOOK_TIMEOUT_SECONDS = 30
MAX_HOOK_TIMEOUT_SECONDS = 600
# 钩子说的话要进上下文，太长会挤掉正经内容
MAX_HOOK_TEXT_CHARS = 4000
COMMAND_PREVIEW_CHARS = 200


@dataclass
class HookDefinition:
    """配置里的一条钩子。"""

    event: str
    command: str
    # 工具名，支持 * 通配、用 | 或 , 分隔多个（如 "edit_file|write_file"、"mcp__*"）；空表示全部
    matcher: str = ""
    timeout_seconds: int = DEFAULT_HOOK_TIMEOUT_SECONDS
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "HookDefinition":
        if not isinstance(data, dict):
            raise ValueError("每条钩子都要是一个 JSON 对象（{...}）")
        event = str(data.get("event") or "").strip()
        if event not in HOOK_EVENTS:
            raise ValueError(f"钩子的时机只能是 {' / '.join(HOOK_EVENTS)}：{event or '（空）'}")
        command = str(data.get("command") or "").strip()
        if not command:
            raise ValueError(f"{event} 钩子的命令不能为空")
        timeout = data.get("timeout_seconds")
        if timeout in (None, ""):
            timeout_seconds = DEFAULT_HOOK_TIMEOUT_SECONDS
        else:
            try:
                timeout_seconds = int(timeout)
            except (TypeError, ValueError):
                raise ValueError(f"{event} 钩子的超时必须是整数秒：{timeout}")
            if not 1 <= timeout_seconds <= MAX_HOOK_TIMEOUT_SECONDS:
                raise ValueError(f"{event} 钩子的超时要在 1–{MAX_HOOK_TIMEOUT_SECONDS} 秒之间：{timeout}")
        return cls(
            event=event,
            command=command,
            matcher=str(data.get("matcher") or "").strip(),
            timeout_seconds=timeout_seconds,
            enabled=_boolean(data.get("enabled"), default=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "matcher": self.matcher,
            "command": self.command,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
        }

    def matches(self, tool_name: str) -> bool:
        if self.event not in TOOL_EVENTS:
            return True
        pattern = self.matcher.strip()
        if not pattern or pattern == "*":
            return True
        parts = [part.strip() for part in re.split(r"[|,]", pattern) if part.strip()]
        return any(fnmatch.fnmatchcase(tool_name, part) for part in parts)


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass
class HookOutcome:
    """一批钩子跑完的结论：拦没拦住、要补给模型的话、以及钩子自己的毛病。"""

    blocked: bool = False
    reason: str = ""
    additional_context: str = ""
    # 超时、退出码非 0 这类：只提示用户，不影响这一轮
    problems: list[str] = field(default_factory=list)


@dataclass
class HookRunner:
    """按配置跑钩子。设置改了不用重建：每次都从 settings.hooks 现取。"""

    settings: "Settings"
    session_id: str
    events: EventBus

    def definitions(self, event: str, tool_name: str = "") -> list[HookDefinition]:
        return [
            hook
            for hook in self.settings.hooks
            if hook.enabled and hook.event == event and hook.matches(tool_name)
        ]

    async def run(self, event: str, payload: dict[str, Any], tool_name: str = "") -> HookOutcome:
        """按配置顺序跑这个时机上的钩子，遇到拦住就停：后面的钩子不该再对着已经作废的调用干活。"""
        outcome = HookOutcome()
        definitions = self.definitions(event, tool_name)
        if not definitions:
            return outcome
        body = {
            **payload,
            "event": event,
            "session_id": self.session_id,
            "workspace": str(self.settings.workspace),
        }
        for definition in definitions:
            try:
                result = await self._run_one(definition, body, tool_name)
            except Exception as error:
                # 钩子出什么错都不能把 agent 带崩：记成问题，界面上提示一下
                result = HookOutcome(problems=[f"{type(error).__name__}: {error}"])
            outcome.problems.extend(result.problems)
            if result.additional_context:
                outcome.additional_context = "\n".join(
                    part for part in (outcome.additional_context, result.additional_context) if part
                )
            if result.blocked:
                outcome.blocked = True
                outcome.reason = result.reason
                break
        return outcome

    async def _run_one(self, definition: HookDefinition, payload: dict[str, Any], tool_name: str) -> HookOutcome:
        started_at = time.monotonic()
        outcome = await self._execute(definition, payload, tool_name)
        await self.events.emit(
            EventType.HOOK,
            event=definition.event,
            tool_name=tool_name,
            command=_clip(definition.command, COMMAND_PREVIEW_CHARS),
            blocked=outcome.blocked,
            reason=outcome.reason,
            problem="；".join(outcome.problems),
            seconds=round(time.monotonic() - started_at, 2),
        )
        return outcome

    async def _execute(self, definition: HookDefinition, payload: dict[str, Any], tool_name: str) -> HookOutcome:
        bash = bash_executable()
        if bash is None:
            return HookOutcome(problems=["这台机器上找不到 bash，钩子没有执行"])
        try:
            process = await asyncio.create_subprocess_exec(
                bash,
                "-c",
                definition.command,
                cwd=str(self.settings.workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(definition, tool_name),
                # 单独的进程组：超时能连脚本派生出来的子进程一起结束
                start_new_session=True,
            )
        except OSError as error:
            return HookOutcome(problems=[f"起不来：{type(error).__name__}: {error}"])
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload_bytes), timeout=definition.timeout_seconds
            )
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            return HookOutcome(problems=[f"超过 {definition.timeout_seconds} 秒没结束，已终止"])
        return _read_result(process.returncode, stdout, stderr)

    def _environment(self, definition: HookDefinition, tool_name: str) -> dict[str, str]:
        """把常用的字段也放进环境变量，简单脚本不用去解析 JSON。"""
        environment = dict(os.environ)
        environment["POCKETAGENT_EVENT"] = definition.event
        environment["POCKETAGENT_SESSION_ID"] = self.session_id
        environment["POCKETAGENT_WORKSPACE"] = str(self.settings.workspace)
        environment["POCKETAGENT_HOME"] = str(self.settings.home_dir)
        if tool_name:
            environment["POCKETAGENT_TOOL_NAME"] = tool_name
        return environment


def _read_result(exit_code: int | None, stdout: bytes, stderr: bytes) -> HookOutcome:
    output = stdout.decode("utf-8", "replace").strip()
    error_output = stderr.decode("utf-8", "replace").strip()
    if exit_code == 2:
        # 和 Claude Code 一样：退出码 2 是「拦住」，stderr 当作理由
        return HookOutcome(blocked=True, reason=_clip(error_output or output or "钩子拦住了这次调用"))
    if exit_code != 0:
        detail = error_output or output or "没有输出"
        return HookOutcome(problems=[f"退出码 {exit_code}：{_clip(detail, COMMAND_PREVIEW_CHARS)}"])
    blocked, reason, additional_context = _read_json_output(output)
    return HookOutcome(blocked=blocked, reason=reason, additional_context=additional_context)


def _read_json_output(output: str) -> tuple[bool, str, str]:
    """退出码 0 时 stdout 可以是 JSON：{"decision": "block", "reason": "...", "additional_context": "..."}。"""
    if not output.startswith("{"):
        return False, "", ""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return False, "", ""
    if not isinstance(data, dict):
        return False, "", ""
    blocked = str(data.get("decision") or "").strip() == "block"
    reason = _clip(str(data.get("reason") or ""))
    additional_context = _clip(str(data.get("additional_context") or ""))
    return blocked, reason, additional_context


def _clip(text: str, limit: int = MAX_HOOK_TEXT_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…（截断）"


class NotifyingApprover:
    """在请你审批 / 回答之前跑一次 notification 钩子：界面马上要等你了，可以推手机、响一声。

    通知本身不改变审批结论——钩子拦不住也放行不了，只是让别的渠道知道你在被等。
    包在审批通道外面，命令行、TUI、web、桌面端和子 agent 的审批都走这一层。
    """

    def __init__(self, approver: Any, hooks: HookRunner) -> None:
        self.approver = approver
        self.hooks = hooks

    async def request_approval(self, request: ApprovalRequest) -> ApprovalAnswer:
        await self._notify("approval", tools=[request.tool_name], summary=request.summary)
        return await self.approver.request_approval(request)

    async def request_approvals(self, requests: list[ApprovalRequest]) -> list[ApprovalAnswer]:
        # 一批合成一张卡片时只通知一次，别一次弹五条
        await self._notify(
            "approval",
            tools=[request.tool_name for request in requests],
            summary=requests[0].summary if requests else "",
        )
        return list(await ask_approvals(self.approver, requests))

    async def ask_questions(self, questions: list[Question]) -> list[QuestionAnswer] | None:
        await self._notify("question", questions=[question.question for question in questions])
        return await self.approver.ask_questions(questions)

    async def request_plan_switch(self, request: PlanSwitchRequest) -> bool:
        """计划模式的「要不要先规划一下」也是一次等你拍板，一起通知。"""
        await self._notify("plan_switch", reason=request.reason)
        ask = getattr(self.approver, "request_plan_switch", None)
        if ask is None:
            return False
        return bool(await ask(request))

    async def request_plan_approval(self, request: PlanApprovalRequest) -> PlanDecision:
        await self._notify("plan_approval", mode=request.mode)
        ask = getattr(self.approver, "request_plan_approval", None)
        if ask is None:
            return PlanDecision(approve=False)
        return await ask(request)

    def __getattr__(self, name: str) -> Any:
        """审批通道上的其他可选能力原样转发：包一层不能把它们藏起来。"""
        approver = self.__dict__.get("approver")
        if approver is None:
            raise AttributeError(name)
        return getattr(approver, name)

    async def _notify(self, kind: str, **payload: Any) -> None:
        # 通知失败不能挡住审批：这里的问题已经由 HookRunner 收进 outcome，不用再管
        await self.hooks.run("notification", {"kind": kind, **payload})
