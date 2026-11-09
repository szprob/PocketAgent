"""computer use 用到的 macOS 系统接口：主屏尺寸、前台应用和窗口、鼠标事件、粘贴文字。

都用 JXA（osascript -l JavaScript）调系统框架，零第三方依赖，也不需要「自动化」权限：
- 主屏尺寸读 NSScreen。以前量 Finder 桌面窗口，接了多块屏时量到的是所有屏拼起来的大小，换算比算错，点击全偏到别处。
- 鼠标用 CGEvent 直接发事件。System Events 的 click at 要等目标应用响应辅助功能请求，会卡到超时。
- 文字用剪贴板粘贴。keystroke 打不了中文，开着中文输入法时英文也会被输入法吞掉；粘完把剪贴板恢复原样。
发事件要「辅助功能」权限，读其他应用的窗口标题要「屏幕录制」权限（没有时标题是空的，只显示应用名）。
"""

import asyncio
import json
import subprocess
from dataclasses import dataclass

JXA_TIMEOUT_SECONDS = 15
# 窗口列表最多列几个；再多模型也用不上
MAX_LISTED_WINDOWS = 8
# 太小的窗口（状态栏小部件、悬浮按钮）不列
MIN_WINDOW_SIDE_POINTS = 40


class ComputerError(Exception):
    """截图 / 键鼠操作没成功；消息直接当工具结果给模型看。"""


@dataclass
class WindowInfo:
    app: str
    title: str
    # 屏幕坐标（点），原点在主屏左上角
    x: float
    y: float
    width: float
    height: float


@dataclass
class ScreenInfo:
    # 主屏（有菜单栏的那块，screencapture -m 截的就是它）的大小，单位是点
    width: float
    height: float
    # 一个点等于几个物理像素（Retina 屏是 2）
    backing_scale: float
    display_count: int
    frontmost_app: str
    # 主屏上看得见的普通窗口，从前到后
    windows: list[WindowInfo]


async def run_jxa(script: str, *arguments: str) -> str:
    """跑一段 JXA，arguments 传给脚本的 run(argv)；返回 stdout，失败抛 ComputerError。"""

    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["osascript", "-l", "JavaScript", "-", *arguments],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=JXA_TIMEOUT_SECONDS,
        )

    try:
        completed = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired as error:
        raise ComputerError("系统脚本执行超时") from error
    if completed.returncode != 0:
        raise ComputerError(completed.stderr.decode("utf-8", "replace").strip() or "系统脚本执行失败")
    return completed.stdout.decode("utf-8", "replace").strip()


SCREEN_INFO_SCRIPT = """
ObjC.import("AppKit");
ObjC.import("CoreGraphics");
function run(argv) {
  var maxWindows = Number(argv[0]), minSide = Number(argv[1]);
  var screens = $.NSScreen.screens;
  var primary = screens.objectAtIndex(0);
  var width = primary.frame.size.width, height = primary.frame.size.height;
  var front = $.NSWorkspace.sharedWorkspace.frontmostApplication;
  var options = $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements;
  var all = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(options, $.kCGNullWindowID))) || [];
  var windows = [];
  for (var i = 0; i < all.length && windows.length < maxWindows; i++) {
    var item = all[i], bounds = item.kCGWindowBounds;
    if (item.kCGWindowLayer !== 0 || item.kCGWindowAlpha === 0 || !bounds) continue;
    if (bounds.Width < minSide || bounds.Height < minSide) continue;
    var onPrimary = bounds.X < width && bounds.X + bounds.Width > 0 && bounds.Y < height && bounds.Y + bounds.Height > 0;
    if (!onPrimary) continue;
    windows.push({app: item.kCGWindowOwnerName || "", title: item.kCGWindowName || "",
                  x: bounds.X, y: bounds.Y, width: bounds.Width, height: bounds.Height});
  }
  return JSON.stringify({
    width: width, height: height, backing_scale: primary.backingScaleFactor, display_count: screens.count,
    frontmost_app: front ? ObjC.unwrap(front.localizedName) : "", windows: windows
  });
}
"""


async def screen_info() -> ScreenInfo:
    output = await run_jxa(SCREEN_INFO_SCRIPT, str(MAX_LISTED_WINDOWS), str(MIN_WINDOW_SIDE_POINTS))
    try:
        data = json.loads(output)
        windows = [
            WindowInfo(
                app=str(item.get("app") or ""),
                title=str(item.get("title") or ""),
                x=float(item["x"]),
                y=float(item["y"]),
                width=float(item["width"]),
                height=float(item["height"]),
            )
            for item in data.get("windows") or []
        ]
        return ScreenInfo(
            width=float(data["width"]),
            height=float(data["height"]),
            backing_scale=float(data.get("backing_scale") or 1),
            display_count=int(data.get("display_count") or 1),
            frontmost_app=str(data.get("frontmost_app") or ""),
            windows=windows,
        )
    except (ValueError, KeyError, TypeError) as error:
        raise ComputerError(f"读不懂屏幕信息：{output[:200]}") from error


