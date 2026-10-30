"""图片工具的单元测试：必应结果页解析、命名、配置解析、三个工具的落盘行为（网络全部打桩）。"""

import asyncio
import base64
import html
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pocketagent.config import AppConfig
from pocketagent.tools.base import ToolContext
from pocketagent.tools.image import (
    DEFAULT_IMAGE_SIZE,
    DownloadImageTool,
    GenerateImageTool,
    ImageGenSettings,
    ImagePayload,
    ImageRequestFailed,
    SearchImagesTool,
    image_extension,
    image_file_stem,
    parse_bing_images,
    sniff_image_extension,
    unique_image_path,
)
from tests.helpers import TemporaryDirectories

# 1×1 的透明 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def bing_item(title: str, image_url: str, source_url: str) -> str:
    metadata = {"t": title, "murl": image_url, "turl": "https://thumb.example/x", "purl": source_url, "desc": "一张图"}
    return f'<a class="iusc" m="{html.escape(json.dumps(metadata), quote=True)}"></a>'


class BingParseTest(unittest.TestCase):
    def test_extracts_and_deduplicates(self) -> None:
        page = (
            "<html><body>"
            + bing_item("标题一", "https://a.example/1.png", "https://a.example/page")
            + bing_item("标题二", "https://b.example/2.jpg", "https://b.example/page")
            + bing_item("重复", "https://a.example/1.png", "https://a.example/page")
            + '<a class="other" m="{}"></a>'
            + "</body></html>"
        )
        results = parse_bing_images(page)
        self.assertEqual([item["title"] for item in results], ["标题一", "标题二"])
        self.assertEqual(results[0]["image_url"], "https://a.example/1.png")
        self.assertEqual(results[0]["hostname"], "a.example")
        self.assertEqual(results[0]["description"], "一张图")

    def test_no_results(self) -> None:
        self.assertEqual(parse_bing_images("<html><body>没有结果</body></html>"), [])


class NamingTest(unittest.TestCase):
    def test_stem_keeps_chinese_and_strips_symbols(self) -> None:
        self.assertEqual(image_file_stem("一只 橘猫/橘!"), "一只-橘猫橘!")

    def test_unique_path_avoids_existing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            first = unique_image_path(directory, "猫", ".png")
            first.write_bytes(b"x")
            second = unique_image_path(directory, "猫", ".png")
            self.assertEqual(first.name, "猫.png")
            self.assertEqual(second.name, "猫-2.png")

    def test_sniff_extension(self) -> None:
        self.assertEqual(sniff_image_extension(TINY_PNG), ".png")
        self.assertIsNone(sniff_image_extension(b"not an image"))
        self.assertEqual(image_extension("", TINY_PNG), ".png")
        self.assertEqual(image_extension("image/webp", b""), ".webp")


