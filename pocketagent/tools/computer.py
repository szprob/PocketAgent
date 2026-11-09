"""computer use 工具（仅 macOS）：screenshot 截主屏给模型看，computer 操作键鼠、打开应用和网址。

- 截图走系统 screencapture（只截主屏），再缩到「一个截图像素 = 一个屏幕点」（长边超过 1440 再等比缩），
  模型给的坐标几乎不用换算，也不会被附件那边长边 2048 的缩图再改一次尺寸、让坐标对不上。
- 结果里写明前台应用和主屏上有哪些窗口：键盘输入和粘贴会进前台应用，模型要知道它是谁。
- computer 每次操作完等界面稳定一下，自动再截一张附在结果里，省掉单独截图的一来一回。
- 屏幕尺寸、窗口、鼠标、粘贴的系统接口见 macos_screen.py；按键还是走 System Events。

需要运行 PocketAgent 的应用有两个系统权限：「屏幕录制」（截图、读窗口标题）和「辅助功能」（键鼠）；
没授权时工具结果里给出设置路径。
"""

import asyncio
import datetime
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult
from pocketagent.tools.macos_screen import (
    ComputerError,
    ScreenInfo,
    mouse_action,
    paste_text,
    screen_info,
)

COMMAND_TIMEOUT_SECONDS = 15
SCREENSHOT_TIMEOUT_SECONDS = 15
# 发给模型的截图长边上限：再大模型也看不出更多细节，只是更费 token、更慢
MAX_SCREENSHOT_SIDE = 1440
# 操作完等多久再截图，让界面有时间反应（打开应用、网页要久一些）
SETTLE_SECONDS = {
    "move": 0.4,
    "type": 0.5,
    "key": 0.6,
    "open_app": 1.5,
    "open_url": 2.0,
}
DEFAULT_SETTLE_SECONDS = 0.6
DEFAULT_SCROLL_LINES = 5
SCROLL_DIRECTIONS = {"up": (1, 0), "down": (-1, 0), "left": (0, 1), "right": (0, -1)}
URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# tool_state 里记最近一张截图怎么换算到屏幕坐标
SCREEN_STATE_KEY = "screen"

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

__all__ = ["ComputerError", "ComputerTool", "ScreenshotTool", "key_script", "png_size"]


@dataclass
class ScreenMapping:
    """最近一张截图和屏幕的对应关系：截图像素 = 屏幕点 × scale。"""

    scale: float
    image_width: int
    image_height: int


def png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 头读宽高，不依赖 Pillow。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def permission_hint(message: str) -> str:
    lowered = message.lower()
    if "assistive" in lowered or "accessibility" in lowered or "not allowed" in lowered or "不允许" in message:
        return f"{message}\n{ACCESS_PERMISSION_HINT}"
    return message


def screenshot_size(screen_width: float, screen_height: float) -> tuple[int, int]:
    """发给模型的截图尺寸：一个像素对一个屏幕点，长边超过上限就等比缩小。"""
    longest = max(screen_width, screen_height)
    ratio = min(1.0, MAX_SCREENSHOT_SIDE / longest) if longest > 0 else 1.0
    return max(1, round(screen_width * ratio)), max(1, round(screen_height * ratio))


def describe_screen(info: ScreenInfo, mapping: ScreenMapping) -> str:
    """截图结果里的文字：尺寸、前台应用、主屏上的窗口（坐标换成截图像素）。"""
    lines = [
        f"主屏截图 {mapping.image_width}x{mapping.image_height} 像素：computer 的坐标直接写这张图里的像素，左上角是 (0, 0)。",
        f"前台应用：{info.frontmost_app or '未知'}（键盘输入和粘贴会进它最前面的窗口）",
    ]
    if info.windows:
        lines.append("主屏上的窗口（从前到后，坐标是截图像素）：")
        for window in info.windows:
            name = f"{window.app}「{window.title}」" if window.title else window.app
            left = max(0, round(window.x * mapping.scale))
            top = max(0, round(window.y * mapping.scale))
            right = min(mapping.image_width, round((window.x + window.width) * mapping.scale))
            bottom = min(mapping.image_height, round((window.y + window.height) * mapping.scale))
            lines.append(f"- {name}：x {left}–{right}，y {top}–{bottom}")
    front_on_primary = any(window.app == info.frontmost_app for window in info.windows)
    if info.frontmost_app and info.windows and not front_on_primary:
        lines.append(
            f"注意：前台应用 {info.frontmost_app} 在主屏上没有窗口（可能在另一块屏幕上），现在输入会进你看不到的窗口；"
            "先点一下主屏上的目标窗口，或者用 open_app 切到要用的应用。"
        )
    if info.display_count > 1:
        lines.append(f"还接了另外 {info.display_count - 1} 块屏幕：截图和点击只覆盖主屏。")
    return "\n".join(lines)


