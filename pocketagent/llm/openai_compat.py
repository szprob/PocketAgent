"""OpenAI 兼容接口（new-api、vLLM、DeepSeek、Qwen 网关……）的流式调用。

重试只在这一层做，而且只重试「再试一次可能就好了」的错误：429、5xx、连接断开、超时（包括流到一半断掉）。
400 / 401 / 403 / 404 这类再试也没用，直接报。退避按指数增长加随机抖动，服务端给了 Retry-After 就听它的。
SDK 自带的重试关掉了，免得两层叠加。
"""

import asyncio
import email.utils
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from pocketagent.llm.base import ContextOverflowError, LLMResponse, StreamListener, ToolCall, Usage, is_context_overflow

MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
RETRY_AFTER_CAP_SECONDS = 60.0
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}


def _retry_after_seconds(error: Exception) -> float | None:
    """服务端在 Retry-After（秒数或 HTTP 日期）/ retry-after-ms 里说了等多久。"""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    milliseconds = headers.get("retry-after-ms")
    if milliseconds:
        try:
            return float(milliseconds) / 1000
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(parsed.timestamp() - time.time(), 0) if parsed else None


def is_retryable(error: Exception) -> bool:
    import httpx
    import openai

    if isinstance(error, (openai.APIConnectionError, openai.APITimeoutError, httpx.TransportError)):
        return True
    if isinstance(error, openai.APIStatusError):
        return error.status_code in RETRYABLE_STATUS_CODES
    return False


def retry_delay_seconds(error: Exception, attempt: int) -> float:
    """第 attempt 次失败后等多久：有 Retry-After 就用它（封顶 60 秒），否则指数退避加抖动。"""
    retry_after = _retry_after_seconds(error)
    if retry_after is not None:
        return min(retry_after, RETRY_AFTER_CAP_SECONDS)
    ceiling = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))
    return ceiling * (0.5 + random.random() / 2)


@dataclass
class _PartialToolCall:
    id: str = ""
    name: str = ""
    arguments_text: str = ""


@dataclass
class StreamAccumulator:
    """把流式 chunk 拼回完整回复。

    工具调用是按 index 分片下发的：第一片带 id 和函数名，后面的片只带参数字符串的一段。
    """

    text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    partial_tool_calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: Usage | None = None

    def add_tool_call_delta(self, tool_call_delta: Any) -> None:
        partial = self.partial_tool_calls.setdefault(tool_call_delta.index, _PartialToolCall())
        if tool_call_delta.id:
            partial.id = tool_call_delta.id
        function_delta = tool_call_delta.function
        if function_delta is None:
            return
        if function_delta.name:
            partial.name += function_delta.name
        if function_delta.arguments:
            partial.arguments_text += function_delta.arguments

    def build_response(self) -> LLMResponse:
        tool_calls = []
        for index in sorted(self.partial_tool_calls):
            tool_calls.append(_finish_tool_call(self.partial_tool_calls[index], index))
        return LLMResponse(
            text="".join(self.text_parts),
            reasoning="".join(self.reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=self.finish_reason,
            usage=self.usage,
        )


def _finish_tool_call(partial: _PartialToolCall, index: int) -> ToolCall:
    raw_arguments = partial.arguments_text.strip() or "{}"
    arguments: dict[str, Any] = {}
    arguments_error = None
    try:
        parsed = json.loads(raw_arguments)
        if isinstance(parsed, dict):
            arguments = parsed
        else:
            arguments_error = "参数必须是 JSON 对象"
    except json.JSONDecodeError as error:
        arguments_error = f"参数不是合法 JSON：{error}"
    return ToolCall(
        # 个别网关不下发 id，自己补一个，保证 tool 消息能对上
        id=partial.id or f"call_{index}",
        name=partial.name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        arguments_error=arguments_error,
    )


def _detail(details: Any, name: str) -> int:
    if details is None:
        return 0
    value = details.get(name) if isinstance(details, dict) else getattr(details, name, None)
    return int(value or 0)


def parse_usage(raw_usage: Any) -> Usage:
    """各家 usage 的字段不完全一样：缓存命中标准位置是 prompt_tokens_details.cached_tokens，
    DeepSeek 官方另有 prompt_cache_hit_tokens；OpenRouter 额外给 cost。"""
    extra = getattr(raw_usage, "model_extra", None) or {}
    cached = _detail(getattr(raw_usage, "prompt_tokens_details", None), "cached_tokens")
    cached = cached or int(extra.get("prompt_cache_hit_tokens") or 0)
    cost = extra.get("cost")
    return Usage(
        prompt_tokens=raw_usage.prompt_tokens or 0,
        completion_tokens=raw_usage.completion_tokens or 0,
        cached_tokens=cached,
        reasoning_tokens=_detail(getattr(raw_usage, "completion_tokens_details", None), "reasoning_tokens"),
        cost=float(cost) if cost is not None else None,
    )


def _reasoning_from_delta(delta: Any) -> str | None:
    # DeepSeek / vLLM 用 reasoning_content，部分网关用 reasoning
    return getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)


