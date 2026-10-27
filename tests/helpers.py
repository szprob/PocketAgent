"""测试公用：在临时目录里搭一个用假模型的 agent，并记录它发出的全部事件。"""

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pocketagent.config import Settings
from pocketagent.core.agent import Agent, create_agent
from pocketagent.core.compaction import MERGE_SUMMARY_INSTRUCTION, SUMMARY_INSTRUCTION
from pocketagent.core.events import Event, EventType
from pocketagent.core.session import Session
from pocketagent.llm import ContextOverflowError, LLMResponse, ScriptedProvider, ToolCall
from pocketagent.safety.permissions import AllowAllApprover, ApprovalMode, Approver
from pocketagent.tools import ToolRegistry


@dataclass
class EventRecorder:
    events: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[Event]:
        return [event for event in self.events if event.type == event_type]


class RoutingProvider:
    """摘要请求和普通对话分开处理：摘要请求（最后一条是压缩指令）返回固定摘要，可以用 gate 卡住；
    普通对话按剧本返回，剧本里放 ContextOverflowError 实例就抛出来，模拟接口报上下文超长。"""

    def __init__(self, chat_responses: list, summary_text: str = "这是摘要") -> None:
        self.chat_responses = list(chat_responses)
        self.summary_text = summary_text
        self.summary_gate = asyncio.Event()
        self.summary_gate.set()
        self.chat_calls: list[list[dict]] = []
        self.summary_calls = 0
        self.summary_requests: list[list[dict]] = []

    async def complete(self, messages, tools, listener) -> LLMResponse:
        # 真的 HTTP 调用会让出事件循环，后台任务才有机会跑；这里也让一下
        await asyncio.sleep(0)
        if messages and messages[-1].get("content") in (SUMMARY_INSTRUCTION, MERGE_SUMMARY_INSTRUCTION):
            self.summary_calls += 1
            self.summary_requests.append(messages)
            await self.summary_gate.wait()
            return LLMResponse(text=self.summary_text)
        self.chat_calls.append(messages)
        response = self.chat_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response.text:
            await listener.on_text(response.text)
        return response


@dataclass
class AgentHarness:
    agent: Agent
    provider: ScriptedProvider
    recorder: EventRecorder
    workspace: Path
    settings: Settings


class TemporaryDirectories:
    """每个测试一套独立的工作目录和 ~/.pocketagent。"""

    def __init__(self) -> None:
        self._root = tempfile.TemporaryDirectory()
        root = Path(self._root.name).resolve()
        self.workspace = root / "workspace"
        self.home_dir = root / "home"
        self.workspace.mkdir()

    def cleanup(self) -> None:
        self._root.cleanup()


def build_harness(
    directories: TemporaryDirectories,
    responses: list[LLMResponse],
    mode: ApprovalMode = ApprovalMode.FULL_AUTO,
    approver: Approver | None = None,
    tools: ToolRegistry | None = None,
    session: Session | None = None,
    provider=None,
    **setting_overrides,
) -> AgentHarness:
    # 默认不去读本机 ~/.claude/skills，测试结果不受机器上装了什么技能影响
    setting_overrides.setdefault("skills_enabled", False)
    settings = Settings(
        workspace=directories.workspace,
        home_dir=directories.home_dir,
        approval_mode=mode,
        **setting_overrides,
    )
    provider = provider or ScriptedProvider(responses)
    agent = create_agent(
        settings, approver=approver or AllowAllApprover(), provider=provider, tools=tools, session=session
    )
    recorder = EventRecorder()
    agent.context.events.subscribe(recorder)
    return AgentHarness(
        agent=agent, provider=provider, recorder=recorder, workspace=directories.workspace, settings=settings
    )


def tool_call(call_id: str, name: str, arguments_json: str) -> ToolCall:
    """按模型的真实输出方式造一个工具调用：参数是 JSON 字符串。"""
    return ToolCall(id=call_id, name=name, arguments=json.loads(arguments_json), raw_arguments=arguments_json)


def roles(messages: list[dict]) -> list[str]:
    return [message["role"] for message in messages]
