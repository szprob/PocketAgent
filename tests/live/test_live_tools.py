"""真实免费接口的联调：读网页、必应搜索 / 搜图、下载图片、智谱免费生图。

这些不需要模型，但和其他 live 测试一样只在 `make test-live` 时跑。
"""

import re
import tempfile
import unittest
from pathlib import Path

from pocketagent.tools.base import ToolContext
from pocketagent.tools.image import (
    DownloadImageTool,
    GenerateImageTool,
    SearchImagesTool,
    sniff_image_extension,
)
from pocketagent.tools.web import WebFetchTool, WebSearchTool, WebSearchSettings
from tests.live.support import resolve_image_settings, skip_when_live_disabled

URL_PATTERN = re.compile(r"https?://[^\s)]+")


@skip_when_live_disabled
class LiveWebToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary.name)
        self.context = ToolContext(workspace=self.workspace)

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    async def test_web_fetch_reads_example_com(self) -> None:
        result = await WebFetchTool().run({"url": "https://example.com"}, self.context)

        self.assertFalse(result.is_error, result.content)
        self.assertIn("Example Domain", result.content)

    async def test_bing_search_returns_results(self) -> None:
        result = await WebSearchTool(WebSearchSettings(provider="bing")).run(
            {"query": "PocketFlow github", "count": 3}, self.context
        )

        self.assertFalse(result.is_error, result.content)
        self.assertIn("PocketFlow", result.content)
        self.assertGreaterEqual(len(URL_PATTERN.findall(result.content)), 3)

    async def test_bing_image_search_and_download(self) -> None:
        search_result = await SearchImagesTool().run({"query": "cute cat", "count": 3}, self.context)
        self.assertFalse(search_result.is_error, search_result.content)
        image_urls = URL_PATTERN.findall(search_result.content)
        self.assertTrue(image_urls, "搜图结果里没有图片链接")

        # 图片站可能拦截外链，前几张里能下到一张就算通过
        saved_path = self.workspace / "downloaded.img"
        for url in image_urls[:3]:
            download_result = await DownloadImageTool().run(
                {"url": url, "save_path": str(saved_path)}, self.context
            )
            if not download_result.is_error:
                break
        self.assertTrue(saved_path.is_file(), "前三张图片都没下载成功")
        self.assertIsNotNone(sniff_image_extension(saved_path.read_bytes()), "下载到的不是图片")


IMAGE_SETTINGS = resolve_image_settings()


@skip_when_live_disabled
@unittest.skipIf(IMAGE_SETTINGS is None, "没有智谱密钥，跳过免费生图（cogview-3-flash）")
class LiveImageGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary.name)
        self.context = ToolContext(workspace=self.workspace)

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    async def test_generate_image_saves_png(self) -> None:
        result = await GenerateImageTool(IMAGE_SETTINGS).run(
            {"prompt": "纯白背景上一个小红圆点，极简风格", "save_path": "generated.png"},
            self.context,
        )

        self.assertFalse(result.is_error, result.content)
        target = self.workspace / "generated.png"
        self.assertTrue(target.is_file(), "生图工具没有把图片写到工作区")
        self.assertIsNotNone(sniff_image_extension(target.read_bytes()), "生成的不是图片")
