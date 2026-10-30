"""真实模型接口的联调：流式文本、工具调用、usage。

用 `make test-live` 跑。默认自动挑配置里的免费模型；没有任何可用模型时整组跳过。
"""

import unittest

from pocketagent.llm.base import SilentListener
from tests.live.support import (
    LIVE_MODEL,
    RecordingListener,
    build_live_provider,
    skip_without_model,
)

ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "把 text 原样返回",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要原样返回的文字"}},
            "required": ["text"],
        },
    },
}


@skip_without_model
class LiveProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = build_live_provider(LIVE_MODEL)

    async def test_text_completion_streams_and_reports_usage(self) -> None:
        listener = RecordingListener()

        response = await self.provider.complete(
            [
                {"role": "system", "content": "只按用户要求回答，不要解释。"},
                {"role": "user", "content": "只回复两个字：收到"},
            ],
            [],
            listener,
        )

        self.assertIn("收到", response.text)
        self.assertTrue(listener.text_parts, "provider 没有把流式增量交给 listener")
        self.assertIsNotNone(response.finish_reason)
        self.assertIsNotNone(response.usage, "接口没有返回 usage，用量统计会退化成估算")
        self.assertGreater(response.usage.prompt_tokens, 0)
        self.assertGreater(response.usage.completion_tokens, 0)

    async def test_tool_call_is_streamed_and_parsed(self) -> None:
        response = await self.provider.complete(
            [
                {"role": "system", "content": "你必须调用 echo 工具，不要直接回答。"},
                {"role": "user", "content": "用 echo 工具返回 hello"},
            ],
            [ECHO_TOOL],
            SilentListener(),
        )

        self.assertTrue(response.tool_calls, "模型没有按提示调用工具，或者工具调用没被解析出来")
        call = response.tool_calls[0]
        self.assertEqual(call.name, "echo")
        self.assertEqual(call.arguments, {"text": "hello"})
        self.assertIn("hello", call.raw_arguments)
