"""联网工具：web_search 搜索，web_fetch 读网页。

搜索默认抓必应的结果页（免费、不用密钥）；也可以在设置里换成 DeepSeek 或智谱的搜索接口，
按调用次数计费，密钥没单独配就用对应厂商模型的密钥。读网页直接用 httpx 抓，HTML 转成纯文本
给模型看。两个都是只读工具，不需要审批。
"""

import base64
import binascii
import datetime
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

ZHIPU_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
BING_SEARCH_URL = "https://www.bing.com/search"
SEARCH_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 20
SNIPPET_LIMIT_CHARS = 600
DEFAULT_FETCH_LIMIT_CHARS = 20_000
MAX_FETCH_LIMIT_CHARS = 80_000
RECENCY_CHOICES = ["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"]
USER_AGENT = "Mozilla/5.0 (compatible; PocketAgent/0.1; +https://github.com/szprob/PocketAgent)"
# 必应看到浏览器 UA 才给完整的结果页，PocketAgent 自己的 UA 会被当成爬虫
BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# 必应的时间筛选：它自己只有一天 / 一周 / 一个月三档（结果页上「时间不限」那个下拉用的就是这几个值）
BING_RECENCY_FILTERS = {"oneDay": "ez1", "oneWeek": "ez2", "oneMonth": "ez3"}
BING_EPOCH = datetime.date(1970, 1, 1)
# DeepSeek 没有单独的时间参数，用它的 after: 搜索语法，往前推几天
DEEPSEEK_RECENCY_DAYS = {"oneDay": 1, "oneWeek": 7, "oneMonth": 30, "oneYear": 365}


@dataclass
class WebSearchSettings:
    # bing 直接抓结果页，免费；deepseek / zhipu 走官方接口，按次计费
    provider: str = "bing"
    # 只有智谱要用密钥
    api_key: str | None = None
    # 智谱的档位：search_std 基础版 / search_pro 高阶版
    engine: str = "search_std"


class SearchFailed(Exception):
    """搜索没成功；消息直接当工具结果给模型看。"""


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索，返回标题、链接和摘要。查最新信息、文档、报错原因时用；"
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
        query = str(arguments.get("query") or "").strip()
        count = min(max(int(arguments.get("count") or 5), 1), 10)
        recency = arguments.get("recency") or "noLimit"
        if recency not in RECENCY_CHOICES:
            recency = "noLimit"
        try:
            if self.settings.provider == "zhipu":
                results = await self._search_zhipu(query, count, recency)
            elif self.settings.provider == "deepseek":
                results = await self._search_deepseek(query, count, recency)
            else:
                results = await self._search_bing(query, count, recency)
        except SearchFailed as error:
            return ToolResult(str(error), is_error=True)
        if not results:
            return ToolResult("没有搜到结果，换个说法试试")
        return ToolResult("\n\n".join(_format_result(index, item) for index, item in enumerate(results, start=1)))

    async def _search_zhipu(self, query: str, count: int, recency: str) -> list[dict[str, Any]]:
        payload = {
            "search_query": query,
            "search_engine": self.settings.engine,
            "count": count,
            "search_recency_filter": recency,
        }
        try:
            async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    ZHIPU_SEARCH_URL, json=payload, headers={"Authorization": f"Bearer {self.settings.api_key}"}
                )
        except httpx.HTTPError as error:
            raise SearchFailed(f"连不上智谱搜索接口：{type(error).__name__}: {error}") from error
        if response.status_code != 200:
            raise SearchFailed(f"搜索失败（HTTP {response.status_code}）：{response.text[:300]}")
        return response.json().get("search_result") or []

    async def _search_deepseek(self, query: str, count: int, recency: str) -> list[dict[str, Any]]:
        payload = {
            "query": deepseek_query_with_recency(query, recency),
            "count": count,
            # 只要摘要：正文交给 web_fetch 按需读，别一次塞太多
            "need_content": False,
        }
        try:
            async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    DEEPSEEK_SEARCH_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                )
        except httpx.HTTPError as error:
            raise SearchFailed(f"连不上 DeepSeek 搜索接口：{type(error).__name__}: {error}") from error
        if response.status_code == 402:
            raise SearchFailed("DeepSeek 账户余额不足，搜索用不了；可以换成必应（免费）")
        if response.status_code == 429:
            raise SearchFailed("DeepSeek 搜索被限速了（账号级 100 QPS），稍后再试")
        if response.status_code != 200:
            raise SearchFailed(
                f"DeepSeek 搜索失败（HTTP {response.status_code}）：{_deepseek_error_message(response)}"
            )
        return [_deepseek_result(item) for item in response.json().get("results") or []]

    async def _search_bing(self, query: str, count: int, recency: str) -> list[dict[str, Any]]:
        params = {"q": query, "count": str(count), "setlang": "zh-Hans"}
        recency_filter = bing_recency_filter(recency)
        if recency_filter:
            params["filters"] = recency_filter
        try:
            async with httpx.AsyncClient(
                timeout=SEARCH_TIMEOUT_SECONDS, follow_redirects=True, headers=BING_HEADERS
            ) as client:
                response = await client.get(BING_SEARCH_URL, params=params)
        except httpx.HTTPError as error:
            raise SearchFailed(f"连不上必应：{type(error).__name__}: {error}") from error
        if response.status_code != 200:
            raise SearchFailed(f"必应搜索失败（HTTP {response.status_code}），稍后再试或者换成智谱搜索")
        results = parse_bing_results(response.text)
        if not results and "b_results" not in response.text:
            # 正常没结果时页面里也有 b_results 容器；连它都没有，多半是被限流了
            raise SearchFailed("必应这次没给结果页（可能被限流了），稍后再试或者换成智谱搜索")
        return results[:count]


