"""web 和桌面端共用的前端模块（pocketagent/web/static/js/shared/）：格式化、Markdown 的链接和图片、端适配。

用 Node 加 tests/mock/fake_dom.mjs 里的极简 DOM 跑，不需要浏览器。两个前端都 import 这一份，
这里坏了两边一起坏，所以链接、图片这些和安全相关的行为要钉住。
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCRIPT_HEADER = r"""
const { installFakeDom, toHtml } = await import("./tests/mock/fake_dom.mjs");
installFakeDom();
const shared = "./pocketagent/web/static/js/shared";
const results = {};
"""

FORMAT_SCRIPT = r"""
const format = await import(`${shared}/format.js`);
const longAgo = new Date(Date.now() - 40 * 86400 * 1000).toISOString();
const twoDaysAgo = new Date(Date.now() - 2 * 86400 * 1000 - 1000).toISOString();
results.tokens = [1234, 35000, 1500000, 999, null, undefined, "abc"].map((value) => format.formatTokens(value));
results.bytes = [0, 2048, 3 * 1024 * 1024].map((value) => format.formatBytes(value));
results.relativeTwoDays = format.formatRelativeTime(twoDaysAgo);
results.relativeLongAgo = format.formatRelativeTime(longAgo);
results.relativeClockForLongAgo = format.formatClock(longAgo);
results.relativeGarbage = format.formatRelativeTime("not a date");
results.relativeFuture = format.formatRelativeTime(new Date(Date.now() + 5000).toISOString());
results.workspaceNames = ["/Users/me/project/", "C:\\Users\\me\\repo", "", "solo"].map((path) => format.workspaceName(path));
results.images = ["a/b.PNG", "C:\\shots\\x.webp", "notes.md", "no-extension"].map((path) => format.isImagePath(path));
process.stdout.write(JSON.stringify(results));
"""

MARKDOWN_SCRIPT = r"""
const { renderMarkdown } = await import(`${shared}/markdown.js`);
const render = (text, options) => toHtml(renderMarkdown(text, options));

results.link = render("看 [文档](https://example.com/a_b_c) 和 www.example.org");
results.unsafeLink = render("[点我](javascript:alert(1))");
results.html = render("<script>alert(1)</script> <b>粗</b>");
results.externalImage = render("![图](https://example.com/x.png)");

const opened = [];
const resolvedFragment = renderMarkdown("![本地](out/chart.png) ![没法换](../../etc/passwd)", {
  resolveImage: (source) => (source.startsWith("out/") ? Promise.resolve(`data:image/png;base64,${source}`) : null),
  onOpenImage: (source) => opened.push(source),
});
// resolveImage 返回 Promise：等它落地再看 src
await new Promise((resolve) => setTimeout(resolve, 0));
results.localImage = toHtml(resolvedFragment);
const localImage = resolvedFragment.childNodes[0].childNodes.find((node) => node.tagName === "img");
await localImage.dispatch("click");
results.opened = opened;

const failedFragment = renderMarkdown("![坏图](out/missing.png)", { resolveImage: () => Promise.reject(new Error("404")) });
await new Promise((resolve) => setTimeout(resolve, 0));
results.failedImage = toHtml(failedFragment);

results.syncImage = render("![同步](a.png)", { resolveImage: (source) => `api/file/raw?path=${source}` });
results.codeBlock = render("```py\nprint('<hi>')\n```");
results.table = render("| a | b |\n|:--|--:|\n| 1 | 2 | 3 |");
results.taskList = render("- [x] 做完了\n- [ ] 还没做");
process.stdout.write(JSON.stringify(results));
"""

PLATFORM_SCRIPT = r"""
const platform = await import(`${shared}/platform.js`);
const capture = (action) => {
  try {
    action();
    return "ok";
  } catch (error) {
    return error.message;
  }
};
results.beforeConfigure = capture(() => platform.icon("folder"));
results.missingPiece = capture(() => platform.configurePlatform({ icon: () => null }));

