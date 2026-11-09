"""computer use 工具的单元测试：按键拼脚本、PNG 头解析、坐标换算、截图尺寸、图片进工具结果消息、旧截图省略。

真的会截屏 / 点鼠标的路径不测（要权限、会动真屏幕）。
"""

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pocketagent.core.nodes import ToolCallOutcome, append_tool_messages
from pocketagent.interaction.attachments import AttachmentStore, omit_old_tool_images, tool_message_content
from pocketagent.tools import default_tool_registry
from pocketagent.tools.base import ToolContext, ToolResult
from pocketagent.tools.computer import (
    SCREEN_STATE_KEY,
    ComputerError,
    ComputerTool,
    ScreenMapping,
    ScreenshotTool,
    capture_screen,
    describe_screen,
    key_script,
    png_size,
    screenshot_size,
)
from pocketagent.tools.macos_screen import ScreenInfo, WindowInfo
from tests.helpers import TemporaryDirectories, build_harness, tool_call


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


def _png(width: int, height: int) -> bytes:
    """只有 PNG 头的假图：png_size 读得出宽高就够了。"""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


def _screen(display_count: int = 1, frontmost: str = "Google Chrome", windows: list[WindowInfo] | None = None) -> ScreenInfo:
    if windows is None:
        windows = [WindowInfo(app="Google Chrome", title="实验列表", x=0, y=30, width=1280, height=708)]
    return ScreenInfo(
        width=1280, height=800, backing_scale=2.0, display_count=display_count, frontmost_app=frontmost, windows=windows
    )


class ComputerToolValidationTest(unittest.TestCase):
    """参数校验路径：不真的碰键鼠。"""

    def test_click_requires_a_screenshot_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "click", "x": 100, "y": 200}, _context(Path(tmp))))
        self.assertTrue(result.is_error)
        self.assertIn("screenshot", result.content)

    def test_click_requires_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp))
            context.tool_state[SCREEN_STATE_KEY] = ScreenMapping(scale=1.0, image_width=1280, image_height=800)
            result = asyncio.run(ComputerTool().run({"action": "click"}, context))
        self.assertTrue(result.is_error)
        self.assertIn("x", result.content)

    def test_coordinates_outside_the_screenshot_are_rejected(self) -> None:
        """模型拿错了尺寸（比如按原始分辨率 2560 报坐标）时直接告诉它，别点到屏幕外或别的屏上去。"""
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(Path(tmp))
            context.tool_state[SCREEN_STATE_KEY] = ScreenMapping(scale=1.0, image_width=1280, image_height=800)
            result = asyncio.run(ComputerTool().run({"action": "click", "x": 2000, "y": 300}, context))
        self.assertTrue(result.is_error)
        self.assertIn("1280x800", result.content)

    def test_unknown_action_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "fly"}, _context(Path(tmp))))
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

    def test_open_url_needs_a_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(ComputerTool().run({"action": "open_url", "url": "www.example.com"}, _context(Path(tmp))))
        self.assertTrue(result.is_error)
        self.assertIn("https://", result.content)