class ImageGenConfigTest(unittest.TestCase):
    def test_zhipu_key_falls_back_to_bigmodel_model(self) -> None:
        config = AppConfig.from_dict(
            {
                "models": [
                    {
                        "id": "glm",
                        "name": "GLM",
                        "model": "glm-5.3-flash",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "api_key": "sk-glm",
                    }
                ]
            }
        )
        settings = config.image_gen_settings()
        self.assertEqual(settings.provider, "zhipu")
        self.assertEqual(settings.model, "cogview-3-flash")
        self.assertEqual(settings.api_key, "sk-glm")
        self.assertEqual(settings.base_url, "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(config.image_gen_key_source(), "用模型「GLM」的密钥")

    def test_explicit_overrides_win(self) -> None:
        config = AppConfig.from_dict(
            {
                "image_gen_provider": "zhipu",
                "image_gen_model": "cogview-4",
                "image_gen_base_url": "https://proxy.example/v4",
                "image_gen_api_key": "sk-mine",
                "models": [
                    {"id": "glm", "name": "GLM", "model": "glm", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "sk-glm"}
                ],
            }
        )
        settings = config.image_gen_settings()
        self.assertEqual((settings.model, settings.base_url, settings.api_key), ("cogview-4", "https://proxy.example/v4", "sk-mine"))

    def test_openai_uses_first_openai_gateway(self) -> None:
        config = AppConfig.from_dict(
            {
                "image_gen_provider": "openai",
                "models": [
                    {"id": "gw", "name": "GW", "model": "m1", "base_url": "https://gw.example/v1", "api_key": "sk-gw"}
                ],
            }
        )
        settings = config.image_gen_settings()
        self.assertEqual(settings.base_url, "https://gw.example/v1")
        self.assertEqual(settings.model, "gpt-image-1")

    def test_no_key_or_disabled_means_no_tool(self) -> None:
        self.assertIsNone(AppConfig.from_dict({}).image_gen_settings())
        with_key = {"models": [{"id": "glm", "name": "GLM", "model": "glm", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "sk-glm"}]}
        self.assertIsNotNone(AppConfig.from_dict(with_key).image_gen_settings())
        disabled = {**with_key, "image_gen_enabled": False}
        self.assertIsNone(AppConfig.from_dict(disabled).image_gen_settings())


class GenerateImageToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.context = ToolContext(workspace=self.directories.workspace)
        self.tool = GenerateImageTool(ImageGenSettings())

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_saves_into_workspace_and_reports_markdown(self) -> None:
        async def fake_generate(settings, prompt, size, count):
            return [ImagePayload(data=TINY_PNG, extension=".png")]

        with patch("pocketagent.tools.image.request_generated_images", fake_generate):
            result = asyncio.run(self.tool.run({"prompt": "一只橘猫"}, self.context))

        self.assertFalse(result.is_error)
        files = sorted((self.directories.workspace / "images").glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertIn(f"![一只橘猫](images/{files[0].name})", result.content)
        self.assertIn("你看不到图片画面", result.content)

    def test_count_saves_multiple_into_directory(self) -> None:
        async def fake_generate(settings, prompt, size, count):
            return [ImagePayload(data=TINY_PNG, extension=".png")] * count

        with patch("pocketagent.tools.image.request_generated_images", fake_generate):
            result = asyncio.run(self.tool.run({"prompt": "猫", "count": 2, "save_path": "assets"}, self.context))

        self.assertFalse(result.is_error)
        files = sorted((self.directories.workspace / "assets").glob("*.png"))
        self.assertEqual(len(files), 2)
        self.assertEqual([file.name for file in files], sorted(file.name for file in files))

    def test_rejects_bad_size_and_file_target_for_count(self) -> None:
        result = asyncio.run(self.tool.run({"prompt": "x", "size": "wide"}, self.context))
        self.assertTrue(result.is_error)
        result = asyncio.run(self.tool.run({"prompt": "x", "count": 2, "save_path": "out.png"}, self.context))
        self.assertTrue(result.is_error)
        self.assertIn("目录", result.content)

    def test_access_flags_outside_paths(self) -> None:
        inside = self.tool.access({"prompt": "x"}, self.context)
        outside_directory = self.directories.workspace.parent / "pics"
        outside = self.tool.access({"prompt": "x", "save_path": str(outside_directory)}, self.context)
        self.assertEqual(inside.outside_paths, [])
        self.assertEqual([str(path) for path in outside.outside_paths], [str(outside_directory.resolve())])


class SearchImagesToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.context = ToolContext(workspace=self.directories.workspace)
        self.tool = SearchImagesTool()

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_formats_results(self) -> None:
        results = [
            {
                "title": "橘猫照片",
                "image_url": "https://a.example/1.png",
                "thumbnail_url": "https://thumb.example/1",
                "source_url": "https://a.example/page",
                "hostname": "a.example",
                "description": "一只橘猫",
            }
        ]

        async def fake_search(query, count):
            return results

        with patch("pocketagent.tools.image.search_bing_images", fake_search):
            result = asyncio.run(self.tool.run({"query": "橘猫"}, self.context))

        self.assertFalse(result.is_error)
        self.assertIn("找到 1 张图", result.content)
        self.assertIn("原图：https://a.example/1.png", result.content)
        self.assertIn("来源：https://a.example/page（a.example）", result.content)
        self.assertIn("download_image", result.content)

    def test_failures_become_error_results(self) -> None:
        async def failing(query, count):
            raise ImageRequestFailed("必应这次没给结果页（可能被限流了），稍后再试")

        with patch("pocketagent.tools.image.search_bing_images", failing):
            result = asyncio.run(self.tool.run({"query": "猫"}, self.context))
        self.assertTrue(result.is_error)


class DownloadImageToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.context = ToolContext(workspace=self.directories.workspace)
        self.tool = DownloadImageTool()

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_saves_with_url_stem_and_reports_markdown(self) -> None:
        async def fake_fetch(image_url):
            return ImagePayload(data=TINY_PNG, extension=".png")

        with patch("pocketagent.tools.image.fetch_image_bytes", fake_fetch):
            result = asyncio.run(self.tool.run({"url": "https://a.example/pics/cat.png"}, self.context))

        self.assertFalse(result.is_error)
        files = sorted((self.directories.workspace / "images").glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith("-cat.png"))
        self.assertIn(f"images/{files[0].name}", result.content)

    def test_save_path_as_file(self) -> None:
        async def fake_fetch(image_url):
            return ImagePayload(data=TINY_PNG, extension=".png")

        with patch("pocketagent.tools.image.fetch_image_bytes", fake_fetch):
            result = asyncio.run(self.tool.run({"url": "https://a.example/pics/x.png", "save_path": "assets/logo.png"}, self.context))

        self.assertFalse(result.is_error)
        self.assertTrue((self.directories.workspace / "assets" / "logo.png").is_file())

    def test_rejects_non_http_and_failures(self) -> None:
        result = asyncio.run(self.tool.run({"url": "ftp://a.example/x.png"}, self.context))
        self.assertTrue(result.is_error)

        async def failing(image_url):
            raise ImageRequestFailed("这个地址不是图片（text/html）：https://a.example/x")

        with patch("pocketagent.tools.image.fetch_image_bytes", failing):
            result = asyncio.run(self.tool.run({"url": "https://a.example/x"}, self.context))
        self.assertTrue(result.is_error)
        self.assertIn("不是图片", result.content)


if __name__ == "__main__":
    unittest.main()
