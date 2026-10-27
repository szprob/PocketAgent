"""图片工具：generate_image 生图，search_images 搜图，download_image 存图。

生图走 OpenAI 兼容的 /images/generations（默认智谱 CogView，cogview-3-flash 免费；也能换成别的网关）；
搜图抓必应图片结果页，免费、不用密钥，只给元数据和链接，不下载；下载单独一个工具，因为它是写文件，
而且默认沙箱里不联网。图片落到工作区后，回复里用相对路径 ![...](...) 引用，界面会直接显示，
一轮结束的「本轮产出的图片」也会自动带上。
"""

import base64
import binascii
import datetime
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from pocketagent.tools.base import Tool, ToolAccess, ToolContext, ToolKind, ToolResult
from pocketagent.tools.web import BING_HEADERS, USER_AGENT

IMAGE_GEN_PROVIDERS = ["zhipu", "openai"]
DEFAULT_IMAGE_MODEL_BY_PROVIDER = {"zhipu": "cogview-3-flash", "openai": "gpt-image-1"}
DEFAULT_IMAGE_BASE_URL_BY_PROVIDER = {
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "openai": "https://api.openai.com/v1",
}
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_DIR = "images"
MAX_IMAGE_COUNT = 4
IMAGE_TIMEOUT_SECONDS = 180
DOWNLOAD_TIMEOUT_SECONDS = 60
SEARCH_TIMEOUT_SECONDS = 30
MAX_IMAGE_BYTES = 30 * 1024 * 1024
DEFAULT_SEARCH_COUNT = 5
MAX_SEARCH_COUNT = 10
DESCRIPTION_LIMIT_CHARS = 200
BING_IMAGE_SEARCH_URL = "https://www.bing.com/images/search"
IMAGE_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}

# 必应把每张图的元数据放在结果项 <a class="iusc" m="{...}"> 的 m 属性里（HTML 转义过的 JSON）
BING_IMAGE_ITEM_PATTERN = re.compile(r'<a\b[^>]*class="[^"]*\biusc\b[^"]*"[^>]*>', re.IGNORECASE)
BING_IMAGE_METADATA_PATTERN = re.compile(r'\bm="([^"]*)"')


@dataclass
class ImageGenSettings:
    """生图用的接口、密钥和模型。"""

    provider: str = "zhipu"
    base_url: str = DEFAULT_IMAGE_BASE_URL_BY_PROVIDER["zhipu"]
    api_key: str | None = None
    model: str = DEFAULT_IMAGE_MODEL_BY_PROVIDER["zhipu"]


@dataclass
class ImagePayload:
    """一张图片的字节和落盘用的扩展名。"""

    data: bytes
    extension: str


class ImageRequestFailed(Exception):
    """生图 / 搜图 / 下载没成功；消息直接当工具结果给模型看。"""


# ---- 公共小工具 ----


def image_generation_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/images/generations"


def image_file_stem(text: str, fallback: str = "image") -> str:
    """把提示词或文件名压成能落盘的短名字；中文保留，路径符号去掉。"""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "", text)
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-. ")
    return cleaned[:40].strip("-. ") or fallback


def unique_image_path(directory: Path, stem: str, extension: str) -> Path:
    path = directory / f"{stem}{extension}"
    counter = 2
    while path.exists():
        path = directory / f"{stem}-{counter}{extension}"
        counter += 1
    return path


def sniff_image_extension(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"GIF8"):
        return ".gif"
    return None


def image_extension(mime: str, data: bytes) -> str:
    extension = IMAGE_EXTENSION_BY_MIME.get(mime) or sniff_image_extension(data)
    return extension or ".png"


def format_size(byte_count: int) -> str:
    if byte_count >= 1024 * 1024:
        return f"{byte_count / 1024 / 1024:.1f} MB"
    return f"{byte_count / 1024:.0f} KB"


def display_reference(path: Path, context: ToolContext) -> tuple[str, str | None]:
    """返回 (给模型看的路径, 界面能内嵌显示的相对路径)；工作区外的文件没有相对路径。"""
    shown = context.display_path(path)
    return shown, (None if Path(shown).is_absolute() else shown)