async def _run_command(
    command: list[str], timeout: float = COMMAND_TIMEOUT_SECONDS, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(command, input=stdin, capture_output=True, timeout=timeout)

    try:
        return await asyncio.to_thread(run)
    except subprocess.TimeoutExpired as error:
        raise ComputerError(f"{command[0]} 执行超时") from error


async def run_applescript(script: str) -> str:
    """跑一段 AppleScript，返回 stdout；失败抛 ComputerError。"""
    completed = await _run_command(["osascript", "-"], stdin=script.encode("utf-8"))
    if completed.returncode != 0:
        raise ComputerError(completed.stderr.decode("utf-8", "replace").strip() or "AppleScript 执行失败")
    return completed.stdout.decode("utf-8", "replace").strip()


async def _resize_png(source: Path, target: Path, size: tuple[int, int]) -> bool:
    """用系统自带的 sips 缩图；失败返回 False，调用方退回用原图。"""
    width, height = size
    try:
        completed = await _run_command(["sips", "-z", str(height), str(width), str(source), "--out", str(target)])
    except (ComputerError, OSError):
        return False
    return completed.returncode == 0 and target.is_file() and target.stat().st_size > 0


async def capture_screen(context: ToolContext) -> ToolResult:
    """截主屏、缩到合适尺寸、记下换算比，返回带图的结果。"""
    directory = context.tool_outputs_dir or context.workspace
    directory.mkdir(parents=True, exist_ok=True)
    # 操作完自动截图时一秒内会截好几张，文件名带上微秒
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    raw_path = directory / f"screenshot-{stamp}-full.png"
    path = directory / f"screenshot-{stamp}.png"
    try:
        completed = await _run_command(["screencapture", "-x", "-m", "-t", "png", str(raw_path)], SCREENSHOT_TIMEOUT_SECONDS)
    except (ComputerError, OSError) as error:
        return ToolResult(f"截图失败：{error}", is_error=True)
    if completed.returncode != 0 or not raw_path.is_file() or raw_path.stat().st_size == 0:
        return ToolResult(f"截图失败。{SCREEN_PERMISSION_HINT}", is_error=True)
    raw_size = png_size(raw_path.read_bytes())
    if raw_size is None:
        return ToolResult(f"截图失败：{raw_path} 不是有效的 PNG。{SCREEN_PERMISSION_HINT}", is_error=True)

    try:
        info = await screen_info()
    except ComputerError as error:
        context.tool_state.pop(SCREEN_STATE_KEY, None)
        raw_path.rename(path)
        return ToolResult(
            f"已截取主屏幕（{raw_size[0]}x{raw_size[1]} 像素），但读不到屏幕尺寸（{error}），点击坐标没法换算；先重试截图",
            images=[path],
        )

    target_size = screenshot_size(info.width, info.height)
    if raw_size == target_size:
        raw_path.rename(path)
        image_size = raw_size
    elif await _resize_png(raw_path, path, target_size):
        raw_path.unlink(missing_ok=True)
        image_size = target_size
    else:
        raw_path.rename(path)
        image_size = raw_size
    mapping = ScreenMapping(scale=image_size[0] / info.width, image_width=image_size[0], image_height=image_size[1])
    context.tool_state[SCREEN_STATE_KEY] = mapping
    return ToolResult(describe_screen(info, mapping), images=[path])


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
        action = f'keystroke "{escape_applescript(base)}"'
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
        "截取主屏幕，截图会直接出现在结果里，你能看到屏幕内容；结果里还写着前台应用和主屏上有哪些窗口。"
        "要操作 GUI 时先截一张看清状态，再用 computer 操作（computer 每次操作完会自动再截一张，不用再单独截图）。"
    )
    parameters = {"type": "object", "properties": {}}

    def describe(self, arguments: dict[str, Any]) -> str:
        return "截屏"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await capture_screen(context)


