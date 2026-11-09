import json
import unittest
from unittest import mock

import httpx

from pocketagent.config import ModelProfile
from pocketagent.llm.anthropic import (
    DEFAULT_MAX_TOKENS,
    AnthropicAPIError,
    AnthropicProvider,
    AnthropicStreamAccumulator,
    claude_reasoning_choices,
    claude_thinking_mode,
    consume_event,
    convert_messages,
    convert_tools,
    messages_url,
)
from pocketagent.llm.base import ContextOverflowError, build_assistant_message


def sse_bytes(events: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}\n" for event in events]
    return "".join(lines).encode("utf-8")


class RecordingListener:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.retries: list[tuple[int, str, float]] = []

    async def on_text(self, text: str) -> None:
        self.text.append(text)

    async def on_reasoning(self, text: str) -> None:
        self.reasoning.append(text)

    async def on_retry(self, attempt: int, error: Exception, wait_seconds: float) -> None:
        self.retries.append((attempt, str(error), wait_seconds))


class OutputLimitTest(unittest.TestCase):
    """默认输出上限跟着现在的 Claude 走：还按 8k 发，写个大文件就会被截断、白跑一轮补救。"""

    def test_default_request_uses_the_modern_limit(self) -> None:
        provider = AnthropicProvider(model="claude-sonnet-4-5", base_url=None, api_key="k")

        self.assertEqual(provider.build_request([], [])["max_tokens"], DEFAULT_MAX_TOKENS)
        self.assertGreater(DEFAULT_MAX_TOKENS, 8192)

    def test_configured_limit_wins(self) -> None:
        provider = AnthropicProvider(model="claude-sonnet-4-5", base_url=None, api_key="k", max_output_tokens=1024)

        self.assertEqual(provider.build_request([], [])["max_tokens"], 1024)


