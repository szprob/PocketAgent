"""思考档位怎么传、缓存命中解析、联网工具、压缩前的历史瘦身、会话摘要缓存。"""

import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pocketagent.compaction import (
    PRESERVED_SKILLS_HEADER,
    RESUME_HINT,
    SUMMARY_PREFIX,
    estimate_tokens,
    previous_summary_text,
    shrink_for_summary,
)
from pocketagent.config import AppConfig, ConfigError, ModelProfile
from pocketagent.llm.base import ContextOverflowError, is_context_overflow
from pocketagent.llm.openai_compat import OpenAICompatibleProvider, parse_usage
from pocketagent.nodes import _snapshot_length
from pocketagent.session import SessionStore
from pocketagent.tools import ToolContext
from pocketagent.tools.web import WebFetchTool, _format_result, html_to_text
from tests.helpers import TemporaryDirectories


class ReasoningStyleTest(unittest.TestCase):
    def build(self, style: str, effort: str | None, extra_body: dict | None = None) -> dict:
        provider = OpenAICompatibleProvider(
            model="m", base_url="https://example.com/v1", api_key="k", reasoning_effort=effort,
            extra_body=extra_body, reasoning_style=style,
        )
        return provider._build_request([{"role": "user", "content": "hi"}], [])

    def test_top_level_style(self) -> None:
        request = self.build("top", "low")

        self.assertEqual(request["reasoning_effort"], "low")
        self.assertNotIn("extra_body", request)

    def test_openrouter_style_merges_into_extra_body(self) -> None:
        request = self.build("openrouter", "high", extra_body={"reasoning": {"exclude": True}, "top_k": 20})

        self.assertNotIn("reasoning_effort", request)
        self.assertEqual(request["extra_body"], {"reasoning": {"exclude": True, "effort": "high"}, "top_k": 20})

    def test_default_effort_sends_nothing(self) -> None:
        request = self.build("openrouter", None)

        self.assertNotIn("reasoning_effort", request)
        self.assertNotIn("extra_body", request)


class ReasoningChoicesTest(unittest.TestCase):
    def test_choices_follow_provider_and_can_be_overridden(self) -> None:
        glm = ModelProfile.from_dict({"model": "glm-5.3-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4"})
        grok = ModelProfile.from_dict(
            {"model": "x-ai/grok-4.7", "base_url": "https://openrouter.ai/api/v1", "reasoning_choices": ["low", "high"]}
        )

        self.assertEqual(glm.available_reasoning_choices(), ["low", "high", "max"])
        self.assertEqual(grok.available_reasoning_choices(), ["low", "high"])
        self.assertEqual(grok.public_dict()["custom_reasoning_choices"], ["low", "high"])

    def test_unsupported_effort_is_rejected(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            ModelProfile.from_dict(
                {"model": "glm-5.3-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "reasoning_effort": "none"}
            )
        self.assertIn("low / high / max", str(caught.exception))

    def test_compaction_and_web_settings_round_trip(self) -> None:
        config = AppConfig.from_dict(
            {
                "models": [{"model": "glm-5.3-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "glm-key"}],
                "auto_compact": False,
                "compact_threshold_percent": 60,
                "web_search_provider": "zhipu",
                "web_search_engine": "search_pro",
            }
        )
        reloaded = AppConfig.from_dict(config.to_dict())

        self.assertFalse(reloaded.auto_compact)
        self.assertEqual(reloaded.compact_threshold_percent, 60)
        self.assertEqual(reloaded.web_search_settings().api_key, "glm-key")
        self.assertEqual(reloaded.web_search_settings().engine, "search_pro")
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"compact_threshold_percent": 99})


class UsageParsingTest(unittest.TestCase):
    def test_standard_and_deepseek_cache_fields(self) -> None:
        standard = SimpleNamespace(
            prompt_tokens=100, completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=80),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=6),
            model_extra={"cost": 0.0012},
        )
        deepseek = SimpleNamespace(
            prompt_tokens=100, completion_tokens=10,
            prompt_tokens_details=None, completion_tokens_details=None,
            model_extra={"prompt_cache_hit_tokens": 64},
        )

        parsed_standard = parse_usage(standard)
        parsed_deepseek = parse_usage(deepseek)

        self.assertEqual((parsed_standard.cached_tokens, parsed_standard.reasoning_tokens), (80, 6))
        self.assertEqual(parsed_standard.cost, 0.0012)
        self.assertEqual(parsed_deepseek.cached_tokens, 64)
        self.assertIsNone(parsed_deepseek.cost)

    def test_context_overflow_detection(self) -> None:
        self.assertTrue(is_context_overflow(Exception("This model's maximum context length is 131072 tokens")))
        self.assertTrue(is_context_overflow(Exception("输入超过最大长度")))
        self.assertFalse(is_context_overflow(Exception("rate limited")))
        self.assertIsInstance(ContextOverflowError("x"), RuntimeError)


