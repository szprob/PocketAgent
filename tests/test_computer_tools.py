"""computer use 工具的单元测试：按键拼脚本、PNG 头解析、图片进工具结果消息。

真的会截屏 / 点鼠标的路径不测（要权限、会动真屏幕）。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from pocketagent.attachments import AttachmentStore, tool_message_content
from pocketagent.tools import default_tool_registry
from pocketagent.tools.base import ToolContext, ToolResult
from pocketagent.tools.computer import (
    ComputerError,
    ComputerTool,
    ScreenshotTool,
    key_script,
    png_size,
)


class KeyScriptTest(unittest.TestCase):
    def test_plain_named_key(self) -> None:
        self.assertEqual(key_script("return"), 'tell application "System Events"\n\tkey code 36\nend tell')

    def test_single_char(self) -> None:
        self.assertIn('keystroke "a"', key_script("a"))

    def test_modifiers(self) -> None:
        self.assertIn('keystroke "t" using {command down, shift down}', key_script("cmd+shift+t"))

    def test_modifier_with_key_code(self) -> None:
        self.assertIn("key code 121 using {control down}", key_script("ctrl+pagedown"))

    def test_rejects_unknown(self) -> None:
        for bad in ("cmd+f13", "meta+c", ""):
            with self.assertRaises(ComputerError):
                key_script(bad)


class PngSizeTest(unittest.TestCase):
    def test_reads_header(self) -> None:
        width, height = 3024, 1964
        data = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
        self.assertEqual(png_size(data), (width, height))
        self.assertIsNone(png_size(b"not a png"))


def _context(tmp: Path) -> ToolContext:
    return ToolContext(workspace=tmp)


class ComputerToolValidationTest(unittest.TestCase):
    """参数校验路径：不真的碰键鼠。"""

    def test_click_requires_screenshot_scale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "click", "x": 100, "y": 200}, _context(Path(tmp))))
        self.assertTrue(result.is_error)
        self.assertIn("screenshot", result.content)

    def test_click_requires_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp))
            context.tool_state["screenshot_scale"] = 2.0
            result = asyncio.run(ComputerTool().run({"action": "click"}, context))
        self.assertTrue(result.is_error)
        self.assertIn("x", result.content)

    def test_unknown_action_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "scroll"}, _context(Path(tmp))))
        self.assertTrue(result.is_error)
        self.assertIn("scroll", result.content)

    def test_type_rejects_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "type", "text": ""}, _context(Path(tmp))))
        self.assertTrue(result.is_error)

    def test_key_rejects_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "key", "key": "  "}, _context(Path(tmp))))
        self.assertTrue(result.is_error)


class ToolResultImagesTest(unittest.TestCase):
    def test_default_empty(self) -> None:
        self.assertEqual(ToolResult("hi").images, [])


class ToolMessageContentTest(unittest.TestCase):
    def test_without_images_is_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(Path(tmp), "sess1")
            self.assertEqual(tool_message_content("你好", [], "screenshot", store), "你好")

    def test_with_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AttachmentStore(root, "sess1")
            image = root / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
            content = tool_message_content("已截屏", [image], "screenshot", store)
            self.assertIsInstance(content, list)
            self.assertEqual(content[0], {"type": "text", "text": "已截屏"})
            image_part = content[1]
            self.assertEqual(image_part["type"], "image_url")
            self.assertTrue(image_part["image_url"]["url"].startswith("pocketagent-attachment:sess1/"))
            self.assertIn("screenshot", image_part["_note"])
            # 图片被复制进了会话附件目录，materialize 才找得到
            attachment_id = image_part["image_url"]["url"].rsplit("/", 1)[1]
            self.assertTrue(store.get(attachment_id).path)

    def test_missing_file_keeps_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(Path(tmp), "sess1")
            content = tool_message_content("已截屏", [Path(tmp) / "nope.png"], "screenshot", store)
            self.assertEqual(content, "已截屏")


class ComputerUseSwitchTest(unittest.TestCase):
    def test_off_by_default(self) -> None:
        names = default_tool_registry().names()

        self.assertNotIn("screenshot", names)
        self.assertNotIn("computer", names)

    @unittest.skipUnless(sys.platform == "darwin", "computer use 只在 macOS 上注册")
    def test_registered_when_enabled_on_macos(self) -> None:
        names = default_tool_registry(computer_use=True).names()

        self.assertIn("screenshot", names)
        self.assertIn("computer", names)


class ScreenshotToolSchemaTest(unittest.TestCase):
    def test_read_kind_and_schema(self) -> None:
        tool = ScreenshotTool()
        self.assertEqual(tool.kind.name, "READ")
        self.assertEqual(tool.parameters, {"type": "object", "properties": {}})