def save_path_access(arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
    """generate_image / download_image 的落盘位置：save_path 没给就默认工作区的 images/ 目录。

    不能用 files.path_argument_access，它只认 path 参数，这里 save_path 没给也要把默认目录算进去。
    """
    raw = str(arguments.get("save_path") or "").strip()
    target = context.resolve_path(raw) if raw else context.resolve_path(DEFAULT_IMAGE_DIR)
    return context.path_access([target], write=True)


# ---- 生图 ----


async def request_generated_images(
    settings: ImageGenSettings, prompt: str, size: str, count: int
) -> list[ImagePayload]:
    """调生图接口。OpenAI 兼容的接口支持 n，智谱一次只出一张，多张就多调几次。"""
    payload: dict[str, Any] = {"model": settings.model, "prompt": prompt, "size": size}
    rounds = count
    if settings.provider == "openai" and count > 1:
        payload["n"] = count
        rounds = 1
    payloads: list[ImagePayload] = []
    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_SECONDS) as client:
        for _ in range(rounds):
            for item in await _post_generation(client, settings, payload):
                payloads.append(await _payload_from_item(client, item))
    if not payloads:
        raise ImageRequestFailed("生图接口没有返回图片")
    return payloads


async def _post_generation(
    client: httpx.AsyncClient, settings: ImageGenSettings, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    url = image_generation_url(settings.base_url)
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    try:
        response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as error:
        raise ImageRequestFailed(f"连不上生图接口：{type(error).__name__}: {error}") from error
    if response.status_code in (401, 403):
        raise ImageRequestFailed(
            f"生图接口鉴权失败（HTTP {response.status_code}），检查 API Key：{_error_text(response)}"
        )
    if response.status_code != 200:
        raise ImageRequestFailed(f"生图失败（HTTP {response.status_code}）：{_error_text(response)}")
    try:
        return response.json().get("data") or []
    except ValueError as error:
        raise ImageRequestFailed(f"生图接口返回的不是 JSON：{response.text[:200]}") from error


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or payload)[:300]


async def _payload_from_item(client: httpx.AsyncClient, item: dict[str, Any]) -> ImagePayload:
    encoded = item.get("b64_json")
    if encoded:
        try:
            data = base64.b64decode(encoded)
        except (binascii.Error, ValueError) as error:
            raise ImageRequestFailed(f"接口返回的 base64 图片解不开：{error}") from error
        return ImagePayload(data=data, extension=image_extension("", data))
    image_url = str(item.get("url") or "")
    if not image_url:
        raise ImageRequestFailed("接口返回的条目里既没有 b64_json 也没有 url")
    try:
        response = await client.get(image_url, follow_redirects=True)
    except httpx.HTTPError as error:
        raise ImageRequestFailed(f"下载生成的图片失败：{type(error).__name__}: {error}") from error
    if response.status_code >= 400:
        raise ImageRequestFailed(f"下载生成的图片失败（HTTP {response.status_code}）：{image_url}")
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return ImagePayload(data=response.content, extension=image_extension(mime, response.content))