class OpenAICompatibleProvider:
    def __init__(
        self,
        model: str,
        base_url: str | None,
        api_key: str | None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout_seconds: float = 600,
        reasoning_style: str = "top",
        max_output_tokens: int | None = None,
    ) -> None:
        # 延迟导入：跑单测、用假 provider 时不需要装 openai
        from openai import AsyncOpenAI

        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.reasoning_style = reasoning_style
        self.max_output_tokens = max_output_tokens
        self.extra_body = extra_body or {}
        # 重试自己做（只重试该重试的），SDK 的关掉
        self.client = AsyncOpenAI(
            base_url=base_url, api_key=api_key or "EMPTY", timeout=timeout_seconds, max_retries=0
        )

    def _build_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = tools
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.max_output_tokens:
            request["max_tokens"] = self.max_output_tokens
        extra_body = dict(self.extra_body)
        if self.reasoning_effort and self.reasoning_style == "openrouter":
            # OpenRouter 自己的写法；顶层 reasoning_effort 有的模型不认
            extra_body["reasoning"] = {**extra_body.get("reasoning", {}), "effort": self.reasoning_effort}
        elif self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort
        if extra_body:
            request["extra_body"] = extra_body
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
        accumulator = StreamAccumulator()
        try:
            stream = await self.client.chat.completions.create(**self._build_request(messages, tools))
        except Exception as error:
            if is_context_overflow(error):
                raise ContextOverflowError(str(error)) from error
            raise
        # async with 保证流读完、出错或被中断时都关掉 HTTP 连接
        async with stream:
            async for chunk in stream:
                if chunk.usage:
                    accumulator.usage = parse_usage(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                await _consume_delta(choice.delta, accumulator, listener)
                if choice.finish_reason:
                    accumulator.finish_reason = choice.finish_reason
        if accumulator.finish_reason is None:
            # OpenAI 兼容接口正常结束时最后一片一定带 finish_reason。
            # 没有就是流被提前关了（网关超时、连接断掉），这时手上只有半截内容，
            # 标成 incomplete 让上层当断掉处理，别当成完整回复。
            accumulator.finish_reason = "incomplete"
        return accumulator.build_response()


async def _consume_delta(delta: Any, accumulator: StreamAccumulator, listener: StreamListener) -> None:
    if delta is None:
        return
    reasoning = _reasoning_from_delta(delta)
    if reasoning:
        accumulator.reasoning_parts.append(reasoning)
        await listener.on_reasoning(reasoning)
    if delta.content:
        accumulator.text_parts.append(delta.content)
        await listener.on_text(delta.content)
    for tool_call_delta in delta.tool_calls or []:
        accumulator.add_tool_call_delta(tool_call_delta)
