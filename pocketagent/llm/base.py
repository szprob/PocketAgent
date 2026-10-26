"""模型接口的公共类型。

内部消息一律用 OpenAI chat 格式（role / content / tool_calls / tool_call_id），
new-api、vLLM、DeepSeek、Qwen 网关都直接吃这个格式；以后接 Anthropic 等协议时在各自的 provider 里转换。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    # 模型给的参数不是合法 JSON 时记下原因，由工具节点把错误回给模型
    arguments_error: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class LLMResponse:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None


class StreamListener(Protocol):
    """provider 每收到一段增量就回调一次，用来把流式输出转成事件。"""

    async def on_text(self, text: str) -> None: ...

    async def on_reasoning(self, text: str) -> None: ...


class SilentListener:
    """不需要把流式输出展示出来的调用（压缩摘要、测试连接）用它。"""

    async def on_text(self, text: str) -> None:
        return None

    async def on_reasoning(self, text: str) -> None:
        return None


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse: ...


def build_assistant_message(response: LLMResponse) -> dict[str, Any]:
    """把一次模型回复转成要追加进对话历史的 assistant 消息。思考内容不回传给模型。"""
    content: str | None = response.text
    if not content and response.tool_calls:
        # 只调工具不说话时 OpenAI 格式要求 content 为 null
        content = None
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments or "{}"},
            }
            for call in response.tool_calls
        ]
    return message