class GenerateImageTool(Tool):
    name = "generate_image"
    description = (
        "用 AI 生成图片（文生图），存到工作区并返回相对路径。画插图、图标、背景、示意图，"
        "或者用户直接要一张图时用；回复里用返回的 ![...](...) 引用，界面会直接显示。"
    )
    kind = ToolKind.EDIT
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "画面描述，越具体越好：主体、风格、构图、颜色；需要画面里有文字时把文字写清楚",
            },
            "size": {
                "type": "string",
                "description": (
                    f"尺寸，如 {DEFAULT_IMAGE_SIZE}（默认）、1344x768（横）、768x1344（竖）；"
                    "各家支持的尺寸有限，传错了接口会报错"
                ),
            },
            "count": {"type": "integer", "description": f"生成几张，1–{MAX_IMAGE_COUNT}，默认 1"},
            "save_path": {
                "type": "string",
                "description": "保存位置：一张时是文件路径，多张时是目录；默认 images/ 下按时间命名",
            },
        },
        "required": ["prompt"],
    }

    def __init__(self, settings: ImageGenSettings) -> None:
        self.settings = settings

    def describe(self, arguments: dict[str, Any]) -> str:
        prompt = str(arguments.get("prompt", ""))
        return prompt if len(prompt) <= 120 else prompt[:117] + "..."

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return save_path_access(arguments, context)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return ToolResult("prompt 不能为空", is_error=True)
        size = str(arguments.get("size") or DEFAULT_IMAGE_SIZE).strip()
        if not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
            return ToolResult(f"尺寸格式不对（要 宽x高，比如 {DEFAULT_IMAGE_SIZE}）：{size}", is_error=True)
        try:
            count = int(arguments.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = min(max(count, 1), MAX_IMAGE_COUNT)
        raw_target = str(arguments.get("save_path") or "").strip()
        if count > 1 and raw_target and Path(raw_target).suffix:
            return ToolResult("生成多张时 save_path 要传目录，不能是文件", is_error=True)
        try:
            payloads = await request_generated_images(self.settings, prompt, size, count)
        except ImageRequestFailed as error:
            return ToolResult(str(error), is_error=True)
        save_as_file = bool(raw_target) and bool(Path(raw_target).suffix)
        target = context.resolve_path(raw_target) if raw_target else context.resolve_path(DEFAULT_IMAGE_DIR)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = image_file_stem(prompt)
        saved: list[Path] = []
        for index, payload in enumerate(payloads, start=1):
            if save_as_file:
                path = unique_image_path(target.parent, target.stem, target.suffix or payload.extension)
            else:
                name = f"{stamp}-{stem}" if index == 1 else f"{stamp}-{stem}-{index}"
                path = unique_image_path(target, name, payload.extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload.data)
            saved.append(path)
        return ToolResult(self.report(saved, prompt, size, context))

    def report(self, saved: list[Path], prompt: str, size: str, context: ToolContext) -> str:
        lines = [f"生成好了 {len(saved)} 张（{self.settings.model}，{size}）："]
        alt = image_file_stem(prompt)
        for index, path in enumerate(saved, start=1):
            shown, relative = display_reference(path, context)
            size_text = format_size(path.stat().st_size)
            if relative:
                lines.append(f"{index}. ![{alt}]({relative}) · 本机 {shown} · {size_text}")
            else:
                lines.append(f"{index}. 本机 {shown} · {size_text}（在工作区外，界面不会内嵌显示）")
        lines.append("（工具结果只有路径，你看不到图片画面，不要描述画面内容。）")
        return "\n".join(lines)


# ---- 搜图 ----


def parse_bing_images(page: str) -> list[dict[str, Any]]:
    """从必应图片结果页里抠出标题、原图、缩略图、来源；解析不出来的条目直接跳过。"""
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for tag in BING_IMAGE_ITEM_PATTERN.findall(page):
        metadata = BING_IMAGE_METADATA_PATTERN.search(tag)
        if metadata is None:
            continue
        try:
            item = json.loads(html.unescape(metadata.group(1)))
        except json.JSONDecodeError:
            continue
        image_url = str(item.get("murl") or "")
        if not image_url or image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        source_url = str(item.get("purl") or "")
        results.append(
            {
                "title": str(item.get("t") or "").strip(),
                "image_url": image_url,
                "thumbnail_url": str(item.get("turl") or ""),
                "source_url": source_url,
                "hostname": urlparse(source_url).hostname or "",
                "description": re.sub(r"\s+", " ", str(item.get("desc") or "")).strip(),
            }
        )
    return results


async def search_bing_images(query: str, count: int) -> list[dict[str, Any]]:
    params = {"q": query, "form": "HDRSC2", "first": "1", "setlang": "zh-Hans"}
    try:
        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT_SECONDS, follow_redirects=True, headers=BING_HEADERS
        ) as client:
            response = await client.get(BING_IMAGE_SEARCH_URL, params=params)
    except httpx.HTTPError as error:
        raise ImageRequestFailed(f"连不上必应图片搜索：{type(error).__name__}: {error}") from error
    if response.status_code != 200:
        raise ImageRequestFailed(f"必应图片搜索失败（HTTP {response.status_code}），稍后再试")
    results = parse_bing_images(response.text)
    if not results and "iusc" not in response.text:
        # 正常没结果时页面里也有 iusc 结果项；连它都没有，多半是被限流了
        raise ImageRequestFailed("必应这次没给结果页（可能被限流了），稍后再试")
    return results[:count]


