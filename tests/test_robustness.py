"""记账、重试、截断、大文件、图片缓存、打包——那些不出错时看不见的地方。"""

import base64
import fnmatch
import io
import tomllib
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

import httpx
import openai

from pocketagent import attachments
from pocketagent.attachments import AttachmentStore, build_user_message, materialize_messages, model_copy_path
from pocketagent.llm import LLMResponse, Usage
from pocketagent.llm.base import SilentListener
from pocketagent.llm.openai_compat import (
    MAX_ATTEMPTS,
    OpenAICompatibleProvider,
    is_retryable,
    retry_delay_seconds,
)
from pocketagent.tokens import IMAGE_TOKENS, estimate_message_tokens, estimate_text_tokens
from pocketagent.tools import ToolContext, default_tool_registry
from pocketagent.tools.files import READ_MAX_LINE_CHARS, READ_MAX_TOTAL_CHARS, ReadFileTool
from pocketagent.web.history import history_items
from tests.helpers import TemporaryDirectories, build_harness, tool_call

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")

try:
    from PIL import Image
except ImportError:
    Image = None


def status_error(status_code: int, headers: dict[str, str] | None = None) -> openai.APIStatusError:
    response = httpx.Response(status_code, request=REQUEST, headers=headers or {})
    return openai.APIStatusError(f"HTTP {status_code}", response=response, body=None)


class TokenEstimateTest(unittest.TestCase):
    def test_chinese_counts_far_more_per_char_than_english(self) -> None:
        chinese = estimate_text_tokens("上下文记账对中文是失真的" * 10)
        english = estimate_text_tokens("context accounting" * 10)

        self.assertEqual(chinese, 84)
        self.assertEqual(english, 49)
        self.assertEqual(estimate_text_tokens(""), 0)

    def test_images_count_as_a_fixed_block(self) -> None:
        message = {"role": "user", "content": [{"type": "text", "text": "看"}, {"type": "image_url", "image_url": {"url": "data:..."}}]}

        self.assertEqual(estimate_message_tokens(message), 4 + 1 + IMAGE_TOKENS)


class TokenAnchorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.context = build_harness(self.directories, []).agent.context

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_anchor_plus_estimate_of_newer_messages(self) -> None:
        self.context.messages.append({"role": "assistant", "content": "好"})
        self.context.record_usage(Usage(prompt_tokens=5000, completion_tokens=300, reasoning_tokens=200))
        anchored = self.context.estimated_context_tokens()
        self.context.messages.append({"role": "tool", "tool_call_id": "c1", "content": "结果" * 50})

        # 思考 token 不回传，不算进去
        self.assertEqual(anchored, 5100)
        self.assertEqual(self.context.estimated_context_tokens(), 5100 + 4 + 70)

    def test_anchor_is_dropped_when_history_is_replaced(self) -> None:
        self.context.record_usage(Usage(prompt_tokens=90000, completion_tokens=10))
        self.context.history_version += 1

        self.assertLess(self.context.estimated_context_tokens(), 90000)


class FlakyProvider(OpenAICompatibleProvider):
    """按剧本抛错，最后成功；不发真请求。"""

    def __init__(self, failures: list[Exception]) -> None:
        super().__init__(model="m", base_url="https://example.com/v1", api_key="k")
        self.failures = list(failures)
        self.attempts = 0

    async def _complete_once(self, messages, tools, listener) -> LLMResponse:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return LLMResponse(text="ok")


class RetryListener(SilentListener):
    def __init__(self) -> None:
        self.retries: list[tuple[int, float]] = []

    async def on_retry(self, attempt: int, error: Exception, wait_seconds: float) -> None:
        self.retries.append((attempt, wait_seconds))


class RetryPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_only_transient_errors_are_retryable(self) -> None:
        self.assertTrue(is_retryable(status_error(429)))
        self.assertTrue(is_retryable(status_error(503)))
        self.assertTrue(is_retryable(openai.APIConnectionError(request=REQUEST)))
        self.assertTrue(is_retryable(httpx.ReadTimeout("slow")))
        self.assertFalse(is_retryable(status_error(400)))
        self.assertFalse(is_retryable(status_error(401)))
        self.assertFalse(is_retryable(ValueError("bug")))

    def test_retry_after_wins_and_backoff_grows_with_jitter(self) -> None:
        self.assertEqual(retry_delay_seconds(status_error(429, {"retry-after": "7"}), 1), 7)
        self.assertEqual(retry_delay_seconds(status_error(429, {"retry-after-ms": "1500"}), 1), 1.5)
        self.assertEqual(retry_delay_seconds(status_error(429, {"retry-after": "3600"}), 1), 60)
        first = [retry_delay_seconds(status_error(503), 1) for _ in range(50)]
        third = [retry_delay_seconds(status_error(503), 3) for _ in range(50)]
        self.assertTrue(all(0.5 <= delay <= 1 for delay in first))
        self.assertTrue(all(2 <= delay <= 4 for delay in third))
        self.assertGreater(len(set(first)), 1)

    async def test_provider_retries_transient_errors_then_succeeds(self) -> None:
        provider = FlakyProvider([status_error(503), openai.APIConnectionError(request=REQUEST)])
        listener = RetryListener()

        with mock.patch("pocketagent.llm.openai_compat.asyncio.sleep", new=mock.AsyncMock()):
            response = await provider.complete([], [], listener)

        self.assertEqual(response.text, "ok")
        self.assertEqual(provider.attempts, 3)
        self.assertEqual([attempt for attempt, _ in listener.retries], [2, 3])

    async def test_provider_does_not_retry_client_errors(self) -> None:
        provider = FlakyProvider([status_error(401)])

        with self.assertRaises(openai.APIStatusError):
            await provider.complete([], [], RetryListener())
        self.assertEqual(provider.attempts, 1)

    async def test_provider_gives_up_after_max_attempts(self) -> None:
        provider = FlakyProvider([status_error(503)] * 10)

        with mock.patch("pocketagent.llm.openai_compat.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(openai.APIStatusError):
                await provider.complete([], [], RetryListener())
        self.assertEqual(provider.attempts, MAX_ATTEMPTS)

    def test_max_output_tokens_is_sent(self) -> None:
        provider = OpenAICompatibleProvider(model="m", base_url=None, api_key="k", max_output_tokens=8192)

        self.assertEqual(provider._build_request([], [])["max_tokens"], 8192)


class TruncationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_truncated_tool_call_is_not_executed_and_model_is_told(self) -> None:
        truncated = LLMResponse(
            tool_calls=[tool_call("c1", "write_file", '{"path": "big.txt", "content": "x"}')], finish_reason="length"
        )
        harness = build_harness(self.directories, [truncated, LLMResponse(text="拆小了重来")])

        await harness.agent.run_turn("写个大文件")

        self.assertFalse((harness.workspace / "big.txt").exists())
        notice = [m for m in harness.agent.context.messages if m.get("_kind") == "notice"]
        self.assertEqual(len(notice), 1)
        self.assertIn("工具调用的参数不完整", notice[0]["content"])
        self.assertEqual(harness.agent.context.turn.final_text, "拆小了重来")

    async def test_gives_up_after_repeated_truncation(self) -> None:
        responses = [LLMResponse(text=f"第{index}段", finish_reason="length") for index in range(5)]
        harness = build_harness(self.directories, responses)

        await harness.agent.run_turn("写长文")

        self.assertEqual(harness.agent.context.turn.stop_reason, "truncated")
        self.assertEqual(len(harness.provider.remaining_responses), 2)


class CutOffReplyTest(unittest.IsolatedAsyncioTestCase):
    """上游把生成掐断（内容审核 / 流没结束就断了）：半截回复留着，但要标出这一轮是断的。"""

    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_content_filter_keeps_the_partial_reply_and_marks_it(self) -> None:
        cut_off = LLMResponse(text="这是一段说到一半的话（", finish_reason="content_filter")
        harness = build_harness(self.directories, [cut_off])

        await harness.agent.run_turn("讲讲这件事")

        turn = harness.agent.context.turn
        self.assertEqual(turn.stop_reason, "filtered")
        self.assertEqual(turn.final_text, "这是一段说到一半的话（")
        last_message = harness.agent.context.messages[-1]
        self.assertEqual(last_message["role"], "assistant")
        self.assertEqual(last_message["_finish"], "filtered")
        # 不重试也不让模型接着写：剧本只有一条，多调一次就会报「剧本用完」
        self.assertEqual(len(harness.provider.remaining_responses), 0)
        self.assertFalse([m for m in harness.agent.context.messages if m.get("_kind") == "notice"])

    async def test_incomplete_stream_is_marked_the_same_way(self) -> None:
        cut_off = LLMResponse(text="只写了一半", finish_reason="incomplete")
        harness = build_harness(self.directories, [cut_off])

        await harness.agent.run_turn("随便说说")

        self.assertEqual(harness.agent.context.turn.stop_reason, "incomplete")
        self.assertEqual(harness.agent.context.messages[-1]["_finish"], "incomplete")

    async def test_cut_off_marker_survives_history_replay(self) -> None:
        cut_off = LLMResponse(text="说到一半", finish_reason="content_filter")
        harness = build_harness(self.directories, [cut_off])
        await harness.agent.run_turn("讲讲这件事")

        items = history_items(harness.agent.context.messages, default_tool_registry())

        replies = [item for item in items if item["kind"] == "assistant"]
        self.assertEqual(replies[-1]["text"], "说到一半")
        self.assertEqual(replies[-1]["finish"], "filtered")

    async def test_marker_key_is_not_sent_to_the_model(self) -> None:
        cut_off = LLMResponse(text="说到一半", finish_reason="content_filter")
        harness = build_harness(self.directories, [cut_off])
        await harness.agent.run_turn("讲讲这件事")

        # _finish 只是给界面看的，发请求前要去掉，否则有的网关会因为多出来的字段报 400
        materialized = materialize_messages(harness.agent.context.messages, None, supports_images=False)
        self.assertFalse([key for message in materialized for key in message if key.startswith("_")])


class ReadFileLimitsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.context = ToolContext(workspace=self.workspace)

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_binary_files_are_refused(self) -> None:
        (self.workspace / "blob.bin").write_bytes(b"abc\x00def" * 10)

        result = await ReadFileTool().run({"path": "blob.bin"}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("二进制", result.content)

    async def test_long_lines_and_huge_files_are_cut_with_a_way_to_continue(self) -> None:
        (self.workspace / "wide.txt").write_text("x" * (READ_MAX_LINE_CHARS + 500) + "\nshort\n", encoding="utf-8")
        line = "y" * 1000
        line_count = READ_MAX_TOTAL_CHARS // 1000 + 50
        (self.workspace / "huge.txt").write_text((line + "\n") * line_count, encoding="utf-8")

        wide = await ReadFileTool().run({"path": "wide.txt"}, self.context)
        huge = await ReadFileTool().run({"path": "huge.txt"}, self.context)

        self.assertIn(f"这一行有 {READ_MAX_LINE_CHARS + 500} 字", wide.content)
        self.assertIn("short", wide.content)
        self.assertLess(len(huge.content), READ_MAX_TOTAL_CHARS + 20_000)
        self.assertIn(f"共 {line_count} 行", huge.content)
        self.assertIn("接着读", huge.content)


@unittest.skipIf(Image is None, "没装 Pillow")
class ImageForModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.root = self.directories.home_dir / "attachments"
        self.store = AttachmentStore(self.root, "s1")
        attachments._DATA_URL_CACHE.clear()

    def tearDown(self) -> None:
        self.directories.cleanup()

    def png_bytes(self, width: int, height: int) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
        return buffer.getvalue()

    def sent_image_size(self, message: dict) -> tuple[int, int]:
        part = materialize_messages([message], self.root, supports_images=True)[0]["content"][1]
        encoded = part["image_url"]["url"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            return image.size

    def test_large_images_are_downscaled_once_for_the_model(self) -> None:
        large = self.store.save("big.png", self.png_bytes(4000, 1000))
        small = self.store.save("small.png", self.png_bytes(300, 200))

        self.assertTrue(model_copy_path(Path(large.path)).is_file())
        self.assertFalse(model_copy_path(Path(small.path)).exists())
        self.assertEqual(self.sent_image_size(build_user_message("看", [large], self.store)), (2048, 512))
        self.assertEqual(self.sent_image_size(build_user_message("看", [small], self.store)), (300, 200))

    def test_each_think_reuses_the_encoded_image(self) -> None:
        image = self.store.save("a.png", self.png_bytes(500, 500))
        message = build_user_message("看", [image], self.store)
        first = materialize_messages([message], self.root, supports_images=True)

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("不该再读盘")):
            second = materialize_messages([message], self.root, supports_images=True)

        self.assertEqual(first, second)


def matches_package_pattern(relative: PurePosixPath, pattern: str) -> bool:
    """setuptools package-data 的写法：每一级单独匹配，* 不跨目录。"""
    pattern_parts = PurePosixPath(pattern).parts
    return len(relative.parts) == len(pattern_parts) and all(
        fnmatch.fnmatchcase(part, pattern_part) for part, pattern_part in zip(relative.parts, pattern_parts)
    )


class PackagingTest(unittest.TestCase):
    def test_package_data_covers_every_static_file(self) -> None:
        """static/* 不递归；以后加了子目录忘了改 pyproject，wheel 里就会少文件（可编辑安装看不出来）。"""
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as file:
            patterns = tomllib.load(file)["tool"]["setuptools"]["package-data"]["pocketagent.web"]
        web_dir = REPOSITORY_ROOT / "pocketagent" / "web"
        static_files = [path for path in (web_dir / "static").rglob("*") if path.is_file() and path.name != ".DS_Store"]

        uncovered = [
            str(relative)
            for relative in (PurePosixPath(path.relative_to(web_dir).as_posix()) for path in static_files)
            if not any(matches_package_pattern(relative, pattern) for pattern in patterns)
        ]

        self.assertGreater(len(static_files), 5)
        self.assertEqual(uncovered, [])


if __name__ == "__main__":
    unittest.main()
