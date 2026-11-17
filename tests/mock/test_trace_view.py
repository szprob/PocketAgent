"""右侧栏「轨迹」：服务端把 trace 整理成条目（server/trace_view.py），前端画时间轴和列表（shared/trace-view.js）。"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pocketagent.server.trace_view import build_trace_view

ROOT = Path(__file__).resolve().parents[2]

TRACE_RECORDS = [
    {"type": "turn_start", "time": 100.0, "user_text": "查一下上海的天气", "attachments": []},
    {"type": "node_start", "time": 100.1, "node": "GuardNode"},
    {"type": "node_start", "time": 100.2, "node": "ThinkNode"},
    {
        "type": "llm_response",
        "time": 104.2,
        "model": "deepseek-flash",
        "text": "",
        "reasoning": "用户要天气，先查一下\n再回答",
        "tool_calls": [{"name": "web_fetch", "arguments": "{}"}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 300, "reasoning_tokens": 200},
    },
    {"type": "tool_call", "time": 104.3, "call_id": "c1", "name": "web_fetch", "summary": "https://wttr.in/Shanghai", "arguments": {"url": "https://wttr.in/Shanghai"}},
    {"type": "tool_result", "time": 105.8, "call_id": "c1", "name": "web_fetch", "content": "晴 16°C\n湿度 70%", "is_error": False, "denied": False},
    {"type": "tool_call", "time": 105.9, "call_id": "c2", "name": "subagent", "summary": "查空气质量（只读）", "arguments": {"description": "查空气质量"}},
    {"type": "subagent_progress", "time": 106.0, "call_id": "c2", "step": "tool", "name": "web_search", "summary": "上海 AQI"},
    {"type": "tool_result", "time": 110.0, "call_id": "c2", "name": "subagent", "content": "AQI 80", "is_error": False, "denied": False},
    "这一行不是 JSON",
    {"type": "node_start", "time": 110.1, "node": "ThinkNode"},
    {"type": "llm_response", "time": 112.1, "model": "deepseek-flash", "text": "上海今天晴，16°C，AQI 80。", "reasoning": "", "tool_calls": [], "usage": {"completion_tokens": 40}},
    {"type": "turn_end", "time": 112.2, "stop_reason": "done"},
    {"type": "turn_start", "time": 200.0, "user_text": "那明天呢", "attachments": []},
    {"type": "compaction", "time": 200.5, "tokens_before": 90000, "tokens_after": 5000, "background": True, "reason": "auto"},
    {"type": "error", "time": 201.0, "message": "BadRequestError: 400"},
    {"type": "tool_call", "time": 202.0, "call_id": "c3", "name": "bash", "summary": "sleep 100", "arguments": {"command": "sleep 100"}},
]


def write_trace(directory: Path) -> Path:
    path = directory / "task.jsonl"
    lines = [record if isinstance(record, str) else json.dumps(record, ensure_ascii=False) for record in TRACE_RECORDS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TraceViewBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.view = build_trace_view(write_trace(Path(self.tmp.name)), "你是 PocketAgent")
        self.entries = self.view["entries"]

    def test_steps_in_order_with_turns(self) -> None:
        kinds = [(entry["kind"], entry["turn"]) for entry in self.entries]
        self.assertEqual(
            kinds,
            [
                ("system", 0), ("user", 1), ("assistant", 1), ("tool", 1), ("tool", 1), ("assistant", 1),
                ("user", 2), ("context", 2), ("error", 2), ("tool", 2),
            ],
        )
        self.assertEqual([turn["stop_reason"] for turn in self.view["turns"]], ["done", ""])
        self.assertEqual(self.entries[0]["detail"], "你是 PocketAgent")

    def test_model_step_starts_when_the_model_was_called_and_splits_tokens(self) -> None:
        think = self.entries[2]
        self.assertEqual((think["start"], think["end"]), (100.2, 104.2))
        self.assertEqual((think["reasoning_tokens"], think["output_tokens"]), (200, 100))
        # 没说话、只调工具时，那一行显示思考（压成一行）
        self.assertEqual(think["text"], "用户要天气，先查一下 再回答")
        self.assertIn("〔调用〕web_fetch", think["detail"])

    def test_tool_steps_carry_arguments_results_and_children(self) -> None:
        fetch, subagent = self.entries[3], self.entries[4]
        self.assertEqual((fetch["name"], fetch["text"], fetch["end"]), ("web_fetch", "https://wttr.in/Shanghai", 105.8))
        self.assertEqual(fetch["result"], "晴 16°C\n湿度 70%")
        self.assertIn("wttr.in", fetch["arguments"])
        self.assertEqual([(child["name"], child["summary"]) for child in subagent["children"]], [("web_search", "上海 AQI")])
        # 还没出结果的工具：没有结束时间，界面显示「进行中」
        self.assertIsNone(self.entries[9]["end"])

    def test_compaction_and_errors_show_up(self) -> None:
        self.assertIn("90000 → 5000", self.entries[7]["text"])
        self.assertEqual(self.entries[8]["text"], "BadRequestError: 400")

    def test_missing_trace_only_has_the_system_prompt(self) -> None:
        view = build_trace_view(Path(self.tmp.name) / "nope.jsonl", "系统")

        self.assertEqual([entry["kind"] for entry in view["entries"]], ["system"])
        self.assertEqual(view["turns"], [])


FRONTEND_SCRIPT = r"""
const { installFakeDom } = await import("./tests/mock/fake_dom.mjs");
installFakeDom();
const shared = "./pocketagent/web/static/js/shared";
const platform = await import(`${shared}/platform.js`);
const requested = [];
platform.configurePlatform({
  icon: () => document.createElement("span"),
  requestJson: async (path) => {
    requested.push(path);
    return TRACE;
  },
  sendJson: async () => ({}),
  imageSource: (path) => path,
});
const { TraceView, initPanelTabs } = await import(`${shared}/trace-view.js`);
const results = {};
const container = document.createElement("div");
const view = new TraceView(container);
view.setVisible(true);
view.showTask("task 1");
await new Promise((resolve) => setTimeout(resolve, 0));

