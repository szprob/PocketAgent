"""Anthropic Messages 接口（api.anthropic.com/v1/messages 以及兼容网关）的流式调用。

内部消息是 OpenAI chat 格式，这里在请求前转成 Anthropic 的结构：
- system 消息提到顶层 `system`，其余消息转成 user / assistant 的 content blocks
- assistant 的 tool_calls 转成 tool_use 块，tool 消息转成 user 里的 tool_result 块
- image_url（data URL）转成 image 源块，OpenAI 的 finish_reason 和 Anthropic 的 stop_reason 互相对应

重试和 OpenAI 兼容 provider 一致：429 / 5xx / 529 / 连接断开重试，400 / 401 直接报；
服务端给了 Retry-After 就按它等。SDK 不用，直接用 httpx 读 SSE，少一个依赖。

Anthropic 的 thinking 块要求原样带回历史，当前消息格式（OpenAI chat）存不下，
所以这里不主动开思考，reasoning_effort 不发给接口；extra_body 里自己写了 thinking 也不做特殊处理。
"""

import asyncio
import copy
import email.utils
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from pocketagent.llm.base import ContextOverflowError, LLMResponse, StreamListener, ToolCall, Usage, is_context_overflow

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192

MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
RETRY_AFTER_CAP_SECONDS = 60.0
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}

# Anthropic 的 stop_reason → 内部用的 finish_reason。max_tokens 要走「被截断」的补救流程。
FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class AnthropicAPIError(RuntimeError):
    """HTTP 层面的错误：状态码和响应头用来判断要不要重试、等多久。"""

    def __init__(self, status_code: int, message: str, headers: Any = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.headers = headers or {}


class AnthropicStreamError(RuntimeError):
    """流里下发的 error 事件（overloaded_error、rate_limit_error 等）。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.retryable = error_type in ("overloaded_error", "rate_limit_error", "api_error")


def messages_url(base_url: str | None) -> str:
    """接口地址可以是 https://api.anthropic.com、带 /v1 的，或者已经写到 /messages 的。"""
    text = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if text.endswith("/messages"):
        return text
    if text.endswith("/v1"):
        return f"{text}/messages"
    return f"{text}/v1/messages"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
    return ""


def _text_block(text: str) -> dict[str, Any] | None:
    # Anthropic 不接受空的 text 块
    return {"type": "text", "text": text} if text else None


def _image_block(url: str) -> dict[str, Any] | None:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        if not data:
            return None
        media_type = header[len("data:") :].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI 的 user / tool content（字符串或分段列表）→ Anthropic content blocks。"""
    if isinstance(content, str):
        block = _text_block(content)
        return [block] if block else []
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        part_type = part.get("type")
        if part_type == "text":
            block = _text_block(str(part.get("text", "")))
        elif part_type == "image_url":
            image_url = part.get("image_url") or {}
            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
            block = _image_block(url)
            if block is None:
                block = _text_block(f"[图片 {url} 没法发给 Anthropic 接口]")
        else:
            block = _text_block(json.dumps(part, ensure_ascii=False))
        if block is not None:
            blocks.append(block)
    return blocks


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = _content_blocks(message.get("content"))
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"tool_{index}",
                "name": function.get("name") or "",
                "input": arguments,
            }
        )
    return blocks


def _tool_result_block(message: dict[str, Any]) -> dict[str, Any] | None:
    tool_use_id = message.get("tool_call_id")
    if not tool_use_id:
        return None
    content = message.get("content")
    if isinstance(content, list):
        blocks = _content_blocks(content)
        value: str | list[dict[str, Any]] = blocks or "（无输出）"
    else:
        value = str(content) if content else "（无输出）"
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": value}


def _append_message(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """Anthropic 允许同一角色连续出现，但合并起来更稳：tool 结果后面常跟着另一条 tool 结果。"""
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """返回 (system, anthropic messages)。system 可能是空字符串。"""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _text_of(message.get("content"))
            if text:
                system_parts.append(text)
        elif role == "user":
            _append_message(converted, "user", _content_blocks(message.get("content")))
        elif role == "assistant":
            _append_message(converted, "assistant", _assistant_blocks(message))
        elif role == "tool":
            block = _tool_result_block(message)
            if block is not None:
                _append_message(converted, "user", [block])
    return "\n\n".join(system_parts), converted


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI function schema → Anthropic 的 name / description / input_schema。"""
    converted = []
    for item in tools:
        function = item.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": function.get("description") or "",
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _tool_call(call_id: str, name: str, raw_arguments: str, initial_input: dict[str, Any]) -> ToolCall:
    text = raw_arguments.strip()
    if not text:
        return ToolCall(id=call_id, name=name, arguments=dict(initial_input))
    arguments: dict[str, Any] = {}
    arguments_error = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            arguments = parsed
        else:
            arguments_error = "参数必须是 JSON 对象"
    except json.JSONDecodeError as error:
        arguments_error = f"参数不是合法 JSON：{error}"
    return ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=text, arguments_error=arguments_error)


@dataclass
class _PartialToolBlock:
    id: str = ""
    name: str = ""
    initial_input: dict[str, Any] = field(default_factory=dict)
    argument_parts: list[str] = field(default_factory=list)


