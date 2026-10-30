"""用量记录和账户余额：本地 JSONL 的增量读取、汇总，以及各家余额接口的响应解析。

余额接口用 httpx.MockTransport 造响应，不连网络。
"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx

from pocketagent.account import billing
from pocketagent.account.usage import UsageLog, UsageMetadata, UsageRecordingProvider
from pocketagent.config import ModelProfile
from pocketagent.llm.base import LLMResponse, SilentListener, Usage


class UsageLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "usage.jsonl"

    def write_entries(self, entries: list[dict]) -> None:
        self.path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8"
        )

    def entry(self, day: date, prompt: int, completion: int, model: str = "model-a", session: str = "s1") -> dict:
        return {
            "time": f"{day.isoformat()}T10:00:00",
            "session_id": session,
            "model": model,
            "model_name": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost": 0.0,
            "estimated": False,
        }

    def test_record_then_incremental_read_does_not_duplicate(self) -> None:
        log = UsageLog(self.path)
        metadata = UsageMetadata(model="m", model_name="模型", model_id="p1", session_id="s1", workspace="/w")

        log.record(metadata, Usage(prompt_tokens=100, completion_tokens=20, cached_tokens=40), estimated=False)
        first_read = log.entries()
        self.assertEqual(len(first_read), 1)
        self.assertEqual(first_read[0]["prompt_tokens"], 100)
        self.assertEqual(first_read[0]["cached_tokens"], 40)
        self.assertEqual(log.entries(), first_read, "没有新内容时不该重复返回旧记录")

        log.record(metadata, Usage(prompt_tokens=10, completion_tokens=2), estimated=True)
        self.assertEqual(len(log.entries()), 2)

    def test_shortened_file_resets_reader(self) -> None:
        log = UsageLog(self.path)
        metadata = UsageMetadata(model="m", session_id="s1")
        log.record(metadata, Usage(prompt_tokens=100), estimated=False)
        log.entries()

        self.path.write_text(json.dumps({"prompt_tokens": 5, "completion_tokens": 1}) + "\n", encoding="utf-8")
        entries = log.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["prompt_tokens"], 5)

    def test_summarize_periods_models_and_daily(self) -> None:
        today = date(2026, 10, 30)
        entries = [
            self.entry(today, 100, 10, "model-a", "s1"),
            self.entry(today - timedelta(days=1), 50, 5, "model-b", "s2"),
            self.entry(today - timedelta(days=10), 30, 3, "model-a", "s1"),
            self.entry(today - timedelta(days=40), 7, 1, "model-b", "s2"),
        ]
        self.write_entries(entries)
        log = UsageLog(self.path)

        summary = log.summarize(today=today)

        periods = {item["label"]: item for item in summary["periods"]}
        self.assertEqual(periods["今天"]["prompt"], 100)
        self.assertEqual(periods["近 7 天"]["prompt"], 150)
        self.assertEqual(periods["近 30 天"]["prompt"], 180)
        self.assertEqual(periods["全部"]["prompt"], 187)

        by_model = {item["model"]: item for item in summary["by_model"]}
        self.assertEqual(by_model["model-a"]["total"], 143)
        self.assertEqual(by_model["model-b"]["total"], 63)
        self.assertEqual(summary["by_model"][0]["model"], "model-a", "模型按总量从多到少排")

        self.assertEqual(len(summary["daily"]), 30)
        self.assertEqual(summary["daily"][-1]["date"], today.isoformat())
        self.assertEqual(summary["daily"][-1]["prompt"], 100)

    def test_session_total_filters_by_session(self) -> None:
        today = date.today()
        self.write_entries([self.entry(today, 10, 1, session="s1"), self.entry(today, 20, 2, session="s2")])

        total = UsageLog(self.path).session_total("s1")

        self.assertEqual(total["prompt"], 10)
        self.assertEqual(total["completion"], 1)
        self.assertEqual(total["calls"], 1)


class _FakeProvider:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response

    async def complete(self, messages, tools, listener) -> LLMResponse:
        return self.response


class UsageRecordingProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_reports_api_usage_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = UsageLog(Path(directory) / "usage.jsonl")
            metadata = UsageMetadata(model="m", session_id="s1")
            provider = UsageRecordingProvider(
                _FakeProvider(LLMResponse(text="hi", usage=Usage(prompt_tokens=7, completion_tokens=3))),
                log,
                metadata,
            )

            await provider.complete([{"role": "user", "content": "hello"}], [], SilentListener())

            entry = log.entries()[0]
            self.assertEqual(entry["prompt_tokens"], 7)
            self.assertEqual(entry["completion_tokens"], 3)
            self.assertFalse(entry["estimated"])

    async def test_missing_usage_falls_back_to_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = UsageLog(Path(directory) / "usage.jsonl")
            metadata = UsageMetadata(model="m", session_id="s1")
            provider = UsageRecordingProvider(_FakeProvider(LLMResponse(text="hi there")), log, metadata)

            await provider.complete([{"role": "user", "content": "hello"}], [], SilentListener())

            entry = log.entries()[0]
            self.assertTrue(entry["estimated"])
            self.assertGreater(entry["prompt_tokens"], 0)
            self.assertGreater(entry["completion_tokens"], 0)


def profile(base_url: str, api_key: str | None = "sk-test", name: str = "模型") -> ModelProfile:
    return ModelProfile(id="p1", name=name, model="m", base_url=base_url, api_key=api_key)


class BillingTest(unittest.IsolatedAsyncioTestCase):
    def test_provider_label(self) -> None:
        self.assertEqual(billing.provider_label(profile("https://api.deepseek.com")), "DeepSeek 官方")
        self.assertEqual(billing.provider_label(profile("https://openrouter.ai/api/v1")), "OpenRouter")
        self.assertEqual(billing.provider_label(profile("https://open.bigmodel.cn/api/paas/v4")), "智谱 BigModel")
        self.assertEqual(billing.provider_label(profile("https://gateway.example.com/v1")), "gateway.example.com")

    async def test_missing_key_and_unsupported_provider(self) -> None:
        no_key = await billing.query_balance(profile("https://api.deepseek.com", api_key=None))
        self.assertFalse(no_key.ok)
        self.assertIn("没配 API Key", no_key.error)

        zhipu = await billing.query_balance(profile("https://open.bigmodel.cn/api/paas/v4"))
        self.assertFalse(zhipu.ok)
        self.assertIn("没有公开的余额查询接口", zhipu.error)

    async def test_deepseek_balance_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://api.deepseek.com/user/balance")
            self.assertEqual(request.headers["Authorization"], "Bearer sk-test")
            return httpx.Response(
                200,
                json={
                    "is_available": True,
                    "balance_infos": [
                        {"total_balance": "10.00", "currency": "CNY", "granted_balance": "2", "topped_up_balance": "8"}
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            lines = await billing._deepseek_balance(client, profile("https://api.deepseek.com"))

        self.assertIn("余额 10.00 CNY", lines[0])
        self.assertIn("赠送 2", lines[0])

    async def test_openrouter_balance_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "usage": 1.2345,
                        "limit": 10,
                        "limit_remaining": 8.7655,
                        "is_free_tier": True,
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            lines = await billing._openrouter_balance(client, profile("https://openrouter.ai/api/v1"))

        self.assertIn("已用 $1.2345", lines[0])
        self.assertIn("剩余 $8.7655", lines[1])
        self.assertIn("免费档", lines[2])

    async def test_openai_compatible_billing_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/subscription"):
                return httpx.Response(200, json={"hard_limit_usd": 20})
            return httpx.Response(200, json={"total_usage": 1234})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            lines = await billing._openai_billing(client, profile("https://gateway.example.com/v1"))

        self.assertEqual(lines, ["额度 $20.00，已用 $12.34，剩余 $7.66"])

    async def test_http_error_is_reported_not_raised(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with mock.patch("pocketagent.account.billing.httpx.AsyncClient", return_value=client):
            report = await billing.query_balance(profile("https://gateway.example.com/v1"))

        self.assertFalse(report.ok)
        self.assertIn("接口返回 500", report.error)