# 鼠标动作：先把指针移过去（有的应用要先收到移动才认点击），再发按下 / 抬起。
# 没有辅助功能权限时 CGEventPost 不报错、事件直接丢掉，所以先查一下，没权限就明确报错。
MOUSE_SCRIPT = """
ObjC.import("CoreGraphics");
ObjC.import("ApplicationServices");
function post(type, x, y, button, clicks) {
  var event = $.CGEventCreateMouseEvent(null, type, {x: x, y: y}, button);
  if (clicks) $.CGEventSetIntegerValueField(event, $.kCGMouseEventClickState, clicks);
  $.CGEventPost($.kCGHIDEventTap, event);
}
function run(argv) {
  if (!$.AXIsProcessTrusted()) throw new Error("not allowed assistive access");
  var action = argv[0], x = Number(argv[1]), y = Number(argv[2]);
  var left = $.kCGMouseButtonLeft;
  post($.kCGEventMouseMoved, x, y, left, 0);
  delay(0.05);
  if (action === "click" || action === "double_click") {
    post($.kCGEventLeftMouseDown, x, y, left, 1);
    post($.kCGEventLeftMouseUp, x, y, left, 1);
    if (action === "double_click") {
      delay(0.05);
      post($.kCGEventLeftMouseDown, x, y, left, 2);
      post($.kCGEventLeftMouseUp, x, y, left, 2);
    }
  } else if (action === "right_click") {
    post($.kCGEventRightMouseDown, x, y, $.kCGMouseButtonRight, 1);
    post($.kCGEventRightMouseUp, x, y, $.kCGMouseButtonRight, 1);
  } else if (action === "drag") {
    var toX = Number(argv[3]), toY = Number(argv[4]), steps = 12;
    post($.kCGEventLeftMouseDown, x, y, left, 1);
    for (var i = 1; i <= steps; i++) {
      delay(0.02);
      post($.kCGEventLeftMouseDragged, x + (toX - x) * i / steps, y + (toY - y) * i / steps, left, 1);
    }
    post($.kCGEventLeftMouseUp, toX, toY, left, 1);
  } else if (action === "scroll") {
    var vertical = Number(argv[3]), horizontal = Number(argv[4]);
    $.CGEventPost($.kCGHIDEventTap, $.CGEventCreateScrollWheelEvent2(null, $.kCGScrollEventUnitLine, 2, vertical, horizontal, 0));
  }
  return "ok";
}
"""


async def mouse_action(action: str, x: float, y: float, *extra: float) -> None:
    """action：move / click / double_click / right_click / drag（extra 是终点）/ scroll（extra 是竖直、水平行数）。坐标单位是点。"""
    await run_jxa(MOUSE_SCRIPT, action, f"{x:.1f}", f"{y:.1f}", *(f"{value:g}" for value in extra))


# 粘贴：存下剪贴板里每一项的每种格式 → 放进要输入的文字 → cmd+v → 等应用读完 → 原样放回去
PASTE_SCRIPT = """
ObjC.import("AppKit");
function run(argv) {
  var board = $.NSPasteboard.generalPasteboard;
  var saved = [];
  var items = board.pasteboardItems;
  for (var i = 0; i < items.count; i++) {
    var item = items.objectAtIndex(i), types = item.types, entry = [];
    for (var j = 0; j < types.count; j++) {
      var type = types.objectAtIndex(j), data = item.dataForType(type);
      if (data) entry.push([type, data]);
    }
    saved.push(entry);
  }
  board.clearContents;
  board.setStringForType($(argv[0]), $.NSPasteboardTypeString);
  Application("System Events").keystroke("v", {using: "command down"});
  delay(0.4);
  board.clearContents;
  var restored = $.NSMutableArray.array;
  saved.forEach(function (entry) {
    var item = $.NSPasteboardItem.alloc.init;
    entry.forEach(function (pair) { item.setDataForType(pair[1], pair[0]); });
    restored.addObject(item);
  });
  if (restored.count > 0) board.writeObjects(restored);
  return "ok";
}
"""


async def paste_text(text: str) -> None:
    await run_jxa(PASTE_SCRIPT, text)