@dataclass
class AnthropicStreamAccumulator:
    """把 SSE 事件拼回一次完整回复。"""

    text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_blocks: dict[int, _PartialToolBlock] = field(default_factory=dict)
    finish_reason: str | None = None
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0

    def build_response(self) -> LLMResponse:
        tool_calls = []
        for index in sorted(self.tool_blocks):
            block = self.tool_blocks[index]
            tool_calls.append(
                _tool_call(
                    block.id or f"tool_{index}",
                    block.name,
                    "".join(block.argument_parts),
                    block.initial_input,
                )
            )
        usage = None
        if self.input_tokens or self.output_tokens or self.cache_read_tokens or self.cache_creation_tokens:
            usage = Usage(
                # 缓存命中的部分也算输入，记账才和接口报的用量对得上
                prompt_tokens=self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens,
                completion_tokens=self.output_tokens,
                cached_tokens=self.cache_read_tokens,
            )
        return LLMResponse(
            text="".join(self.text_parts),
            reasoning="".join(self.reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=self.finish_reason,
            usage=usage,
        )


async def consume_event(
    event: dict[str, Any], accumulator: AnthropicStreamAccumulator, listener: StreamListener
) -> None:
    """处理一条 SSE 事件；error 事件抛出来交给重试逻辑。"""
    event_type = event.get("type")
    if event_type == "message_start":
        usage = ((event.get("message") or {}).get("usage") or {})
        accumulator.input_tokens = int(usage.get("input_tokens") or 0)
        accumulator.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        accumulator.cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    elif event_type == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            index = int(event.get("index") or 0)
            accumulator.tool_blocks[index] = _PartialToolBlock(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                initial_input=dict(block.get("input") or {}),
            )
        text = block.get("text")
        if text:
            accumulator.text_parts.append(str(text))
            await listener.on_text(str(text))
    elif event_type == "content_block_delta":
        delta = event.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = str(delta.get("text") or "")
            accumulator.text_parts.append(text)
            await listener.on_text(text)
        elif delta_type == "thinking_delta":
            thinking = str(delta.get("thinking") or "")
            accumulator.reasoning_parts.append(thinking)
            await listener.on_reasoning(thinking)
        elif delta_type == "input_json_delta":
            index = int(event.get("index") or 0)
            block = accumulator.tool_blocks.setdefault(index, _PartialToolBlock())
            block.argument_parts.append(str(delta.get("partial_json") or ""))
    elif event_type == "message_delta":
        delta = event.get("delta") or {}
        stop_reason = delta.get("stop_reason")
        if stop_reason:
            accumulator.finish_reason = FINISH_REASONS.get(str(stop_reason), str(stop_reason))
        usage = event.get("usage") or {}
        accumulator.output_tokens = int(usage.get("output_tokens") or accumulator.output_tokens)
    elif event_type == "error":
        error = event.get("error") or {}
        message = str(error.get("message") or "接口返回了错误")
        if is_context_overflow(message):
            raise ContextOverflowError(message)
        raise AnthropicStreamError(str(error.get("type") or "error"), message)


def _retry_after_seconds(headers: Any) -> float | None:
    value = headers.get("retry-after") if headers else None
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(parsed.timestamp() - time.time(), 0) if parsed else None


def is_retryable(error: Exception) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, AnthropicAPIError):
        return error.status_code in RETRYABLE_STATUS_CODES
    if isinstance(error, AnthropicStreamError):
        return error.retryable
    return False


def retry_delay_seconds(error: Exception, attempt: int) -> float:
    """第 attempt 次失败后等多久：有 Retry-After 就用它（封顶 60 秒），否则指数退避加抖动。"""
    retry_after = _retry_after_seconds(getattr(error, "headers", None))
    if retry_after is not None:
        return min(retry_after, RETRY_AFTER_CAP_SECONDS)
    ceiling = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))
    return ceiling * (0.5 + random.random() / 2)


def _error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()[:1000] or "（空响应）"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return body.strip()[:1000]


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        base_url: str | None,
        api_key: str | None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout_seconds: float = 600,
        max_output_tokens: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.temperature = temperature
        # 保留字段让 attach_auxiliary_providers 的接口一致；Anthropic 这边不用它
        self.reasoning_effort = reasoning_effort
        self.extra_body = extra_body or {}
        self.max_output_tokens = max_output_tokens
        self.client = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    def with_reasoning_effort(self, reasoning_effort: str | None) -> "AnthropicProvider":
        derived = copy.copy(self)
        derived.reasoning_effort = reasoning_effort
        return derived

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def build_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        system, converted = convert_messages(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens or DEFAULT_MAX_TOKENS,
            "messages": converted,
            "stream": True,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = convert_tools(tools)
        if self.temperature is not None:
            request["temperature"] = self.temperature
        request.update(self.extra_body)
        return request

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._complete_once(messages, tools, listener)
            except ContextOverflowError:
                raise
            except Exception as error:
                if attempt == MAX_ATTEMPTS or not is_retryable(error):
                    raise
                wait_seconds = retry_delay_seconds(error, attempt)
                await listener.on_retry(attempt + 1, error, wait_seconds)
                await asyncio.sleep(wait_seconds)
        raise AssertionError("不会走到这里")

    async def _complete_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse:
        url = messages_url(self.base_url)
        request = self.build_request(messages, tools)
        async with self.client.stream("POST", url, headers=self._headers(), json=request) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                message = _error_message(body)
                if is_context_overflow(message):
                    raise ContextOverflowError(message)
                raise AnthropicAPIError(response.status_code, message, response.headers)
            accumulator = AnthropicStreamAccumulator()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                await consume_event(event, accumulator, listener)
        result = accumulator.build_response()
        if result.finish_reason is None:
            # 没收到 message_delta 的 stop_reason：流被提前关了，当断掉处理
            result.finish_reason = "incomplete"
        return result
