"""联网工具的分支：网页 HTML / 文本截断 / 二进制、四家搜索的响应和错误处理（网络全部打桩）。"""

import datetime
import json
import unittest
from pathlib import Path
from unittest import mock

from pocketagent.tools.base import ToolContext
from pocketagent.tools.web import WebFetchTool, WebSearchSettings, WebSearchTool


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        headers: dict | None = None,
        url: str = "https://example.com/page",
        payload=None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.content = text.encode("utf-8")
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeClient:
    def __init__(self, response: FakeResponse, calls: list) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.response

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self.response


class WebFetchToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list = []
        self.context = ToolContext(workspace=Path("."))

    def patch_client(self, response: FakeResponse):
        return mock.patch(
            "pocketagent.tools.web.httpx.AsyncClient", side_effect=lambda **kwargs: FakeClient(response, self.calls)
        )

    async def test_html_page_is_converted_to_markdown(self) -> None:
        page = (
            "<html><head><title>文档标题</title></head><body>"
            "<nav>导航</nav><main><h1>正文</h1><p>一段说明</p></main></body></html>"
        )
        response = FakeResponse(text=page, headers={"content-type": "text/html; charset=utf-8"})

        with self.patch_client(response):
            result = await WebFetchTool().run({"url": "https://example.com/page"}, self.context)

        self.assertFalse(result.is_error)
        self.assertIn("# 文档标题", result.content)
        self.assertIn("## 正文", result.content)
        self.assertIn("一段说明", result.content)
        self.assertNotIn("导航", result.content)

    async def test_json_is_returned_raw_and_can_be_truncated(self) -> None:
        response = FakeResponse(text='{"answer": 42}', headers={"content-type": "application/json"})

        with self.patch_client(response):
            result = await WebFetchTool().run({"url": "https://example.com/data", "max_chars": 35}, self.context)

        self.assertIn('{"answer"', result.content)
        self.assertIn("没显示", result.content)

    async def test_http_error_and_binary_content(self) -> None:
        not_found = FakeResponse(status_code=404, text="missing", headers={"content-type": "text/html"})
        with self.patch_client(not_found):
            failed = await WebFetchTool().run({"url": "https://example.com/missing"}, self.context)
        self.assertTrue(failed.is_error)
        self.assertIn("HTTP 404", failed.content)

        binary = FakeResponse(text="\x89PNG", headers={"content-type": "image/png"})
        with self.patch_client(binary):
            refused = await WebFetchTool().run({"url": "https://example.com/image.png"}, self.context)
        self.assertIn("不是文本内容", refused.content)


class WebSearchProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list = []
        self.context = ToolContext(workspace=Path("."))

    def patch_client(self, response: FakeResponse):
        return mock.patch(
            "pocketagent.tools.web.httpx.AsyncClient", side_effect=lambda **kwargs: FakeClient(response, self.calls)
        )

    async def test_exa_sends_the_recommended_request(self) -> None:
        response = FakeResponse(
            payload={
                "results": [
                    {
                        "title": "标题",
                        "url": "https://a.example",
                        "publishedDate": "2026-11-01T08:00:00.000Z",
                        "highlights": ["第一段摘录", "第二段摘录"],
                    }
                ]
            }
        )

        with self.patch_client(response):
            result = await WebSearchTool(WebSearchSettings(provider="exa", api_key="exa-key")).run(
                {"query": "问题", "objective": "找官方文档，优先一手来源"}, self.context
            )

        self.assertFalse(result.is_error)
        self.assertIn("1. 标题（2026-11-01）", result.content)
        self.assertIn("第一段摘录 … 第二段摘录", result.content)
        self.assertIn("https://a.example", result.content)
        method, url, kwargs = self.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.exa.ai/search"))
        self.assertEqual(kwargs["headers"]["x-api-key"], "exa-key")
        body = kwargs["json"]
        self.assertEqual(body["type"], "auto")
        self.assertEqual(body["contents"], {"highlights": True})
        self.assertEqual(body["objective"], "找官方文档，优先一手来源")
        # 模型没点名要几条就不传 numResults，用 Exa 自己的默认
        self.assertNotIn("numResults", body)
        self.assertNotIn("startPublishedDate", body)

    async def test_exa_passes_count_and_recency_through(self) -> None:
        with self.patch_client(FakeResponse(payload={"results": []})):
            await WebSearchTool(WebSearchSettings(provider="exa", api_key="exa-key")).run(
                {"query": "问题", "count": 3, "recency": "oneWeek", "objective": "o"}, self.context
            )

        body = self.calls[0][2]["json"]
        self.assertEqual(body["numResults"], 3)
        expected = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        self.assertEqual(body["startPublishedDate"], expected)

    async def test_exa_errors_are_reported(self) -> None:
        unauthorized = FakeResponse(
            status_code=401, payload={"error": "Invalid API key", "tag": "INVALID_API_KEY"}
        )
        with self.patch_client(unauthorized):
            result = await WebSearchTool(WebSearchSettings(provider="exa", api_key="bad")).run(
                {"query": "问题", "objective": "o"}, self.context
            )
        self.assertTrue(result.is_error)
        self.assertIn("HTTP 401", result.content)

        with self.patch_client(FakeResponse(status_code=429, text="slow down")):
            limited = await WebSearchTool(WebSearchSettings(provider="exa", api_key="k")).run(
                {"query": "问题", "objective": "o"}, self.context
            )
        self.assertTrue(limited.is_error)
        self.assertIn("限速", limited.content)

    async def test_exa_without_a_key_is_reported(self) -> None:
        result = await WebSearchTool(WebSearchSettings(provider="exa")).run(
            {"query": "问题", "objective": "o"}, self.context
        )

        self.assertTrue(result.is_error)
        self.assertIn("EXA_API_KEY", result.content)
        self.assertEqual(self.calls, [])

    def test_exa_tool_requires_an_objective(self) -> None:
        exa = WebSearchTool(WebSearchSettings(provider="exa", api_key="k"))
        self.assertEqual(exa.parameters["required"], ["query", "objective"])
        self.assertIn("rank first", exa.parameters["properties"]["objective"]["description"])
        # 其他厂商还是一次只问 query
        self.assertEqual(WebSearchTool(WebSearchSettings(provider="bing")).parameters["required"], ["query"])
        self.assertNotIn("objective", WebSearchTool(WebSearchSettings(provider="zhipu")).parameters["properties"])

    async def test_zhipu_search_returns_formatted_results(self) -> None:
        response = FakeResponse(
            payload={
                "search_result": [
                    {"title": "标题", "link": "https://a.example", "content": "摘要", "publish_date": "2026-10-01"}
                ]
            }
        )

        with self.patch_client(response):
            result = await WebSearchTool(WebSearchSettings(provider="zhipu", api_key="search-key")).run(
                {"query": "问题", "count": 3, "recency": "oneWeek"}, self.context
            )

        self.assertFalse(result.is_error)
        self.assertIn("1. 标题（2026-10-01）", result.content)
        self.assertIn("https://a.example", result.content)
        method, url, kwargs = self.calls[0]
        self.assertEqual((method, url), ("POST", "https://open.bigmodel.cn/api/paas/v4/web_search"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer search-key")
        self.assertEqual(kwargs["json"]["search_recency_filter"], "oneWeek")

    async def test_zhipu_http_error_becomes_tool_error(self) -> None:
        with self.patch_client(FakeResponse(status_code=503, text="busy")):
            result = await WebSearchTool(WebSearchSettings(provider="zhipu", api_key="k")).run(
                {"query": "问题"}, self.context
            )

        self.assertTrue(result.is_error)
        self.assertIn("HTTP 503", result.content)

    async def test_deepseek_special_status_codes(self) -> None:
        for status_code, expected in ((402, "余额不足"), (429, "限速")):
            with self.subTest(status_code=status_code), self.patch_client(FakeResponse(status_code=status_code)):
                result = await WebSearchTool(WebSearchSettings(provider="deepseek", api_key="k")).run(
                    {"query": "问题"}, self.context
                )
            self.assertTrue(result.is_error)
            self.assertIn(expected, result.content)

    async def test_deepseek_results_are_mapped(self) -> None:
        response = FakeResponse(
            payload={
                "results": [
                    {"title": "结果", "url": "https://b.example", "summary": "长摘要", "date": "2026-10-02T00:00:00"}
                ]
            }
        )

        with self.patch_client(response):
            result = await WebSearchTool(WebSearchSettings(provider="deepseek", api_key="k")).run(
                {"query": "问题", "recency": "noLimit"}, self.context
            )

        self.assertIn("结果（2026-10-02）", result.content)
        self.assertIn("长摘要", result.content)
        self.assertIn("https://b.example", result.content)

    async def test_bing_without_a_result_page_is_reported(self) -> None:
        with self.patch_client(FakeResponse(status_code=200, text="<html>验证码</html>")):
            result = await WebSearchTool(WebSearchSettings(provider="bing")).run({"query": "问题"}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("没给结果页", result.content)

    async def test_bing_http_error_is_reported(self) -> None:
        with self.patch_client(FakeResponse(status_code=500, text="oops")):
            result = await WebSearchTool(WebSearchSettings(provider="bing")).run({"query": "问题"}, self.context)

        self.assertTrue(result.is_error)
        self.assertIn("必应搜索失败（HTTP 500）", result.content)