def format_search_results(results: list[dict[str, Any]]) -> str:
    lines = [f"找到 {len(results)} 张图："]
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item['title'] or '（无标题）'}")
        lines.append(f"   原图：{item['image_url']}")
        if item["thumbnail_url"]:
            lines.append(f"   缩略图：{item['thumbnail_url']}")
        if item["source_url"]:
            host = f"（{item['hostname']}）" if item["hostname"] else ""
            lines.append(f"   来源：{item['source_url']}{host}")
        if item["description"]:
            lines.append(f"   简介：{item['description'][:DESCRIPTION_LIMIT_CHARS]}")
    lines.append("要把哪张存到工作区，用 download_image 传它的原图链接。")
    return "\n".join(lines)


class SearchImagesTool(Tool):
    name = "search_images"
    description = (
        "搜索网上现成的图片，返回标题、原图链接、缩略图和来源。找参考图、素材时用；"
        "要把图存进项目再用 download_image。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "count": {"type": "integer", "description": f"返回几张，1–{MAX_SEARCH_COUNT}，默认 {DEFAULT_SEARCH_COUNT}"},
        },
        "required": ["query"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("query", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult("query 不能为空", is_error=True)
        try:
            count = int(arguments.get("count") or DEFAULT_SEARCH_COUNT)
        except (TypeError, ValueError):
            count = DEFAULT_SEARCH_COUNT
        count = min(max(count, 1), MAX_SEARCH_COUNT)
        try:
            results = await search_bing_images(query, count)
        except ImageRequestFailed as error:
            return ToolResult(str(error), is_error=True)
        if not results:
            return ToolResult("没有搜到图片，换个说法试试")
        return ToolResult(format_search_results(results))


# ---- 下载 ----


async def fetch_image_bytes(image_url: str) -> ImagePayload:
    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        try:
            response = await client.get(image_url)
        except httpx.HTTPError as error:
            raise ImageRequestFailed(f"连不上图片地址：{type(error).__name__}: {error}") from error
    if response.status_code >= 400:
        raise ImageRequestFailed(f"下载图片失败（HTTP {response.status_code}）：{image_url}")
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise ImageRequestFailed(f"这个地址不是图片（{mime or '未知类型'}）：{image_url}")
    if len(response.content) > MAX_IMAGE_BYTES:
        raise ImageRequestFailed(
            f"图片太大（{format_size(len(response.content))}，上限 {format_size(MAX_IMAGE_BYTES)}）：{image_url}"
        )
    return ImagePayload(data=response.content, extension=image_extension(mime, response.content))


class DownloadImageTool(Tool):
    name = "download_image"
    description = (
        "把网上的图片（比如 search_images 结果里的原图链接）下载到工作区，返回相对路径；"
        "回复里可以用 ![...](...) 引用。"
    )
    kind = ToolKind.EDIT
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "图片地址（http / https）"},
            "save_path": {"type": "string", "description": "保存位置，默认 images/ 下按时间命名；传目录或文件路径都行"},
        },
        "required": ["url"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("url", ""))

    def access(self, arguments: dict[str, Any], context: ToolContext) -> ToolAccess:
        return save_path_access(arguments, context)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        image_url = str(arguments.get("url") or "").strip()
        if urlparse(image_url).scheme not in ("http", "https"):
            return ToolResult("只支持 http / https 图片地址", is_error=True)
        raw_target = str(arguments.get("save_path") or "").strip()
        try:
            payload = await fetch_image_bytes(image_url)
        except ImageRequestFailed as error:
            return ToolResult(str(error), is_error=True)
        target = context.resolve_path(raw_target) if raw_target else context.resolve_path(DEFAULT_IMAGE_DIR)
        if target.suffix:
            path = unique_image_path(target.parent, target.stem, target.suffix or payload.extension)
        else:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"{stamp}-{image_file_stem(Path(unquote(urlparse(image_url).path)).stem, 'image')}"
            path = unique_image_path(target, name, payload.extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload.data)
        shown, relative = display_reference(path, context)
        size_text = format_size(len(payload.data))
        if relative:
            return ToolResult(f"已保存到 {relative}（{size_text}）\n![{path.stem}]({relative})\n本机路径：{shown}")
        return ToolResult(f"已保存到 {shown}（{size_text}；在工作区外，界面不会内嵌显示）")
