"""computer use 工具（仅 macOS）：screenshot 截屏给模型看，computer 操作键鼠。

截图走系统 screencapture，键鼠走 AppleScript（System Events 的 click at / keystroke / key code），
零第三方依赖。需要运行 PocketAgent 的应用有两个系统权限：「屏幕录制」（截图）和
「辅助功能」（键鼠）；没授权时工具结果里给出设置路径。

模型看到的是截图（像素），但系统要的是屏幕坐标（点，Retina 屏是截图的一半），
所以 computer 的 x/y 直接用截图像素坐标，执行时按当时的缩放比换算。
"""

import asyncio
import datetime
import re
import subprocess
from pathlib import Path
from typing import Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

OSA_TIMEOUT_SECONDS = 15
SCREENSHOT_TIMEOUT_SECONDS = 15

# 键名 → System Events 的 key code
KEY_CODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "forward_delete": 117, "up": 126, "down": 125, "left": 123,
    "right": 124, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
# 组合键里的修饰键写法 → AppleScript 的修饰键名（fn 发不出去，不支持）
MODIFIER_NAMES = {
    "cmd": "command", "command": "command",
    "ctrl": "control", "control": "control",
    "alt": "option", "option": "option",
    "shift": "shift",
}

SCREEN_PERMISSION_HINT = (
    "没有「屏幕录制」权限：打开 系统设置 → 隐私与安全性 → 屏幕与窗口录制，"
    "给运行 PocketAgent 的应用打开开关，然后重启应用再试"
)
ACCESS_PERMISSION_HINT = (
    "没有「辅助功能」权限：打开 系统设置 → 隐私与安全性 → 辅助功能，"
    "给运行 PocketAgent 的应用打开开关"
)


class ComputerError(Exception):
    """截图 / 键鼠操作没成功；消息直接当工具结果给模型看。"""


def png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 头读宽高，不依赖 Pillow。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def permission_hint(message: str) -> str:
    lowered = message.lower()
    if "assistive" in lowered or "accessibility" in lowered or "not allowed" in lowered:
        return f"{message}\n{ACCESS_PERMISSION_HINT}"
    return message


async def run_applescript(script: str) -> str:
    """跑一段 AppleScript，返回 stdout；失败抛 ComputerError。"""

    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["osascript", "-"],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=OSA_TIMEOUT_SECONDS,
        )

    try:
        completed = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired as error:
        raise ComputerError("AppleScript 执行超时") from error
    if completed.returncode != 0:
        raise ComputerError(completed.stderr.decode("utf-8", "replace").strip() or "AppleScript 执行失败")
    return completed.stdout.decode("utf-8", "replace").strip()


async def screen_logical_size() -> tuple[int, int] | None:
    """主屏幕的逻辑分辨率（点）。Finder 的桌面窗口盖住主屏，拿它的宽高。"""
    try:
        bounds = await run_applescript('tell application "Finder" to get bounds of window of desktop')
        numbers = [int(value) for value in re.findall(r"-?\d+", bounds)]
    except (ComputerError, ValueError):
        return None
    if len(numbers) != 4:
        return None
    return numbers[2] - numbers[0], numbers[3] - numbers[1]


def key_script(key: str) -> str:
    """把 "cmd+shift+t" / "return" 这样的按键描述拼成 AppleScript。"""
    parts = [part.strip().lower() for part in key.split("+") if part.strip()]
    if not parts:
        raise ComputerError("key 不能为空")
    modifiers = []
    for part in parts[:-1]:
        if part not in MODIFIER_NAMES:
            raise ComputerError(f"不认识的修饰键 {part}；支持：cmd、ctrl、alt、shift")
        modifiers.append(MODIFIER_NAMES[part])
    base = parts[-1]
    if base in KEY_CODES:
        action = f"key code {KEY_CODES[base]}"
    elif len(base) == 1:
        action = f'keystroke "{base}"'
    else:
        raise ComputerError(f"不认识的按键 {base}；支持：{'、'.join(sorted(KEY_CODES))}，或单个字符")
    if modifiers:
        using = ", ".join(f"{name} down" for name in modifiers)
        action += f" using {{{using}}}"
    return f'tell application "System Events"\n\t{action}\nend tell'


def escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class ScreenshotTool(Tool):
    name = "screenshot"
    kind = ToolKind.READ
    description = (
        "截取主屏幕，截图会直接出现在结果里，你能看到屏幕内容。"
        "要操作 GUI 时先截一张看清状态，再用 computer 点击 / 输入，做完再截一张确认结果。"
    )
    parameters = {"type": "object", "properties": {}}

    def describe(self, arguments: dict[str, Any]) -> str:
        return "截屏"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        directory = context.tool_outputs_dir or context.workspace
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"screenshot-{stamp}.png"

        def capture() -> int:
            return subprocess.run(
                ["screencapture", "-x", "-t", "png", str(path)],
                capture_output=True,
                timeout=SCREENSHOT_TIMEOUT_SECONDS,
            ).returncode

        try:
            returncode = await asyncio.to_thread(capture)
        except subprocess.TimeoutExpired as error:
            raise ComputerError("截图命令超时") from error
        if returncode != 0 or not path.is_file() or path.stat().st_size == 0:
            return ToolResult(f"截图失败。{SCREEN_PERMISSION_HINT}", is_error=True)
        size = png_size(path.read_bytes())
        if size is None:
            return ToolResult(f"截图失败：{path} 不是有效的 PNG。{SCREEN_PERMISSION_HINT}", is_error=True)
        width, height = size
        lines = [f"已截取主屏幕（{width}x{height} 像素）："]
        logical = await screen_logical_size()
        if logical is not None:
            scale = width / logical[0]
            context.tool_state["screenshot_scale"] = scale
            lines.append(f"屏幕逻辑分辨率 {logical[0]}x{logical[1]} 点，缩放比 {scale:g}；"
                         "computer 的 x/y 直接写这张截图里的像素坐标，会自动换算")
        else:
            lines.append("拿不到屏幕逻辑分辨率，点击坐标可能不准，先重试一次")
        return ToolResult("\n".join(lines), images=[path])


class ComputerTool(Tool):
    name = "computer"
    kind = ToolKind.EXECUTE
    description = (
        "操作本机 macOS 的键盘鼠标：点击、输入文字、按键。坐标用 screenshot 截图里的像素坐标"
        "（会自动换算成屏幕坐标），所以操作前先 screenshot 看清屏幕，操作完再截一张确认。"
        "右键和拖拽不支持。每次操作都要用户所在环境有「辅助功能」权限。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["click", "double_click", "type", "key"],
                "description": "click 单击，double_click 双击（同一坐标连点两次），type 输入文字，key 按键 / 组合键",
            },
            "x": {"type": "integer", "description": "点击位置的横坐标（截图像素）"},
            "y": {"type": "integer", "description": "点击位置的纵坐标（截图像素）"},
            "text": {"type": "string", "description": "action=type 时要输入的文字，换行会被当成回车"},
            "key": {
                "type": "string",
                "description": (
                    "action=key 时按什么键：单个键如 return、tab、escape、delete、up、down、"
                    "pageup、pagedown、f1…f12，或组合键如 cmd+c、cmd+shift+t"
                ),
            },
        },
        "required": ["action"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        action = str(arguments.get("action", ""))
        if action in ("click", "double_click"):
            return f"{action} ({arguments.get('x')}, {arguments.get('y')})"
        if action == "type":
            text = str(arguments.get("text", ""))
            return f'type "{text[:40]}"' if len(text) <= 40 else f'type "{text[:37]}..."'
        if action == "key":
            return f"key {arguments.get('key', '')}"
        return action

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip()
        try:
            if action == "click":
                await self._click(arguments, context, times=1)
            elif action == "double_click":
                await self._click(arguments, context, times=2)
            elif action == "type":
                await self._type(arguments)
            elif action == "key":
                await self._key(arguments)
            else:
                return ToolResult(f"未知 action：{action}；支持 click、double_click、type、key", is_error=True)
        except ComputerError as error:
            return ToolResult(permission_hint(str(error)), is_error=True)
        return ToolResult(f"{self.describe(arguments)} 完成")

    async def _click(self, arguments: dict[str, Any], context: ToolContext, times: int) -> None:
        raw_x, raw_y = arguments.get("x"), arguments.get("y")
        if not isinstance(raw_x, (int, float)) or not isinstance(raw_y, (int, float)):
            raise ComputerError("click / double_click 需要截图里的像素坐标 x 和 y")
        # 坐标必须按模型看过的那张截图换算；换算比由 screenshot 记在共享状态里，没截过图就先去截
        scale = context.tool_state.get("screenshot_scale")
        if not scale or scale <= 0:
            raise ComputerError("还没有可用的截图换算比：先调 screenshot 截一张，再用它画面里的像素坐标点击")
        x = int(round(float(raw_x) / scale))
        y = int(round(float(raw_y) / scale))
        clicks = "\n".join([f"\tclick at {{{x}, {y}}}"] * times)
        await run_applescript(f'tell application "System Events"\n{clicks}\nend tell')

    async def _type(self, arguments: dict[str, Any]) -> None:
        text = arguments.get("text")
        if not isinstance(text, str) or not text:
            raise ComputerError("type 需要非空的 text")
        await run_applescript(f'tell application "System Events" to keystroke "{escape_applescript(text)}"')

    async def _key(self, arguments: dict[str, Any]) -> None:
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ComputerError("key 需要按键描述，如 cmd+c 或 return")
        await run_applescript(key_script(key))
