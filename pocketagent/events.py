"""事件总线：agent 运行过程中的一切（流式文本、工具调用、节点进出）都以事件发出。

PocketFlow 的节点 exec 只能一次性返回结果，没有框架级的流式；这里用旁路的事件总线补上。
界面（CLI / 以后的 TUI、IDE 插件、Web）和 trace 都只是事件的订阅者。
"""

import inspect
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Awaitable, Callable


class EventType(str, Enum):
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    NODE_START = "node_start"
    NODE_END = "node_end"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    LLM_RESPONSE = "llm_response"
    LLM_RETRY = "llm_retry"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMPACTION = "compaction"
    STEP_LIMIT = "step_limit"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_record(self) -> dict[str, Any]:
        """转成可以 json.dumps 的扁平字典（配合 json_default），trace 和 web 前端共用这个格式。"""
        return {"type": self.type.value, "time": self.timestamp, **self.data}


def json_default(value: Any) -> Any:
    """json.dumps 的 default：事件里可能带 dataclass（如 Usage）、Enum、Path。"""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


EventHandler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """按订阅顺序把事件依次交给每个处理函数，同步、异步函数都可以。"""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def emit(self, event_type: EventType, **data: Any) -> None:
        event = Event(type=event_type, data=data)
        for handler in self._handlers:
            outcome = handler(event)
            if inspect.isawaitable(outcome):
                await outcome
