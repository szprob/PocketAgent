"""联网工具：web_search 搜索，web_fetch 读网页。

搜索用智谱的 Web Search API（国内直连，2–3 秒），密钥没单独配就用配置里智谱模型的密钥。
读网页直接用 httpx 抓，HTML 转成纯文本给模型看。两个都是只读工具，不需要审批。
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

ZHIPU_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
SEARCH_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 20
SNIPPET_LIMIT_CHARS = 600
DEFAULT_FETCH_LIMIT_CHARS = 20_000
MAX_FETCH_LIMIT_CHARS = 80_000
RECENCY_CHOICES = ["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"]
USER_AGENT = "Mozilla/5.0 (compatible; PocketAgent/0.1; +https://github.com/szprob/PocketAgent)"


@dataclass
class WebSearchSettings:
    api_key: str
    # search_std 基础版 / search_pro 高阶版
    engine: str = "search_std"


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索，返回标题、链接、日期和摘要。查最新信息、文档、报错原因时用；"
        "摘要不够就再用 web_fetch 打开链接看全文。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "count": {"type": "integer", "description": "返回几条，1–10，默认 5"},
            "recency": {
                "type": "string",
                "enum": RECENCY_CHOICES,
                "description": "时间范围：noLimit 不限（默认）/ oneDay / oneWeek / oneMonth / oneYear",
            },
        },
        "required": ["query"],
    }

    def __init__(self, settings: WebSearchSettings) -> None:
        self.settings = settings

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("query", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        count = min(max(int(arguments.get("count") or 5), 1), 10)
        recency = arguments.get("recency") or "noLimit"
        if recency not in RECENCY_CHOICES:
            recency = "noLimit"
        payload = {
            "search_query": arguments["query"],
            "search_engine": self.settings.engine,
            "count": count,
            "search_recency_filter": recency,
        }
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                ZHIPU_SEARCH_URL, json=payload, headers={"Authorization": f"Bearer {self.settings.api_key}"}
            )
        if response.status_code != 200:
            return ToolResult(f"搜索失败（HTTP {response.status_code}）：{response.text[:300]}", is_error=True)
        results = response.json().get("search_result") or []
        if not results:
            return ToolResult("没有搜到结果，换个说法试试")
        return ToolResult("\n\n".join(_format_result(index, item) for index, item in enumerate(results, start=1)))


def _format_result(index: int, item: dict[str, Any]) -> str:
    snippet = re.sub(r"\s+", " ", item.get("content") or "").strip()[:SNIPPET_LIMIT_CHARS]
    date = f"（{item['publish_date']}）" if item.get("publish_date") else ""
    return f"{index}. {item.get('title', '').strip()}{date}\n   {item.get('link', '')}\n   {snippet}"


# ---- 读网页 ----

SKIPPED_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe", "nav", "footer", "aside", "form"}
# 页面有 <main> / <article> 且里面字够多，就只要这部分，去掉侧栏、导航之类
CONTENT_TAGS = {"main", "article"}
MIN_MAIN_CONTENT_CHARS = 300
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "br", "li", "ul", "ol", "tr", "table", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _TextExtractor(HTMLParser):
    """把 HTML 变成大致保留段落的纯文本，顺便拿到标题。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # <main> / <article> 里的文字另记一份
        self.content_parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._content_depth = 0
        self._in_title = False

    def _append(self, text: str) -> None:
        self.parts.append(text)
        if self._content_depth > 0:
            self.content_parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TAGS:
            self._skip_depth += 1
        if tag in CONTENT_TAGS:
            self._content_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in BLOCK_TAGS:
            self._append("\n")
        if tag in ("h1", "h2", "h3"):
            self._append("## ")
        if tag == "li":
            self._append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self._append("\n")
        if tag in CONTENT_TAGS and self._content_depth > 0:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self._append(data)

    def text(self) -> str:
        main_content = self._tidy(self.content_parts)
        if len(main_content) >= MIN_MAIN_CONTENT_CHARS:
            return main_content
        return self._tidy(self.parts)

    @staticmethod
    def _tidy(parts: list[str]) -> str:
        joined = "".join(parts)
        lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in joined.splitlines()]
        # 只剩一个符号的空行（空列表项、空标题、表格分隔）也去掉
        compact = "\n".join(line for line in lines if line and line not in ("-", "##", "|"))
        return re.sub(r"\n{3,}", "\n\n", compact)


def html_to_text(html: str) -> tuple[str, str]:
    """返回 (标题, 正文纯文本)。"""
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.title.strip(), extractor.text()


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "打开一个网页（http / https），返回标题和正文纯文本。看文档、读搜索结果里的链接时用。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "网址"},
            "max_chars": {
                "type": "integer",
                "description": f"最多返回多少字，默认 {DEFAULT_FETCH_LIMIT_CHARS}，最多 {MAX_FETCH_LIMIT_CHARS}",
            },
        },
        "required": ["url"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("url", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = str(arguments["url"]).strip()
        if urlparse(url).scheme not in ("http", "https"):
            return ToolResult("只支持 http / https 网址", is_error=True)
        limit = min(int(arguments.get("max_chars") or DEFAULT_FETCH_LIMIT_CHARS), MAX_FETCH_LIMIT_CHARS)
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            return ToolResult(f"打开失败（HTTP {response.status_code}）：{url}", is_error=True)
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            title, body = html_to_text(response.text)
            text = f"# {title}\n{response.url}\n\n{body}" if title else f"{response.url}\n\n{body}"
        elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
            text = f"{response.url}\n\n{response.text}"
        else:
            return ToolResult(f"不是文本内容（{content_type or '未知类型'}，{len(response.content)} 字节）：{response.url}")
        if len(text) > limit:
            # 网页一般开头最要紧，截掉后面
            text = f"{text[:limit]}\n\n（后面还有 {len(text) - limit} 字没显示，需要的话把 max_chars 调大再读）"
        return ToolResult(text)
