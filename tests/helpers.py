"""测试公用：在临时目录里搭一个用假模型的 agent，并记录它发出的全部事件。"""

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pocketagent.agent import Agent, create_agent
from pocketagent.config import Settings
from pocketagent.events import Event, EventType
from pocketagent.llm import LLMResponse, ScriptedProvider, ToolCall
from pocketagent.permissions import AllowAllApprover, ApprovalMode, Approver
from pocketagent.session import Session
from pocketagent.tools import ToolRegistry


@dataclass
class EventRecorder:
    events: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[Event]:
        return [event for event in self.events if event.type == event_type]


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
    **setting_overrides,
) -> AgentHarness:
    settings = Settings(
        workspace=directories.workspace,
        home_dir=directories.home_dir,
        approval_mode=mode,
        **setting_overrides,
    )
    provider = ScriptedProvider(responses)
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