class ComputerToolActionTest(unittest.IsolatedAsyncioTestCase):
    """动作怎么换算、按什么顺序发；真正发事件和截图的函数换成假的。"""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.context = _context(Path(self.tmp.name))
        self.calls: list[tuple] = []
        shot = Path(self.tmp.name) / "after.png"
        shot.write_bytes(_png(1440, 900))

        async def fake_mouse(action, x, y, *extra):
            self.calls.append(("mouse", action, x, y, *extra))

        async def fake_paste(text):
            self.calls.append(("paste", text))

        async def fake_open(command, failure):
            self.calls.append(("open", *command))

        async def fake_capture(context):
            self.calls.append(("capture",))
            return ToolResult("主屏截图 1440x900 像素", images=[shot])

        for name, fake in [
            ("mouse_action", fake_mouse),
            ("paste_text", fake_paste),
            ("_open", fake_open),
            ("capture_screen", fake_capture),
        ]:
            patcher = mock.patch(f"pocketagent.tools.computer.{name}", fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch("pocketagent.tools.computer.asyncio.sleep", mock.AsyncMock())
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    async def test_click_converts_screenshot_pixels_to_screen_points_and_returns_a_new_screenshot(self) -> None:
        # 5K 屏：2880x1800 点缩成 1440x900 的截图，截图像素 = 点 × 0.5
        self.context.tool_state[SCREEN_STATE_KEY] = ScreenMapping(scale=0.5, image_width=1440, image_height=900)

        result = await ComputerTool().run({"action": "click", "x": 720, "y": 200}, self.context)

        self.assertFalse(result.is_error)
        self.assertEqual(self.calls, [("mouse", "click", 1440.0, 400.0), ("capture",)])
        self.assertIn("完成", result.content)
        self.assertIn("1440x900", result.content)
        self.assertEqual(len(result.images), 1)

    async def test_scroll_and_drag(self) -> None:
        self.context.tool_state[SCREEN_STATE_KEY] = ScreenMapping(scale=1.0, image_width=1280, image_height=800)

        await ComputerTool().run({"action": "scroll", "x": 600, "y": 400, "direction": "down", "amount": 3}, self.context)
        await ComputerTool().run({"action": "drag", "x": 10, "y": 20, "to_x": 300, "to_y": 40}, self.context)

        self.assertEqual(self.calls[0], ("mouse", "scroll", 600.0, 400.0, -3, 0))
        self.assertEqual(self.calls[2], ("mouse", "drag", 10.0, 20.0, 300.0, 40.0))

    async def test_type_switches_to_the_app_first_then_pastes(self) -> None:
        result = await ComputerTool().run({"action": "type", "text": "正大广场 停车", "app": "Google Chrome"}, self.context)

        self.assertFalse(result.is_error)
        self.assertEqual(
            self.calls, [("open", "open", "-a", "Google Chrome"), ("paste", "正大广场 停车"), ("capture",)]
        )

    async def test_open_url_uses_the_default_handler(self) -> None:
        await ComputerTool().run({"action": "open_url", "url": "https://example.com/a"}, self.context)

        self.assertEqual(self.calls[0], ("open", "open", "https://example.com/a"))


class CaptureScreenTest(unittest.IsolatedAsyncioTestCase):
    """截图尺寸和换算比：以前用 Finder 桌面窗口量屏幕，两块屏时量成拼起来的 3200 宽，换算比错成 0.8。"""

    async def test_retina_screenshot_is_scaled_to_one_pixel_per_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = ToolContext(workspace=Path(tmp), tool_outputs_dir=Path(tmp))

            async def fake_command(command, timeout=15, stdin=None):
                Path(command[-1]).write_bytes(_png(2560, 1600))
                return subprocess.CompletedProcess(command, 0, b"", b"")

            async def fake_resize(source, target, size):
                target.write_bytes(_png(*size))
                return True

            async def fake_info():
                return _screen(display_count=2)

            with mock.patch("pocketagent.tools.computer._run_command", fake_command), mock.patch(
                "pocketagent.tools.computer._resize_png", fake_resize
            ), mock.patch("pocketagent.tools.computer.screen_info", fake_info):
                result = await capture_screen(context)

            self.assertFalse(result.is_error)
            mapping = context.tool_state[SCREEN_STATE_KEY]
            self.assertEqual((mapping.scale, mapping.image_width, mapping.image_height), (1.0, 1280, 800))
            self.assertEqual(png_size(result.images[0].read_bytes()), (1280, 800))
            self.assertIn("1280x800", result.content)
            self.assertIn("另外 1 块屏幕", result.content)
            # 原始大图不留在输出目录里
            self.assertEqual(len(list(Path(tmp).glob("*-full.png"))), 0)


class ScreenDescriptionTest(unittest.TestCase):
    def test_screenshot_size_caps_the_long_side(self) -> None:
        self.assertEqual(screenshot_size(1280, 800), (1280, 800))
        self.assertEqual(screenshot_size(2560, 1440), (1440, 810))

    def test_lists_windows_in_screenshot_pixels(self) -> None:
        mapping = ScreenMapping(scale=0.5, image_width=640, image_height=400)

        text = describe_screen(_screen(), mapping)

        self.assertIn("前台应用：Google Chrome", text)
        self.assertIn("Google Chrome「实验列表」：x 0–640，y 15–369", text)
        self.assertNotIn("另外", text)

    def test_warns_when_the_frontmost_app_is_not_on_the_main_screen(self) -> None:
        mapping = ScreenMapping(scale=1.0, image_width=1280, image_height=800)

        text = describe_screen(_screen(display_count=2, frontmost="微信"), mapping)

        self.assertIn("前台应用 微信 在主屏上没有窗口", text)


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


class ToolImagesInHistoryTest(unittest.IsolatedAsyncioTestCase):
    """带图的工具结果要真的进历史。只测 tool_message_content 不够：
    append_tool_messages 把算好的 content 丢掉时，那种测试照样是绿的。"""

    async def test_image_part_is_written_into_the_tool_message(self) -> None:
        directories = TemporaryDirectories()
        self.addCleanup(directories.cleanup)
        harness = build_harness(directories, [])
        image = directories.workspace / "shot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
        outcome = ToolCallOutcome(
            call=tool_call("call_1", "screenshot", "{}"),
            result=ToolResult("已截屏", images=[image]),
        )

        append_tool_messages(harness.agent.context, [outcome])

        message = harness.agent.context.messages[-1]
        self.assertEqual(message["role"], "tool")
        self.assertIsInstance(message["content"], list)
        self.assertEqual(message["content"][0], {"type": "text", "text": "已截屏"})
        self.assertEqual(message["content"][1]["type"], "image_url")


class OmitOldToolImagesTest(unittest.TestCase):
    """历史里只给模型看最近几张截图，按批省略，用户自己附的图不动。"""

    @staticmethod
    def _shot(index: int) -> dict:
        return {
            "role": "tool",
            "tool_call_id": f"call_{index}",
            "content": [
                {"type": "text", "text": f"第 {index} 张"},
                {"type": "image_url", "image_url": {"url": f"pocketagent-attachment:s/{index}"}, "_name": f"shot{index}.png"},
            ],
        }

    def _history(self, count: int) -> list[dict]:
        user_image = {"type": "image_url", "image_url": {"url": "pocketagent-attachment:s/user"}, "_name": "mine.png"}
        return [{"role": "user", "content": [{"type": "text", "text": "看图"}, user_image]}] + [
            self._shot(index) for index in range(count)
        ]

    @staticmethod
    def _image_count(messages: list[dict]) -> int:
        return sum(
            1
            for message in messages
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        )

    def test_keeps_everything_until_a_full_batch_is_surplus(self) -> None:
        messages = self._history(5)

        self.assertIs(omit_old_tool_images(messages), messages)

    def test_omits_the_oldest_batch_and_leaves_user_images(self) -> None:
        messages = self._history(7)

        result = omit_old_tool_images(messages)

        # 7 张截图多出 4 张，省掉最早的 3 张（整批），剩 4 张截图 + 用户的 1 张
        self.assertEqual(self._image_count(result), 5)
        self.assertEqual(result[1]["content"][1]["type"], "text")
        self.assertIn("shot0.png", result[1]["content"][1]["text"])
        self.assertEqual(result[4]["content"][1]["type"], "image_url")
        self.assertEqual(result[0]["content"][1]["type"], "image_url")
        # 原消息不改
        self.assertEqual(self._image_count(messages), 8)