const calls = [];
platform.configurePlatform({
  icon: (name, className) => {
    const node = document.createElement("svg");
    node.className = `icon ${name} ${className}`.trim();
    return node;
  },
  requestJson: async (path) => {
    calls.push(["GET", path]);
    return {};
  },
  sendJson: async (path, method, body) => {
    calls.push([method, path, body]);
    return {};
  },
  imageSource: (apiPath) => `${apiPath}&token=t`,
});
const { button, el } = await import(`${shared}/dom.js`);
results.button = toHtml(button("ghost", "保存", "check", "保存设置"));
results.zeroText = toHtml(el("span", "count", 0));
await platform.requestJson("/api/info");
await platform.sendJson("/api/workspaces", "POST", { path: "/tmp" });
results.calls = calls;
results.image = await platform.imageSource("/api/file/raw?path=a.png");
process.stdout.write(JSON.stringify(results));
"""

POPOVER_SCRIPT = r"""
const { placePopover, resetPopoverPlacement } = await import(`${shared}/popover.js`);
// 只摆位置要用到的几样：祖先链、计算样式、矩形。弹层贴在按钮左边，宽 340
const rectOf = (top, bottom, left = 0, right = 1000) => ({ top, bottom, left, right, width: right - left, height: bottom - top });
const makeCase = ({ windowHeight, windowWidth = 1000, containerTop, anchorTop, anchorLeft = 100, natural }) => {
  globalThis.document = { body: { tag: "body" } };
  globalThis.window = { innerHeight: windowHeight, innerWidth: windowWidth, getComputedStyle: (node) => node.computed };
  const container = { parentElement: document.body, computed: { overflowX: "hidden", overflowY: "auto" }, getBoundingClientRect: () => rectOf(containerTop, windowHeight, 0, windowWidth) };
  const row = { parentElement: container, computed: { overflowX: "visible", overflowY: "visible" } };
  const anchor = { parentElement: row, getBoundingClientRect: () => rectOf(anchorTop, anchorTop + 30, anchorLeft, anchorLeft + 120) };
  const classes = new Set();
  const popover = {
    style: {},
    scrollHeight: natural,
    classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), remove: (name) => classes.delete(name) },
    get offsetLeft() { return Number.parseFloat(this.style.left || "0"); },
    getBoundingClientRect() { const left = anchorLeft + this.offsetLeft; return rectOf(0, 0, left, left + 340); },
  };
  const placement = placePopover(popover, anchor, { gap: 8, margin: 12, maxHeight: 480 });
  return { placement, opensDown: classes.has("opens-down"), maxHeight: popover.style.maxHeight, left: popover.style.left, reset: () => { resetPopoverPlacement(popover); return { opensDown: classes.has("opens-down"), maxHeight: popover.style.maxHeight, left: popover.style.left }; } };
};