class MessageConversionTest(unittest.TestCase):
    def test_system_tool_calls_and_tool_results_are_converted(self) -> None:
        system, messages = convert_messages(
            [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "看看这个文件"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_a", "content": "print(1)"},
                {"role": "tool", "tool_call_id": "call_b", "content": "第二段"},
                {"role": "assistant", "content": "看完了"},
            ]
        )

        self.assertEqual(system, "你是助手")
        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user", "assistant"])
        tool_use = messages[1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["input"], {"path": "a.py"})
        # 连续两条 tool 结果合并成一条 user 消息里的两个块
        self.assertEqual([block["type"] for block in messages[2]["content"]], ["tool_result", "tool_result"])
        self.assertEqual(messages[2]["content"][1]["tool_use_id"], "call_b")

    def test_images_are_converted_to_source_blocks(self) -> None:
        _, messages = convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                }
            ]
        )

        image = messages[0]["content"][1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["source"]["type"], "base64")
        self.assertEqual(image["source"]["media_type"], "image/png")
        self.assertEqual(image["source"]["data"], "AAAA")

    def test_tools_become_input_schemas(self) -> None:
        converted = convert_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "description": "执行命令",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                    },
                }
            ]
        )

        self.assertEqual(converted, [
            {
                "name": "bash",
                "description": "执行命令",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ])


class ProviderFactoryTest(unittest.TestCase):
    def test_protocol_selects_the_provider(self) -> None:
        from pocketagent.config import Settings
        from pocketagent.core.agent import create_provider

        anthropic = create_provider(
            Settings(model="claude-test", protocol="anthropic", base_url="https://api.anthropic.com")
        )
        openai = create_provider(Settings(model="gpt-test", protocol="openai", base_url="https://example.com/v1"))

        self.assertIsInstance(anthropic, AnthropicProvider)
        self.assertEqual(type(openai).__name__, "OpenAICompatibleProvider")


class MessagesUrlTest(unittest.TestCase):
    def test_defaults_and_existing_suffixes(self) -> None:
        self.assertEqual(messages_url(None), "https://api.anthropic.com/v1/messages")
        self.assertEqual(messages_url("https://api.anthropic.com"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(messages_url("https://api.anthropic.com/v1/"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(
            messages_url("https://api.anthropic.com/v1/messages"), "https://api.anthropic.com/v1/messages"
        )


class AnthropicProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_streams_text_tool_call_and_usage(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好的"}},
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.py"}'},
                },
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
                {"type": "message_stop"},
            ]
            return httpx.Response(200, content=sse_bytes(events))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider(
            model="claude-test",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
            max_output_tokens=1024,
            http_client=client,
        )
        try:
            response = await provider.complete(
                [{"role": "system", "content": "规则"}, {"role": "user", "content": "读 a.py"}],
                [
                    {
                        "type": "function",
                        "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
                    }
                ],
                RecordingListener(),
            )
        finally:
            await client.aclose()

        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-ant-test")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(seen["body"]["system"], "规则")
        self.assertEqual(seen["body"]["max_tokens"], 1024)
        self.assertTrue(seen["body"]["stream"])
        self.assertEqual(seen["body"]["tools"][0]["name"], "read_file")
        self.assertEqual(response.text, "好的")
        self.assertEqual(response.finish_reason, "tool_calls")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(response.usage.prompt_tokens, 15)
        self.assertEqual(response.usage.cached_tokens, 5)
        self.assertEqual(response.usage.completion_tokens, 7)

    async def test_max_tokens_stop_reason_maps_to_length(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            events = [
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "半截"}},
                {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 2}},
            ]
            return httpx.Response(200, content=sse_bytes(events))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider("claude-test", None, "sk", http_client=client)
        try:
            response = await provider.complete([{"role": "user", "content": "写"}], [], RecordingListener())
        finally:
            await client.aclose()

        self.assertEqual(response.text, "半截")
        self.assertEqual(response.finish_reason, "length")

    async def test_context_overflow_and_client_errors_are_not_retried(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            too_long = "prompt is too long: 300000 tokens > 200000 maximum"
            body = {"type": "error", "error": {"type": "invalid_request_error", "message": too_long}}
            return httpx.Response(400, json=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider("claude-test", None, "sk", http_client=client)
        try:
            with self.assertRaises(ContextOverflowError):
                await provider.complete([{"role": "user", "content": "很长的历史"}], [], RecordingListener())
        finally:
            await client.aclose()

        self.assertEqual(attempts["count"], 1)

    async def test_overloaded_is_retried_then_succeeds(self) -> None:
        responses = [
            httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "忙"}}),
            httpx.Response(
                200,
                content=sse_bytes(
                    [
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "收到"}},
                        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                    ]
                ),
            ),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        listener = RecordingListener()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider("claude-test", None, "sk", http_client=client)
        try:
            with mock.patch("pocketagent.llm.anthropic.retry_delay_seconds", return_value=0):
                response = await provider.complete([{"role": "user", "content": "你好"}], [], listener)
        finally:
            await client.aclose()

        self.assertEqual(response.text, "收到")
        self.assertEqual(len(listener.retries), 1)
        self.assertIn("529", listener.retries[0][1])
        self.assertEqual(responses, [])

    async def test_non_retryable_error_has_status_code(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"type": "error", "error": {"type": "authentication_error", "message": "密钥不对"}}
            return httpx.Response(401, json=body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider("claude-test", None, "bad-key", http_client=client)
        try:
            with self.assertRaises(AnthropicAPIError) as raised:
                await provider.complete([{"role": "user", "content": "你好"}], [], RecordingListener())
        finally:
            await client.aclose()

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("密钥不对", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class ThinkingRequestTest(unittest.TestCase):
    """怎么开思考看型号：4.6 起 adaptive + effort，4.5 及以前按预算（选了档位才开），认不出的不发。"""

    TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}]

    def test_adaptive_models_think_with_effort_and_drop_temperature(self) -> None:
        provider = AnthropicProvider(model="claude-opus-4-6", base_url=None, api_key="k", temperature=0.3, reasoning_effort="high")

        request = provider.build_request([], self.TOOLS)

        self.assertEqual(request["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(request["output_config"], {"effort": "high"})
        self.assertNotIn("temperature", request)
        self.assertNotIn("anthropic-beta", provider._headers(request))

    def test_adaptive_without_effort_uses_the_model_default(self) -> None:
        request = AnthropicProvider(model="claude-opus-5-5", base_url=None, api_key="k").build_request([], [])

        self.assertEqual(request["thinking"]["type"], "adaptive")
        self.assertNotIn("output_config", request)

    def test_budget_models_think_only_when_a_level_is_picked(self) -> None:
        thinking = AnthropicProvider(model="claude-sonnet-4-5-20250929", base_url=None, api_key="k", reasoning_effort="high")
        plain = AnthropicProvider(model="claude-sonnet-4-5-20250929", base_url=None, api_key="k", temperature=0.2)

        request = thinking.build_request([], self.TOOLS)

        self.assertEqual(request["thinking"], {"type": "enabled", "budget_tokens": 16000})
        self.assertGreater(request["max_tokens"], 16000)
        self.assertEqual(thinking._headers(request)["anthropic-beta"], "interleaved-thinking-2025-05-14")
        plain_request = plain.build_request([], self.TOOLS)
        self.assertNotIn("thinking", plain_request)
        self.assertEqual(plain_request["temperature"], 0.2)

    def test_unknown_models_and_user_written_thinking_are_left_alone(self) -> None:
        gateway = AnthropicProvider(model="deepseek-chat", base_url="https://example.com", api_key="k", reasoning_effort="high")
        custom = AnthropicProvider(
            model="claude-opus-4-6", base_url=None, api_key="k", reasoning_effort="high", extra_body={"thinking": {"type": "disabled"}}
        )

        self.assertNotIn("thinking", gateway.build_request([], []))
        self.assertEqual(custom.build_request([], [])["thinking"], {"type": "disabled"})
        self.assertNotIn("output_config", custom.build_request([], []))

    def test_reasoning_choices_follow_the_model(self) -> None:
        self.assertEqual(claude_thinking_mode("claude-opus-4-20250514"), "budget")
        self.assertEqual(claude_thinking_mode("anthropic/claude-sonnet-4.6"), "adaptive")
        self.assertEqual(claude_reasoning_choices("claude-opus-4-6"), ["low", "medium", "high", "max"])
        self.assertIn("xhigh", claude_reasoning_choices("claude-sonnet-5-5"))
        self.assertEqual(claude_reasoning_choices("claude-haiku-4-5")[0], "none")
        self.assertEqual(claude_reasoning_choices("glm-5.3"), [])
        profile = ModelProfile.from_dict({"model": "claude-opus-4-6", "base_url": "https://api.anthropic.com"})
        self.assertEqual(profile.available_reasoning_choices(), ["low", "medium", "high", "max"])


class ThinkingBlocksRoundTripTest(unittest.IsolatedAsyncioTestCase):
    """思考块连同签名存进历史，下一步原样放回原位置；不传的话带工具时这一步的思考会被接口悄悄关掉。"""

    async def _response(self, events: list[dict]):
        accumulator = AnthropicStreamAccumulator()
        listener = RecordingListener()
        for event in events:
            await consume_event(event, accumulator, listener)
        return accumulator.build_response(), listener

    async def test_signed_blocks_go_back_in_their_original_order(self) -> None:
        response, listener = await self._response(
            [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先读文件"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-0"}},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "redacted_thinking", "data": "R"}},
                {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}},
                {"type": "content_block_start", "index": 3, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 3, "delta": {"type": "thinking_delta", "thinking": "再搜一下"}},
                {"type": "content_block_delta", "index": 3, "delta": {"type": "signature_delta", "signature": "sig-3"}},
                {"type": "content_block_start", "index": 4, "content_block": {"type": "tool_use", "id": "t2", "name": "grep", "input": {}}},
                {"type": "content_block_start", "index": 5, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 5, "delta": {"type": "thinking_delta", "thinking": "没签名"}},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
            ]
        )

        self.assertEqual(listener.reasoning, ["先读文件", "再搜一下", "没签名"])
        message = build_assistant_message(response)
        _, converted = convert_messages([{"role": "user", "content": "hi"}, message])

        blocks = converted[1]["content"]
        self.assertEqual(
            [(block["type"], block.get("signature") or block.get("data") or block.get("id")) for block in blocks],
            [("thinking", "sig-0"), ("redacted_thinking", "R"), ("tool_use", "t1"), ("thinking", "sig-3"), ("tool_use", "t2")],
        )
        self.assertEqual(blocks[0]["thinking"], "先读文件")
        # 存的是原样的块，发出去不带内部的位置信息
        self.assertNotIn("position", blocks[0])

    async def test_omitted_display_still_round_trips_the_signature(self) -> None:
        response, _ = await self._response(
            [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "好"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
        )

        _, converted = convert_messages([{"role": "user", "content": "hi"}, build_assistant_message(response)])

        self.assertEqual(converted[1]["content"][0], {"type": "thinking", "thinking": "", "signature": "sig"})
        self.assertEqual(converted[1]["content"][1], {"type": "text", "text": "好"})
