"""右侧栏「轨迹」：服务端把 trace 整理成条目（server/trace_view.py），前端画时间轴和列表（shared/trace-view.js）。"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pocketagent.server.trace_view import build_task_trace, build_trace_view, build_trace_view_from_calls

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


def openai_call(call_id, messages, response, started, ended, tools=None):
    return {
        "id": call_id,
        "started": started,
        "time": ended,
        "meta": {"model": "deepseek-flash", "model_name": "DeepSeek Flash"},
        "request": {"model": "deepseek-flash", "messages": messages, "tools": tools or []},
        "response": {"text": "", "reasoning": "", "tool_calls": [], "usage": None, **response},
    }


def assistant_with_calls(text, calls, reasoning=None):
    message = {"role": "assistant", "content": text or None, "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": args}} for cid, name, args in calls]}
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


class TraceViewFromCallsTest(unittest.TestCase):
    """轨迹就是模型调用原文：直接从每次请求 / 回复整理，跨轮去掉思考不算「历史换过了」。"""

    def build(self):
        system = {"role": "system", "content": "你是 PocketAgent"}
        user1 = {"role": "user", "content": "读 a.txt"}
        call1 = assistant_with_calls("", [("t1", "read_file", '{"path": "a.txt"}')], reasoning="先读")
        result1 = {"role": "tool", "tool_call_id": "t1", "content": "1\thi"}
        answer1 = {"role": "assistant", "content": "文件里是 hi"}
        notice = {"role": "user", "content": "（今天是 2026-11-18，系统提示里写的日期已经过时，以这条为准）"}
        user2 = {"role": "user", "content": "派个子 agent 查一下"}
        call2 = assistant_with_calls("", [("t2", "subagent", '{"description": "查", "prompt": "查一下"}')])
        sub_system = {"role": "system", "content": "你是 PocketAgent\n\n# 你现在是子 agent\n主 agent 派你去做一件事"}
        review_prompt = {"role": "user", "content": "你是编程助手的安全审查员。助手想执行 bash"}
        summary_message = {"role": "user", "content": "（之前的对话已压缩，以下是摘要）\n\n读了 a.txt……"}
        from pocketagent.core.compaction import SUMMARY_INSTRUCTION
        calls = [
            openai_call(0, [system, user1], {"reasoning": "先读", "tool_calls": [{"id": "t1", "name": "read_file", "arguments": '{"path": "a.txt"}'}], "usage": {"completion_tokens": 30, "reasoning_tokens": 20}}, 10.0, 12.0),
            # 第二步：上一次的回复（带思考）+ 工具结果
            openai_call(1, [system, user1, call1, result1], {"text": "文件里是 hi"}, 13.0, 14.0),
            # 新的一轮：上一轮的思考被去掉了（跨轮保留关着），前缀照样对得上；末尾插了一条日期提示
            openai_call(2, [system, user1, assistant_with_calls("", [("t1", "read_file", '{"path": "a.txt"}')]), result1, answer1, user2, notice], {"tool_calls": [{"id": "t2", "name": "subagent", "arguments": '{"description": "查", "prompt": "查一下"}'}]}, 20.0, 21.0),
            openai_call(3, [sub_system, {"role": "user", "content": "查一下"}], {"tool_calls": [{"id": "s1", "name": "web_search", "arguments": '{"query": "上海天气"}'}]}, 21.5, 22.0),
            openai_call(4, [review_prompt], {"text": "{\"allow\": true, \"reason\": \"只读\"}"}, 22.1, 22.3),
            openai_call(5, [system, user1, call1, result1, answer1, user2, notice, call2, {"role": "tool", "tool_call_id": "t2", "content": "晴"}, {"role": "user", "content": SUMMARY_INSTRUCTION}], {"text": "交接摘要正文"}, 30.0, 33.0),
            # 压缩之后：历史换成摘要
            openai_call(6, [system, summary_message], {"text": "接着做完了"}, 34.0, 35.0),
        ]
        return build_trace_view_from_calls(calls)

    def test_steps_come_from_the_raw_requests_and_responses(self) -> None:
        view = self.build()
        rows = [(entry["kind"], entry["turn"], entry["text"][:12]) for entry in view["entries"]]

        self.assertEqual(view["source"], "calls")
        self.assertEqual(
            [kind for kind, _, _ in rows],
            ["system", "user", "assistant", "tool", "assistant", "user", "context", "assistant", "tool", "review", "summary", "context", "context", "assistant"],
        )
        self.assertEqual([turn["index"] for turn in view["turns"]], [1, 2])
        entries = view["entries"]
        # 模型步骤：从发请求到收完回复；思考 / 输出 token 照 usage 分
        self.assertEqual((entries[2]["start"], entries[2]["end"], entries[2]["reasoning_tokens"], entries[2]["output_tokens"]), (10.0, 12.0, 20, 10))
        # 工具：参数原样、结果从下一次请求里来，结束时间是带着结果去调模型的那一刻
        self.assertEqual((entries[3]["name"], entries[3]["text"], entries[3]["result"], entries[3]["end"]), ("read_file", "a.txt", "1\thi", 13.0))
        self.assertIn("今天是 2026-11-18", entries[6]["text"])
        # 子 agent 的调用挂在 subagent 工具底下
        self.assertEqual([(child["name"], child["summary"]) for child in entries[8]["children"]], [("web_search", "上海天气")])
        self.assertIn("只读", entries[9]["text"])
        self.assertEqual(entries[10]["detail"], "交接摘要正文")
        # 压缩后历史换成摘要：先标一句「历史换过了」，再显示摘要
        self.assertIn("历史换过了", entries[11]["text"])
        self.assertEqual(entries[12]["text"], "压缩后的摘要（替换了之前的对话）")

    def test_anthropic_requests_are_read_too(self) -> None:
        call = {
            "id": 0, "started": 1.0, "time": 2.0, "meta": {"model": "claude"},
            "request": {
                "model": "claude",
                "system": "系统",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "读 a.txt"}]},
                    {"role": "assistant", "content": [{"type": "thinking", "thinking": "嗯", "signature": "s"}, {"type": "tool_use", "id": "u1", "name": "read_file", "input": {"path": "a.txt"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "hi"}]},
                ],
            },
            "response": {"text": "是 hi", "reasoning": "", "tool_calls": [], "usage": None},
        }

        kinds = [(entry["kind"], entry["text"]) for entry in build_trace_view_from_calls([call])["entries"]]

        self.assertEqual(
            kinds,
            [("system", "系统提示词"), ("user", "读 a.txt"), ("assistant", "嗯"), ("tool", "a.txt"), ("assistant", "是 hi")],
        )

    def test_failed_calls_show_up_and_tool_errors_come_from_the_event_trace(self) -> None:
        system = {"role": "system", "content": "系统"}
        user = {"role": "user", "content": "跑一下"}
        calls = [
            openai_call(0, [system, user], {"tool_calls": [{"id": "b1", "name": "bash", "arguments": '{"command": "false"}'}]}, 1.0, 2.0),
            {**openai_call(1, [system, user, assistant_with_calls("", [("b1", "bash", '{"command": "false"}')]), {"role": "tool", "tool_call_id": "b1", "content": "退出码 1"}], {}, 3.0, 9.0), "response": None, "error": "APIConnectionError: 连不上"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            calls_path = Path(tmp) / "calls.jsonl"
            raw = [{"id": c["id"], "started": c["started"], "time": c["time"], "meta": c["meta"], "request": {"model": "m"},
                    "messages": {"base": None, "keep": 0, "append": c["request"]["messages"]}, "tools": {"absent": True},
                    "response": c["response"], **({"error": c["error"]} if "error" in c else {})} for c in calls]
            calls_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in raw) + "\n", encoding="utf-8")
            trace_path = Path(tmp) / "trace.jsonl"
            trace_path.write_text(json.dumps({"type": "tool_result", "time": 2.5, "call_id": "b1", "is_error": True, "denied": False}) + "\n", encoding="utf-8")
            view = build_task_trace(calls_path, trace_path)

        kinds = [entry["kind"] for entry in view["entries"]]
        self.assertEqual(kinds, ["system", "user", "assistant", "tool", "error"])
        tool, error = view["entries"][3], view["entries"][4]
        self.assertTrue(tool["is_error"])
        self.assertEqual(tool["result"], "退出码 1")
        self.assertIn("调用模型失败：APIConnectionError", error["text"])

    def test_without_raw_calls_falls_back_to_the_event_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = build_task_trace(Path(tmp) / "none.jsonl", write_trace(Path(tmp)), "系统")

        self.assertEqual(view["source"], "trace")
        self.assertEqual(view["entries"][1]["kind"], "user")


EXPORT_SCRIPT = r"""
const { installFakeDom } = await import("./tests/mock/fake_dom.mjs");
installFakeDom();
globalThis.window = globalThis;
const downloads = [];
const originalCreate = document.createElement;
document.createElement = (tagName) => {
  const node = originalCreate(tagName);
  if (tagName === "a") {
    node.click = () => downloads.push(node.download);
  }
  return node;
};
let blobText = "";
URL.createObjectURL = (blob) => { blobText = blob; return "blob:x"; };
URL.revokeObjectURL = () => {};
const shared = "./pocketagent/web/static/js/shared";
const platform = await import(`${shared}/platform.js`);
platform.configurePlatform({
  icon: () => document.createElement("span"),
  requestJson: async (path) => (path.endsWith("/calls") ? { calls: [{ id: 0, request: { model: "m" }, response: { text: "好" } }] } : VIEW),
  sendJson: async () => ({}),
  imageSource: (path) => path,
});
const { TraceView } = await import(`${shared}/trace-view.js`);
const container = document.createElement("div");
const view = new TraceView(container);
view.setVisible(true);
view.showTask("t1");
await new Promise((resolve) => setTimeout(resolve, 0));
const results = { note: container.querySelector(".trace-source").textContent, exportDisabled: container.querySelector(".trace-export").disabled };
await view.exportCalls();
results.downloads = downloads;
results.lines = (await blobText.text()).trim().split("\n").map((line) => JSON.parse(line));
results.after = container.querySelector(".trace-source").textContent;
process.stdout.write(JSON.stringify(results));
process.exit(0);
"""


class TraceExportFrontendTest(unittest.TestCase):
    """轨迹页「导出」：下载的就是这个任务的原文，一行一次调用。"""

    def test_export_downloads_the_raw_calls(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("没装 Node")
        view = {"source": "calls", "entries": [{"id": 0, "kind": "system", "turn": 0, "text": "系统提示词", "start": None, "end": None}], "turns": []}
        result = subprocess.run(
            [node, "--input-type=module", "-e", f"const VIEW = {json.dumps(view, ensure_ascii=False)};\n" + EXPORT_SCRIPT],
            cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        results = json.loads(result.stdout)

        self.assertEqual(results["note"], "来源：模型调用原文（每次请求和回复原样）")
        self.assertFalse(results["exportDisabled"])
        self.assertEqual(results["downloads"], ["pocketagent-calls-t1.jsonl"])
        self.assertEqual(results["lines"], [{"id": 0, "request": {"model": "m"}, "response": {"text": "好"}}])
        self.assertEqual(results["after"], "已导出 1 次调用")
