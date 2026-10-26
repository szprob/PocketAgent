"""OpenAI 兼容接口（new-api、vLLM、DeepSeek、Qwen 网关……）的流式调用。"""

import json
from dataclasses import dataclass, field
from typing import Any

from pocketagent.llm.base import LLMResponse, StreamListener, ToolCall, Usage


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
    ) -> None:
        # 延迟导入：跑单测、用假 provider 时不需要装 openai
        from openai import AsyncOpenAI

        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.extra_body = extra_body or {}
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "EMPTY", timeout=timeout_seconds)

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
        if self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort
        if self.extra_body:
            request["extra_body"] = self.extra_body
        return request

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse:
        accumulator = StreamAccumulator()
        # async with 保证流读完、出错或被中断时都关掉 HTTP 连接
        async with await self.client.chat.completions.create(**self._build_request(messages, tools)) as stream:
            async for chunk in stream:
                if chunk.usage:
                    accumulator.usage = Usage(
                        prompt_tokens=chunk.usage.prompt_tokens or 0,
                        completion_tokens=chunk.usage.completion_tokens or 0,
                    )
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                await _consume_delta(choice.delta, accumulator, listener)
                if choice.finish_reason:
                    accumulator.finish_reason = choice.finish_reason
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
