"""浏览器前端不碰 DOM 的核心逻辑：斜杠命令识别、状态事件总线、模型查找、token / URL 拼接。

用 Node 直接 import 前端的 ES 模块，不需要浏览器；DOM 控件级的交互不在这里测。
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = r"""
globalThis.window = {
  location: { href: "http://localhost:8765/?token=abc" },
  localStorage: {
    store: new Map(),
    getItem(key) { return this.store.get(key) ?? null; },
    setItem(key, value) { this.store.set(key, String(value)); },
  },
};
globalThis.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => "" });

const state = await import("./pocketagent/web/static/js/core/state.js");
const commands = await import("./pocketagent/web/static/js/core/commands.js");
const api = await import("./pocketagent/web/static/js/core/api.js");

const failures = [];
function check(name, condition) {
  if (!condition) failures.push(name);
}
check("looksLikeCommand help", commands.looksLikeCommand("/help"));
check("looksLikeCommand with args", commands.looksLikeCommand("/model ds"));
check("looksLikeCommand path", !commands.looksLikeCommand("/usr/bin"));
check("looksLikeCommand plain text", !commands.looksLikeCommand("hello"));
check("mode labels", Object.keys(state.MODE_LABELS).length === 5 && state.MODE_LABELS.full_auto === "完全访问");
check("agent mode labels", state.AGENT_MODE_LABELS.plan === "计划");

const events = [];
state.on("demo", (payload) => events.push(payload));
state.emit("demo", 1);
state.emit("demo", 2);
check("state event bus", events.join(",") === "1,2");

state.state.info = { models: [{ id: "m1", name: "模型一" }] };
check("findModel", state.findModel("m1")?.name === "模型一" && state.findModel("missing") === null);
check("withToken", api.withToken("api/info") === "http://localhost:8765/api/info?token=abc");
check("socketUrl", api.socketUrl("s 1") === "ws://localhost:8765/ws/s%201?token=abc");

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exit(1);
}
process.stdout.write("ok");
"""


class WebCoreJavaScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("没装 Node，跳过前端核心逻辑测试")

    def test_core_modules(self) -> None:
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", CHECK_SCRIPT],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")