// 聊天页：输入框在底部，上面空间够，往上弹，高度按内容
results.roomAbove = makeCase({ windowHeight: 880, containerTop: 0, anchorTop: 800, natural: 400 });
// 新任务页：按窗口算上面还够，但滚动容器从 300 开始，往上弹会被切掉，要往下弹
results.clippedByContainer = makeCase({ windowHeight: 880, containerTop: 300, anchorTop: 420, natural: 400 });
// 窗口矮，上下都不够：挑大的那边，限高让面板自己滚
results.tight = makeCase({ windowHeight: 460, containerTop: 64, anchorTop: 246, natural: 400 });
// 按钮靠右，面板往右会伸出窗口：往左挪回来
const shifted = makeCase({ windowHeight: 880, containerTop: 0, anchorTop: 800, anchorLeft: 800, natural: 300 });
results.shifted = { ...shifted, afterReset: shifted.reset() };
process.stdout.write(JSON.stringify(results));
"""


class SharedFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("没装 Node，跳过前端共享模块测试")

    def run_script(self, body: str) -> dict:
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", SCRIPT_HEADER + body],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            # Node 输出总是 UTF-8；不写的话 Windows 上按本地代码页解码，中文会乱码或直接解码失败
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_format_helpers(self) -> None:
        results = self.run_script(FORMAT_SCRIPT)

        self.assertEqual(results["tokens"], ["1.2k", "35k", "1.5m", "999", "?", "?", "?"])
        self.assertEqual(results["bytes"], ["0 B", "2 KB", "3.0 MB"])
        self.assertEqual(results["relativeTwoDays"], "2 天前")
        # 一周以外直接给日期，比「40 天前」好认
        self.assertEqual(results["relativeLongAgo"], results["relativeClockForLongAgo"])
        self.assertEqual(results["relativeGarbage"], "")
        self.assertEqual(results["relativeFuture"], "刚刚")
        self.assertEqual(results["workspaceNames"], ["project", "repo", "", "solo"])
        self.assertEqual(results["images"], [True, True, False, False])

    def test_markdown_links_images_and_blocks(self) -> None:
        results = self.run_script(MARKDOWN_SCRIPT)

        # 链接都开新窗口：浏览器里是新标签页，桌面端由主进程交给系统浏览器
        self.assertIn('<a class="md-link" href="https://example.com/a_b_c" target="_blank" rel="noopener">文档</a>', results["link"])
        self.assertIn('href="https://www.example.org"', results["link"])
        self.assertNotIn("<a", results["unsafeLink"])
        self.assertIn("&lt;script&gt;", results["html"])
        self.assertNotIn("<script", results["html"])
        self.assertNotIn("<b>", results["html"])
        self.assertIn('<a class="md-image-link" href="https://example.com/x.png"', results["externalImage"])

        self.assertIn('src="data:image/png;base64,out/chart.png"', results["localImage"])
        # 换不出地址的路径按原文显示，不去猜
        self.assertIn("![没法换](../../etc/passwd)", results["localImage"])
        self.assertEqual(results["opened"], ["out/chart.png"])
        # 取图失败就把 img 去掉，不留一个破图标
        self.assertNotIn("<img", results["failedImage"])
        self.assertIn('src="api/file/raw?path=a.png"', results["syncImage"])

        self.assertIn('<button class="code-copy" type="button">复制</button>', results["codeBlock"])
        self.assertIn("print('&lt;hi&gt;')", results["codeBlock"])
        # 左对齐是默认样式，不加类名；单元格数以表头为准，多出来的第三格丢掉
        self.assertIn("<th>a</th>", results["table"])
        self.assertIn('<td class="md-align-right">2</td>', results["table"])
        self.assertNotIn(">3<", results["table"])
        self.assertIn('<ul class="md-task-list">', results["taskList"])
        self.assertIn("<input type=\"checkbox\" disabled checked>", results["taskList"])

    def test_platform_adapter(self) -> None:
        results = self.run_script(PLATFORM_SCRIPT)

        self.assertIn("configurePlatform", results["beforeConfigure"])
        self.assertIn("缺少 requestJson", results["missingPiece"])
        self.assertEqual(
            results["button"],
            '<button class="ghost" type="button" title="保存设置" aria-label="保存设置">'
            '<svg class="icon check"></svg><span>保存</span></button>',
        )
        self.assertEqual(results["zeroText"], '<span class="count">0</span>')
        self.assertEqual(results["calls"], [["GET", "/api/info"], ["POST", "/api/workspaces", {"path": "/tmp"}]])
        self.assertEqual(results["image"], "/api/file/raw?path=a.png&token=t")

    def test_popover_placement(self) -> None:
        results = self.run_script(POPOVER_SCRIPT)

        self.assertEqual(results["roomAbove"]["placement"], "above")
        self.assertFalse(results["roomAbove"]["opensDown"])
        self.assertEqual(results["roomAbove"]["maxHeight"], "480px")
        # 只看窗口会判成往上弹；算上会裁内容的滚动容器才知道上面只剩 100 像素
        self.assertEqual(results["clippedByContainer"]["placement"], "below")
        self.assertTrue(results["clippedByContainer"]["opensDown"])
        # 上面 246-64-20=162，下面 460-276-20=164：往下，限高 164
        self.assertEqual(results["tight"]["placement"], "below")
        self.assertEqual(results["tight"]["maxHeight"], "164px")
        # 右边界 1000-12=988，面板本来到 800+340=1140：左移 152
        self.assertEqual(results["shifted"]["left"], "-152px")
        self.assertEqual(results["shifted"]["afterReset"], {"opensDown": False, "maxHeight": "", "left": ""})


class StylesheetSyntaxTest(unittest.TestCase):
    """样式表少一个右括号，浏览器不报错，只是后面的规则全被当成嵌套规则吞掉：
    桌面端就这样丢过右栏、弹窗和自动化页的全部样式。这里按括号配对做个最基本的检查。"""

    STYLESHEETS = [
        ROOT / "pocketagent" / "web" / "static" / "style.css",
        ROOT / "desktop" / "renderer" / "style.css",
    ]

    def test_braces_are_balanced(self) -> None:
        for path in self.STYLESHEETS:
            with self.subTest(stylesheet=path.relative_to(ROOT)):
                text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
                self.assertEqual(text.count("/*"), 0, "有没闭合的注释")
                depth = 0
                for line_number, line in enumerate(text.splitlines(), 1):
                    for character in line:
                        depth += {"{": 1, "}": -1}.get(character, 0)
                    self.assertGreaterEqual(depth, 0, f"第 {line_number} 行多了一个 }}")
                    # 两份样式表都只有 @media 套一层规则，再深就是漏了右括号
                    self.assertLessEqual(depth, 2, f"第 {line_number} 行附近有没闭合的 {{")
                self.assertEqual(depth, 0, "结尾还有没闭合的 {")


if __name__ == "__main__":
    unittest.main()