def _format_result(index: int, item: dict[str, Any]) -> str:
    snippet = re.sub(r"\s+", " ", item.get("content") or "").strip()[:SNIPPET_LIMIT_CHARS]
    date = f"（{item['publish_date']}）" if item.get("publish_date") else ""
    return f"{index}. {item.get('title', '').strip()}{date}\n   {item.get('link', '')}\n   {snippet}"


# ---- DeepSeek 搜索 ----

def deepseek_query_with_recency(query: str, recency: str) -> str:
    """DeepSeek 没有单独的时间参数，用它支持的 after: 语法往前卡日期。"""
    days = DEEPSEEK_RECENCY_DAYS.get(recency)
    if not days:
        return query
    since = datetime.date.today() - datetime.timedelta(days=days)
    return f"{query} after:{since.isoformat()}"


def _deepseek_result(item: dict[str, Any]) -> dict[str, Any]:
    """转成和智谱一样的形状，后面共用一套格式化。"""
    return {
        "title": item.get("title") or "",
        "link": item.get("url") or "",
        # summary 是给模型读的长摘要（600–800 字），没有就退回 snippet
        "content": item.get("summary") or item.get("snippet") or "",
        "publish_date": str(item.get("date") or "")[:10],
    }


def _deepseek_error_message(response: httpx.Response) -> str:
    try:
        message = response.json().get("error", {}).get("message")
    except ValueError:
        message = None
    return message or response.text[:300]


# ---- 必应结果页 ----

def bing_recency_filter(recency: str) -> str | None:
    """必应的时间筛选参数。它只有一天 / 一周 / 一个月三档，一年用自定义区间（天数从 1970-01-01 算）。"""
    if recency == "oneYear":
        today = datetime.date.today()
        start = today - datetime.timedelta(days=365)
        return f'ex1:"ez5_{(start - BING_EPOCH).days}_{(today - BING_EPOCH).days}"'
    code = BING_RECENCY_FILTERS.get(recency)
    return f'ex1:"{code}"' if code else None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _BingResultParser(HTMLParser):
    """从必应结果页里抠出每条结果的标题、链接和摘要。

    必应把结果放在 li.b_algo 里，标题是其中第一个 h2 下的 a；li 里面还会套 li（站内链接），
    所以按 li 的层数判断一条结果在哪结束。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._list_depth = 0
        self._current: dict[str, str] | None = None
        self._in_title = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li":
            if self._list_depth == 0 and "b_algo" in (attributes.get("class") or "").split():
                self._current = {}
                self._list_depth = 1
                return
            if self._list_depth:
                self._list_depth += 1
        if self._current is None:
            return
        if tag == "h2" and not self._current.get("title"):
            self._in_title = True
            self._title_parts = []
        elif tag == "a" and self._in_title and "link" not in self._current:
            self._current["link"] = _unwrap_bing_link(attributes.get("href") or "")
        elif tag == "p" and not self._current.get("content"):
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "h2" and self._in_title:
            self._in_title = False
            self._current["title"] = _squash("".join(self._title_parts))
        elif tag == "p" and self._in_snippet:
            self._in_snippet = False
            self._current["content"] = _squash("".join(self._snippet_parts))
        elif tag == "li" and self._list_depth:
            self._list_depth -= 1
            if self._list_depth == 0:
                finished, self._current = self._current, None
                if finished.get("title") and finished.get("link"):
                    self.results.append(finished)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_snippet:
            self._snippet_parts.append(data)


def _unwrap_bing_link(href: str) -> str:
    """必应有时把结果链接包成 /ck/a?...&u=a1<base64>，还原成真实地址。"""
    parsed = urlparse(href)
    if not parsed.path.startswith("/ck/a"):
        return href
    encoded = (parse_qs(parsed.query).get("u") or [""])[0]
    if not encoded.startswith("a1"):
        return href
    payload = encoded[2:]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (binascii.Error, ValueError):
        return href
    return decoded.decode("utf-8", "replace") or href


def parse_bing_results(html: str) -> list[dict[str, str]]:
    """解析必应结果页；解析不出来就返回空列表。"""
    parser = _BingResultParser()
    parser.feed(html)
    return parser.results


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