const rows = () => container.querySelectorAll(".trace-item");
results.requested = requested;
results.rowKinds = rows().map((row) => row.className.replace("trace-item ", "").split(" ")[0]);
results.turnLabels = container.querySelectorAll(".trace-turn").map((node) => node.textContent).filter(Boolean);
results.toolRow = rows()[3].textContent;
results.children = container.querySelectorAll(".trace-child").map((node) => node.textContent);
results.running = container.querySelectorAll(".trace-duration").map((node) => node.textContent).slice(-1)[0];
const blocks = container.querySelectorAll(".trace-block");
results.blockCount = blocks.length;
results.thinkingWidth = blocks[2].querySelector(".trace-block-thinking").style.width;
results.longWidth = blocks[2].style.width;

// 不按时长：每块一样宽
await container.querySelector(".trace-toggle").dispatch("click");
results.equalWidths = [...new Set(container.querySelectorAll(".trace-block").map((block) => block.style.width))];

// 点一行展开，看到参数和结果
await rows()[3].querySelector(".trace-row").dispatch("click");
results.detail = container.querySelector(".trace-detail").textContent;

// 搜索过滤，时间轴点块会清掉搜索、选中那一步
const search = container.querySelector(".trace-search");
search.value = "AQI";
await search.dispatch("input");
results.searched = rows().length;
await container.querySelectorAll(".trace-block")[7].dispatch("click");
results.afterSelect = { rows: rows().length, selected: container.querySelector(".trace-item.selected")?.dataset.entryId };

// 页签：切到轨迹、记住选择、回调
const panel = document.createElement("aside");
const makeTab = (name) => { const tab = document.createElement("button"); tab.dataset.panelTab = name; panel.append(tab); return tab; };
const makePane = (name) => { const pane = document.createElement("div"); pane.dataset.panelPane = name; panel.append(pane); return pane; };
makeTab("overview"); const traceTab = makeTab("trace");
const overviewPane = makePane("overview"); const tracePane = makePane("trace");
const saved = {}; const changes = [];
initPanelTabs(panel, { load: (key, fallback) => fallback, save: (key, value) => { saved[key] = value; }, onChange: (tab) => changes.push(tab) });
await traceTab.dispatch("click");
results.tabs = { changes, saved, overviewHidden: overviewPane.classList.contains("hidden"), traceHidden: tracePane.classList.contains("hidden"), panelClass: panel.className };
process.stdout.write(JSON.stringify(results));
"""


class TraceViewFrontendTest(unittest.TestCase):
    """前端拿服务端整理好的数据画时间轴和列表；用 Node 加假 DOM 跑。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("没装 Node，跳过前端测试")

    def test_timeline_list_search_and_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = build_trace_view(write_trace(Path(tmp)), "你是 PocketAgent")
        script = f"const TRACE = {json.dumps(view, ensure_ascii=False)};\n" + FRONTEND_SCRIPT
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", script],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        results = json.loads(result.stdout)

        self.assertEqual(results["requested"], ["/api/sessions/task%201/trace"])
        self.assertEqual(
            results["rowKinds"],
            ["kind-system", "kind-user", "kind-assistant", "kind-tool", "kind-tool", "kind-assistant", "kind-user", "kind-context", "kind-error", "kind-tool"],
        )
        self.assertEqual(results["turnLabels"], ["1 轮", "2 轮"])
        self.assertIn("web_fetch https://wttr.in/Shanghai → 晴 16°C", results["toolRow"])
        self.assertEqual(len(results["children"]), 1)
        self.assertIn("web_search", results["children"][0])
        self.assertEqual(results["running"], "进行中")
        # 时间轴：每一步一块；模型那块按 token 分出思考段（200 / 300），按时长时 4 秒的块比一格宽
        self.assertEqual(results["blockCount"], 10)
        self.assertTrue(results["thinkingWidth"].startswith("66.6"))
        self.assertGreater(int(results["longWidth"].rstrip("px")), 14)
        self.assertEqual(results["equalWidths"], ["14px"])
        self.assertIn("参数", results["detail"])
        self.assertIn("湿度 70%", results["detail"])
        # 子 agent 那一步（子调用和结果里有）和最后的回答都提到 AQI
        self.assertEqual(results["searched"], 2)
        self.assertEqual(results["afterSelect"], {"rows": 10, "selected": "7"})
        self.assertEqual(results["tabs"]["changes"], ["overview", "trace"])
        self.assertEqual(results["tabs"]["saved"], {"rightPanelTab": "trace"})
        self.assertTrue(results["tabs"]["overviewHidden"])
        self.assertFalse(results["tabs"]["traceHidden"])
        self.assertIn("trace-active", results["tabs"]["panelClass"])