class ComputerTool(Tool):
    name = "computer"
    kind = ToolKind.EXECUTE
    description = (
        "操作本机 macOS：点击、拖拽、滚动、输入文字、按键，打开应用和网址。"
        "坐标用最近一次截图里的像素坐标（左上角是 0,0）；每次操作完会自动附上一张新截图和前台窗口信息，看它确认结果。"
        "能不用坐标就不用：打开网页用 open_url，启动或切换应用用 open_app。"
        "输入文字走剪贴板粘贴，中文也能输；type / key 会进前台应用，前台不对就带上 app 参数先把它切到前台。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "click", "double_click", "right_click", "move", "drag", "scroll",
                    "type", "key", "open_app", "open_url",
                ],
                "description": (
                    "click 单击 / double_click 双击 / right_click 右键 / move 只移动鼠标（悬停）/ "
                    "drag 从 (x, y) 拖到 (to_x, to_y) / scroll 在 (x, y) 处滚动 / type 输入文字 / key 按键或组合键 / "
                    "open_app 启动或切到某个应用 / open_url 用默认程序打开网址"
                ),
            },
            "x": {"type": "integer", "description": "横坐标（截图像素）"},
            "y": {"type": "integer", "description": "纵坐标（截图像素）"},
            "to_x": {"type": "integer", "description": "drag 的终点横坐标（截图像素）"},
            "to_y": {"type": "integer", "description": "drag 的终点纵坐标（截图像素）"},
            "direction": {"type": "string", "enum": list(SCROLL_DIRECTIONS), "description": "scroll 的方向"},
            "amount": {"type": "integer", "description": f"scroll 滚几行，默认 {DEFAULT_SCROLL_LINES}"},
            "text": {"type": "string", "description": "action=type 时要输入的文字（粘贴进去，换行照原样）"},
            "key": {
                "type": "string",
                "description": (
                    "action=key 时按什么键：单个键如 return、tab、escape、delete、up、down、"
                    "pageup、pagedown、f1…f12，或组合键如 cmd+c、cmd+shift+t"
                ),
            },
            "app": {
                "type": "string",
                "description": "应用名，如 Google Chrome、微信、Finder。open_app 必填；type / key 带上时先把它切到前台",
            },
            "url": {"type": "string", "description": "action=open_url 时的网址"},
        },
        "required": ["action"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        action = str(arguments.get("action", ""))
        if action in ("click", "double_click", "right_click", "move"):
            return f"{action} ({arguments.get('x')}, {arguments.get('y')})"
        if action == "drag":
            return f"drag ({arguments.get('x')}, {arguments.get('y')}) → ({arguments.get('to_x')}, {arguments.get('to_y')})"
        if action == "scroll":
            return f"scroll {arguments.get('direction', 'down')} {arguments.get('amount') or DEFAULT_SCROLL_LINES} 行"
        if action == "type":
            text = str(arguments.get("text", ""))
            return f'type "{text[:40]}"' if len(text) <= 40 else f'type "{text[:37]}..."'
        if action == "key":
            return f"key {arguments.get('key', '')}"
        if action == "open_app":
            return f"打开应用 {arguments.get('app', '')}"
        if action == "open_url":
            return f"打开网址 {arguments.get('url', '')}"
        return action

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip()
        try:
            await self._perform(action, arguments, context)
        except ComputerError as error:
            return ToolResult(permission_hint(str(error)), is_error=True)
        await asyncio.sleep(SETTLE_SECONDS.get(action, DEFAULT_SETTLE_SECONDS))
        screen = await capture_screen(context)
        if screen.is_error:
            return ToolResult(f"{self.describe(arguments)} 完成；之后自动截图失败：{screen.content}")
        return ToolResult(f"{self.describe(arguments)} 完成。下面是操作后的屏幕。\n{screen.content}", images=screen.images)

    async def _perform(self, action: str, arguments: dict[str, Any], context: ToolContext) -> None:
        if action in ("click", "double_click", "right_click", "move"):
            x, y = _screen_point(arguments, "x", "y", context)
            await mouse_action(action, x, y)
        elif action == "drag":
            x, y = _screen_point(arguments, "x", "y", context)
            to_x, to_y = _screen_point(arguments, "to_x", "to_y", context)
            await mouse_action("drag", x, y, to_x, to_y)
        elif action == "scroll":
            await self._scroll(arguments, context)
        elif action == "type":
            text = arguments.get("text")
            if not isinstance(text, str) or not text:
                raise ComputerError("type 需要非空的 text")
            await _activate_if_asked(arguments)
            await paste_text(text)
        elif action == "key":
            key = arguments.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ComputerError("key 需要按键描述，如 cmd+c 或 return")
            script = key_script(key)
            await _activate_if_asked(arguments)
            await run_applescript(script)
        elif action == "open_app":
            app = str(arguments.get("app") or "").strip()
            if not app:
                raise ComputerError("open_app 需要 app（应用名）")
            await _open(["open", "-a", app], f"打不开应用 {app}")
        elif action == "open_url":
            url = str(arguments.get("url") or "").strip()
            if not URL_SCHEME.match(url):
                raise ComputerError("open_url 需要带协议的网址，如 https://example.com")
            await _open(["open", url], f"打不开网址 {url}")
        else:
            raise ComputerError(
                f"未知 action：{action}；支持 click、double_click、right_click、move、drag、scroll、type、key、open_app、open_url"
            )

    async def _scroll(self, arguments: dict[str, Any], context: ToolContext) -> None:
        direction = str(arguments.get("direction") or "down")
        if direction not in SCROLL_DIRECTIONS:
            raise ComputerError("scroll 的 direction 只能是 up、down、left、right")
        x, y = _screen_point(arguments, "x", "y", context)
        lines = min(max(int(arguments.get("amount") or DEFAULT_SCROLL_LINES), 1), 50)
        vertical_sign, horizontal_sign = SCROLL_DIRECTIONS[direction]
        await mouse_action("scroll", x, y, vertical_sign * lines, horizontal_sign * lines)


def _screen_point(arguments: dict[str, Any], x_name: str, y_name: str, context: ToolContext) -> tuple[float, float]:
    """截图像素坐标换成屏幕坐标（点）。必须按模型看过的那张截图换算，没截过图就先去截。"""
    mapping = context.tool_state.get(SCREEN_STATE_KEY)
    if not isinstance(mapping, ScreenMapping):
        raise ComputerError("还没有可用的截图：先调 screenshot 截一张，再用它画面里的像素坐标")
    raw_x, raw_y = arguments.get(x_name), arguments.get(y_name)
    if not isinstance(raw_x, (int, float)) or not isinstance(raw_y, (int, float)):
        raise ComputerError(f"需要截图里的像素坐标 {x_name} 和 {y_name}")
    if not (0 <= raw_x < mapping.image_width and 0 <= raw_y < mapping.image_height):
        raise ComputerError(
            f"坐标 ({raw_x}, {raw_y}) 超出了截图范围（{mapping.image_width}x{mapping.image_height}）；"
            "坐标要用最近一张截图里的像素"
        )
    return float(raw_x) / mapping.scale, float(raw_y) / mapping.scale


async def _activate_if_asked(arguments: dict[str, Any]) -> None:
    """type / key 带了 app 时先把它切到前台，等它接住焦点。"""
    app = str(arguments.get("app") or "").strip()
    if app:
        await _open(["open", "-a", app], f"切不到应用 {app}")
        await asyncio.sleep(0.4)


async def _open(command: list[str], failure: str) -> None:
    try:
        completed = await _run_command(command)
    except OSError as error:
        raise ComputerError(f"{failure}：{error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ComputerError(f"{failure}：{detail or '原因未知'}")