class WebToolsTest(unittest.IsolatedAsyncioTestCase):
    def test_html_to_text_prefers_main_content(self) -> None:
        body = "<p>" + "正文内容很长。" * 60 + "</p>"
        html = (
            "<html><head><title>标题</title><style>.x{}</style></head><body>"
            "<nav><a>首页</a><a>关于</a></nav><main><h1>文档</h1>" + body + "<ul><li>要点一</li></ul></main>"
            "<footer>版权所有</footer><script>alert(1)</script></body></html>"
        )

        title, text = html_to_text(html)

        self.assertEqual(title, "标题")
        self.assertIn("## 文档", text)
        self.assertIn("- 要点一", text)
        for junk in ("首页", "版权所有", "alert", ".x{}"):
            self.assertNotIn(junk, text)

    async def test_fetch_rejects_non_http_urls(self) -> None:
        result = await WebFetchTool().run({"url": "file:///etc/passwd"}, ToolContext(workspace=Path(".")))

        self.assertTrue(result.is_error)

    def test_search_result_format(self) -> None:
        text = _format_result(1, {"title": " 标题 ", "link": "https://a.b", "content": "第一行\n第二行", "publish_date": "2026-10-01"})

        self.assertEqual(text, "1. 标题（2026-10-01）\n   https://a.b\n   第一行 第二行")


class ShrinkForSummaryTest(unittest.TestCase):
    def test_truncates_long_messages_then_drops_oldest_exchanges_keeping_pairs(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "最初的需求"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "很长" * 20000},
            {"role": "assistant", "content": "中间" * 3000},
            {"role": "user", "content": "最近的问题"},
        ]

        shrunk = shrink_for_summary(messages, limit_tokens=1500)

        self.assertEqual(shrunk[0]["role"], "system")
        self.assertEqual(shrunk[1]["content"], "最初的需求")
        self.assertEqual(shrunk[-1]["content"], "最近的问题")
        self.assertLessEqual(estimate_tokens(shrunk), 1500)
        ids_called = {call["id"] for message in shrunk for call in message.get("tool_calls") or []}
        ids_answered = {message["tool_call_id"] for message in shrunk if message["role"] == "tool"}
        self.assertEqual(ids_called, ids_answered)


class IncrementalCompactionTest(unittest.TestCase):
    """增量合并：历史开头已是旧摘要时，摘要在旧摘要基础上合并，而不是从头重压。"""

    def test_previous_summary_text_strips_markers_and_skills(self) -> None:
        message = {
            "role": "user",
            "content": SUMMARY_PREFIX + "旧摘要正文" + PRESERVED_SKILLS_HEADER + "技能正文" + RESUME_HINT,
        }

        self.assertEqual(previous_summary_text([{"role": "system", "content": "s"}, message]), "旧摘要正文")

    def test_previous_summary_text_returns_none_for_fresh_history(self) -> None:
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "普通对话"}]

        self.assertIsNone(previous_summary_text(messages))

    def test_shrink_never_truncates_or_drops_the_previous_summary(self) -> None:
        summary_message = {"role": "user", "content": SUMMARY_PREFIX + "旧摘要" * 5000}
        messages = [
            {"role": "system", "content": "s"},
            summary_message,
            {"role": "assistant", "content": "新增" * 9000},
        ]

        shrunk = shrink_for_summary(messages, limit_tokens=100)

        self.assertEqual(shrunk[1]["content"], summary_message["content"])

    def test_snapshot_boundary_never_starts_on_a_tool_result(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "任务"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "结果"},
        ]

        self.assertEqual(_snapshot_length(messages), 2)

    def test_snapshot_keeps_at_most_the_tail(self) -> None:
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "任务"}]
        for index in range(20):
            messages.append({"role": "assistant", "content": f"回复{index}"})
            messages.append({"role": "user", "content": f"追问{index}"})

        start = _snapshot_length(messages)

        self.assertGreaterEqual(start, 3)
        self.assertLessEqual(len(messages) - start, 8)
        self.assertNotEqual(messages[start].get("role"), "tool")


class SessionSummaryCacheTest(unittest.TestCase):
    def test_only_changed_files_are_reparsed(self) -> None:
        directories = TemporaryDirectories()
        try:
            store = SessionStore(directories.home_dir / "sessions")
            first = store.new_session(directories.workspace)
            first.messages.append({"role": "user", "content": "第一个任务"})
            store.save(first)
            self.assertEqual([summary.title for summary in store.summaries()], ["第一个任务"])

            # 同一个文件没变就不重新解析：故意写坏内容但保持修改时间，缓存应该照用
            path = store.path_for(first.id)
            modified = path.stat().st_mtime
            path.write_text("{坏了", encoding="utf-8")
            os.utime(path, (modified, modified))
            self.assertEqual([summary.title for summary in store.summaries()], ["第一个任务"])

            time.sleep(0.01)
            first.title = "改了名"
            store.save(first)
            self.assertEqual([summary.title for summary in store.summaries()], ["改了名"])
            store.delete(first.id)
            self.assertEqual(store.summaries(), [])
        finally:
            directories.cleanup()


if __name__ == "__main__":
    unittest.main()
