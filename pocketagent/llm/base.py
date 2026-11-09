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
    # 厂商挂在工具调用上、要求原样带回的字段：Gemini 的思考签名在 extra_content.google.thought_signature
    extra_content: dict[str, Any] | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # prompt 里命中缓存的部分（DeepSeek、Grok 等会报；没报就是 0）
    cached_tokens: int = 0
    # completion 里花在思考上的部分
    reasoning_tokens: int = 0
    # 接口直接报的费用（美元，OpenRouter 会报）；没报是 None
    cost: float | None = None


@dataclass
class LLMResponse:
    text: str = ""
    # 思考内容的文字（界面显示用，也作为 reasoning_content 传回 DeepSeek 这类接口）
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None
    # 带签名 / 加密的思考块，各家要求原样传回（见 REASONING_DETAILS_FIELD / THINKING_BLOCKS_FIELD）
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    thinking_blocks: list[dict[str, Any]] = field(default_factory=list)


class StreamListener(Protocol):
    """provider 每收到一段增量就回调一次，用来把流式输出转成事件。"""

    async def on_text(self, text: str) -> None: ...

    async def on_reasoning(self, text: str) -> None: ...

    async def on_retry(self, attempt: int, error: Exception, wait_seconds: float) -> None:
        """要重试了：前面流出来的半截输出作废。"""


class ContextOverflowError(RuntimeError):
    """接口说输入超过了模型的上下文长度。ThinkNode 收到它会先压缩再重试。"""


CONTEXT_OVERFLOW_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "token limit",
    "上下文长度",
    "超出最大长度",
    "超过最大长度",
)


def is_context_overflow(error: Exception) -> bool:
    message = str(error).lower()
    return any(hint in message for hint in CONTEXT_OVERFLOW_HINTS)


class SilentListener:
    """不需要把流式输出展示出来的调用（压缩摘要、测试连接）用它。"""

    async def on_text(self, text: str) -> None:
        return None

    async def on_reasoning(self, text: str) -> None:
        return None

    async def on_retry(self, attempt: int, error: Exception, wait_seconds: float) -> None:
        return None


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse: ...


# assistant 消息里存模型思考内容的字段。带工具的请求要把这一轮之前几步的思考原样传回，各家都这么要求：
# 不传的话模型每调一次工具都从头想一遍，忘了已经查到什么、本来打算怎么做，同样的搜索反复搜。
# 各家要的格式不一样，所以分几个字段存，由各自的 provider 挑自己认的发、其余的去掉：
# - reasoning_content：思考的文字，DeepSeek、智谱、Kimi 这类 OpenAI 兼容接口直接认
# - reasoning_details：OpenRouter 的思考块数组（带签名 / 加密内容），原样传回
# - thinking_blocks：Anthropic 的 thinking / redacted_thinking 块（带签名），每块记着它在原回复里排第几
# - Gemini 的签名挂在工具调用上（tool_calls[].extra_content），见 ToolCall.extra_content
# 之前几轮（最后一条用户消息之前）的思考各家说法不一：DeepSeek 不需要，智谱要另开「保留式思考」才用得上，
# 所以默认只传这一轮的（without_earlier_reasoning），设置里打开「跨轮保留思考内容」才全部传。
REASONING_FIELD = "reasoning_content"
REASONING_DETAILS_FIELD = "reasoning_details"
THINKING_BLOCKS_FIELD = "thinking_blocks"
REASONING_FIELDS = (REASONING_FIELD, REASONING_DETAILS_FIELD, THINKING_BLOCKS_FIELD)


def without_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉 assistant 消息里所有格式的思考内容和工具调用上的签名，不改原消息。"""
    cleaned = []
    for message in messages:
        if any(name in message for name in REASONING_FIELDS):
            message = {key: value for key, value in message.items() if key not in REASONING_FIELDS}
        if any("extra_content" in call for call in message.get("tool_calls") or []):
            message = {**message, "tool_calls": without_tool_call_extras(message["tool_calls"])}
        cleaned.append(message)
    return cleaned


def without_tool_call_extras(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in call.items() if key != "extra_content"} for call in tool_calls]


def without_earlier_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只留最后一条用户消息之后（这一轮工具调用之间）的思考内容，更早的去掉。不改原消息。
    PocketAgent 自己插的提示（_kind：模式切换、截断说明等）不算用户消息，不当成新一轮的开始。"""
    last_user_index = -1
    for index, message in enumerate(messages):
        if message.get("role") == "user" and not message.get("_kind"):
            last_user_index = index
    earlier = without_reasoning(messages[: last_user_index + 1])
    return earlier + messages[last_user_index + 1 :]


def build_assistant_message(response: LLMResponse) -> dict[str, Any]:
    """把一次模型回复转成要追加进对话历史的 assistant 消息，思考内容一起留下（见 REASONING_FIELD）。"""
    content: str | None = response.text
    if not content and response.tool_calls:
        # 只调工具不说话时 OpenAI 格式要求 content 为 null
        content = None
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if response.tool_calls:
        message["tool_calls"] = [_tool_call_entry(call) for call in response.tool_calls]
    if response.reasoning:
        message[REASONING_FIELD] = response.reasoning
    if response.reasoning_details:
        message[REASONING_DETAILS_FIELD] = response.reasoning_details
    if response.thinking_blocks:
        message[THINKING_BLOCKS_FIELD] = response.thinking_blocks
    return message


def _tool_call_entry(call: ToolCall) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.raw_arguments or "{}"},
    }
    if call.extra_content:
        entry["extra_content"] = call.extra_content
    return entry
