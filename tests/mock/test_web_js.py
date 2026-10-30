"""浏览器前端不碰 DOM 的核心逻辑：斜杠命令识别、状态事件总线、模型查找、token / URL 拼接，
以及触发器表单吐出来的 JSON 后端认不认。

用 Node 直接 import 前端的 ES 模块，不需要浏览器；DOM 控件级的交互不在这里测。
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from pocketagent.server.triggers.model import WebhookSource, trigger_from_dict

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

# 弹窗里能选出来的每种组合都走一遍，把结果交给后端的校验函数——字段名对不上就会在这一步炸
TRIGGER_FORM_SCRIPT = r"""
const form = await import("./pocketagent/web/static/js/automations/trigger-form.js");

const cases = [
  {
    name: "每天定时",
    values: { triggerKind: "clock", clockMode: "daily", dailyTime: "18:30", weekdays: [0, 2], conditionKind: "always" },
  },
  {
    name: "隔一段时间",
    values: { triggerKind: "clock", clockMode: "interval", clockIntervalMinutes: "120", conditionKind: "always" },
  },
  {
    name: "等任务结束",
    values: { triggerKind: "task_end", watchAutomation: "a1", conditionKind: "status" },
  },
  {
    name: "外部 POST",
    values: { triggerKind: "webhook", conditionKind: "always" },
  },
  {
    name: "轮询",
    values: { triggerKind: "poll", pollIntervalMinutes: "15", pollCommand: "test -f ready", conditionKind: "always" },
  },
  {
    name: "正则条件",
    values: { triggerKind: "webhook", conditionKind: "match", conditionPattern: "已完成|DONE" },
  },
  {
    name: "脚本条件",
    values: { triggerKind: "clock", clockMode: "daily", dailyTime: "09:00", weekdays: [0], conditionKind: "shell", conditionCommand: "grep -q ok report.txt" },
  },
];

process.stdout.write(JSON.stringify(cases.map((item) => ({ name: item.name, trigger: form.triggerFromForm(item.values) }))));
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


class TriggerFormJavaScriptTest(unittest.TestCase):
    """前端的表单映射和后端的校验是同一条契约，两边字段名差一个字母就只有点保存才报错。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("没装 Node，跳过触发器表单测试")
        result = subprocess.run(
            [cls.node, "--input-type=module", "-e", TRIGGER_FORM_SCRIPT],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.cases = {item["name"]: item["trigger"] for item in json.loads(result.stdout)}

    def parse(self, name: str):
        return trigger_from_dict(self.cases[name])

    def test_daily_clock(self) -> None:
        trigger = self.parse("每天定时")

        self.assertEqual(trigger.describe(), "周一、周三 18:30")
        self.assertEqual(trigger.source.weekdays, [0, 2])

    def test_interval_clock(self) -> None:
        self.assertEqual(self.parse("隔一段时间").describe(), "每 2 小时")

    def test_task_end(self) -> None:
        trigger = self.parse("等任务结束")

        self.assertEqual(trigger.source.automation_id, "a1")
        self.assertEqual(trigger.describe({"a1": "日报"}), "「日报」跑完后（仅 done）")

    def test_webhook_gets_a_secret_from_the_server(self) -> None:
        trigger = self.parse("外部 POST")

        self.assertIsInstance(trigger.source, WebhookSource)
        # 前端不该带 secret 过来，服务端保存时生成
        self.assertNotIn("secret", self.cases["外部 POST"]["source"])
        self.assertGreaterEqual(len(trigger.source.secret), 20)

    def test_poll(self) -> None:
        trigger = self.parse("轮询")

        self.assertEqual(trigger.source.interval_minutes, 15)
        self.assertEqual(trigger.source.command, "test -f ready")

    def test_conditions(self) -> None:
        self.assertEqual(self.parse("正则条件").condition.pattern, "已完成|DONE")
        self.assertEqual(self.parse("脚本条件").condition.command, "grep -q ok report.txt")

    def test_everything_survives_a_save_and_load(self) -> None:
        """存回去再读出来还是同一个触发器：字段没被丢掉，也没有多出别人的。"""
        for name in self.cases:
            with self.subTest(name):
                trigger = self.parse(name)
                reloaded = trigger_from_dict(trigger.to_dict())
                self.assertEqual(reloaded.describe(), trigger.describe())
                self.assertEqual(reloaded.to_dict()["source"]["kind"], trigger.to_dict()["source"]["kind"])

