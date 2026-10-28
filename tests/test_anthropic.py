import json
import unittest
from unittest import mock

import httpx

from pocketagent.llm.anthropic import (
    AnthropicAPIError,
    AnthropicProvider,
    convert_messages,
    convert_tools,
    messages_url,
)
from pocketagent.llm.base import ContextOverflowError


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
