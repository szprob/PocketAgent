// 右侧栏「轨迹」：参考 DeepSeek Harness 的轨迹页，放在对话旁边，不用切页。
// 上面一条时间轴（输入 / 模型 / 工具三行色块，模型的色块浅色是思考、深色是输出），下面按轮列出每一步，点开看完整内容。
// 数据来自 GET /api/sessions/{id}/trace：服务端从模型调用原文整理（轨迹就是原文，这里只是排得好看），
// 老任务没有原文才退回事件记录（见 server/trace_view.py）。「导出」下载的就是这份原文（GET .../calls）。
// 任务跑着时收到事件就节流后重新取一遍，面板没打开时只记一笔，打开时再取。

import { button, el } from "./dom.js";
import { requestJson } from "./platform.js";

const KIND_LABELS = {
  system: "系统",
  user: "用户",
  interjection: "插话",
  assistant: "助手",
  tool: "工具",
  context: "上下文",
  error: "出错",
  review: "审批",
  summary: "压缩",
};
const SOURCE_NOTES = {
  calls: "来源：模型调用原文（每次请求和回复原样）",
  trace: "来源：事件记录（这个任务没有原文，导出不了）",
};
const LANES = [
  { key: "input", label: "输入" },
  { key: "model", label: "模型" },
  { key: "tool", label: "工具" },
];
const LANE_OF_KIND = {
  system: "input",
  user: "input",
  interjection: "input",
  context: "input",
  assistant: "model",
  error: "model",
  review: "model",
  summary: "model",
  tool: "tool",
};
const REFRESH_DELAY_MS = 600;
// 时间轴：不按时长时每一步一样宽；按时长时宽度随时长对数增长（几秒和几分钟的步骤都看得清）
const SLOT_PIXELS = 14;
const BLOCK_GAP_PIXELS = 2;
const TURN_GAP_PIXELS = 12;
// 列表离底部这么近时，刷新后继续贴着底部（跟着任务往下走）
const FOLLOW_THRESHOLD_PIXELS = 40;

export class TraceView {
  constructor(container) {
    this.container = container;
    this.taskId = null;
    this.data = null;
    this.visible = false;
    this.dirty = false;
    this.loading = false;
    this.refreshTimer = null;
    this.error = "";
    this.byDuration = true;
    this.showChildren = true;
    this.query = "";
    this.notice = "";
    this.expanded = new Set();
    this.selectedId = null;
    this.build();
  }

  build() {
    this.container.classList.add("trace-view");
    const toolbar = el("div", "trace-toolbar");
    this.durationToggle = this.toggleButton("时长", "色块宽度按每一步花的时间", () => {
      this.byDuration = !this.byDuration;
      this.render();
    });
    this.childrenToggle = this.toggleButton("子调用", "显示子 agent 里的每一次调用", () => {
      this.showChildren = !this.showChildren;
      this.render();
    });
    this.search = el("input", "trace-search");
    this.search.type = "search";
    this.search.placeholder = "搜索";
    this.search.addEventListener("input", () => {
      this.query = this.search.value.trim().toLowerCase();
      this.renderList();
    });
    this.exportButton = button("trace-export", "导出", "", "下载这个任务每次调模型的完整请求和回复（JSONL，一行一次调用）");
    this.exportButton.addEventListener("click", () => this.exportCalls());
    toolbar.append(this.durationToggle, this.childrenToggle, this.search, this.exportButton);

    this.sourceNote = el("div", "trace-source");
    this.timeline = el("div", "trace-timeline");
    this.list = el("div", "trace-list");
    this.container.replaceChildren(toolbar, this.sourceNote, this.timeline, this.list);
    this.render();
  }

  toggleButton(label, title, onClick) {
    const toggle = button("trace-toggle active", label, "", title);
    toggle.addEventListener("click", onClick);
    return toggle;
  }

  showTask(taskId) {
    this.taskId = taskId;
    this.data = null;
    this.error = "";
    this.expanded.clear();
    this.selectedId = null;
    this.render();
    if (this.visible) {
      this.load();
    } else {
      this.dirty = true;
    }
  }

  clear() {
    this.taskId = null;
    this.data = null;
    this.render();
  }

  setVisible(visible) {
    this.visible = visible;
    if (visible && this.taskId && (this.dirty || !this.data)) {
      this.load();
    }
  }

  // 有新事件（模型回复了、工具出结果了、一轮结束了）：节流后重新取
  refreshSoon() {
    if (!this.taskId) {
      return;
    }
    if (!this.visible) {
      this.dirty = true;
      return;
    }
    window.clearTimeout(this.refreshTimer);
    this.refreshTimer = window.setTimeout(() => this.load(), REFRESH_DELAY_MS);
  }

  async load() {
    const taskId = this.taskId;
    if (!taskId || this.loading) {
      this.dirty = true;
      return;
    }
    this.loading = true;
    this.dirty = false;
    try {
      const data = await requestJson(`/api/sessions/${encodeURIComponent(taskId)}/trace`);
      if (taskId === this.taskId) {
        this.data = data;
        this.error = "";
      }
    } catch (error) {
      if (taskId === this.taskId) {
        this.error = `读不到轨迹：${error.message || error}`;
      }
    } finally {
      this.loading = false;
    }
    if (taskId !== this.taskId) {
      return;
    }
    this.render();
    // 取的过程中又来了新事件
    if (this.dirty) {
      this.refreshSoon();
    }
  }

  render() {
    this.durationToggle.classList.toggle("active", this.byDuration);
    this.childrenToggle.classList.toggle("active", this.showChildren);
    const source = this.data?.source;
    this.sourceNote.textContent = this.notice || SOURCE_NOTES[source] || "";
    this.exportButton.disabled = !this.taskId || source === "trace";
    this.renderTimeline();
    this.renderList();
  }

  entries() {
    return this.data?.entries || [];
  }

  // ---- 时间轴 ----

  renderTimeline() {
    const entries = this.entries();
    this.timeline.replaceChildren();
    if (entries.length === 0) {
      return;
    }
    const labels = el("div", "trace-lane-labels");
    for (const lane of LANES) {
      labels.append(el("div", "trace-lane-label", lane.label));
    }
    const scroller = el("div", "trace-lanes");
    const canvas = el("div", "trace-canvas");
    let x = 0;
    let previousTurn = null;
    for (const entry of entries) {
      if (previousTurn !== null && entry.turn !== previousTurn) {
        x += TURN_GAP_PIXELS;
        const divider = el("div", "trace-turn-divider");
        divider.style.left = `${x - TURN_GAP_PIXELS / 2}px`;
        canvas.append(divider);
      }
      previousTurn = entry.turn;
      const width = this.blockWidth(entry);
      canvas.append(this.block(entry, x, width));
      x += width + BLOCK_GAP_PIXELS;
    }
    canvas.style.width = `${x}px`;
    scroller.append(canvas);
    this.timeline.append(labels, scroller);
    // 新的步骤在右边，打开时先看到最近的
    scroller.scrollLeft = scroller.scrollWidth;
  }

  blockWidth(entry) {
    if (!this.byDuration || entry.start === null || entry.start === undefined) {
      return SLOT_PIXELS;
    }
    const seconds = durationSeconds(entry);
    if (seconds < 0.05) {
      return SLOT_PIXELS;
    }
    return Math.round(Math.max(6, 8 + 22 * Math.log2(1 + seconds)));
  }

  block(entry, x, width) {
    const lane = LANE_OF_KIND[entry.kind] || "input";
    const block = el("button", `trace-block kind-${entry.kind} lane-${lane}`);
    block.type = "button";
    block.style.left = `${x}px`;
    block.style.width = `${width}px`;
    block.title = blockTitle(entry);
    if (isRunning(entry)) {
      block.classList.add("running");
    }
    if (entry.kind === "tool" && (entry.is_error || entry.denied)) {
      block.classList.add("failed");
    }
    // 模型的色块按 token 分成思考（浅）和输出（深）两段
    const total = (entry.reasoning_tokens || 0) + (entry.output_tokens || 0);
    if (entry.kind === "assistant" && total > 0) {
      const thinking = el("span", "trace-block-thinking");
      thinking.style.width = `${(100 * entry.reasoning_tokens) / total}%`;
      block.append(thinking);
    }
    block.addEventListener("click", () => this.select(entry.id));
    return block;
  }

  // ---- 列表 ----

  renderList() {
    const scrollParent = this.list;
    const followBottom =
      scrollParent.scrollHeight - scrollParent.scrollTop - scrollParent.clientHeight < FOLLOW_THRESHOLD_PIXELS;
    const previousTop = scrollParent.scrollTop;
    this.list.replaceChildren();
    if (!this.taskId) {
      this.list.append(el("div", "trace-empty", "打开一个任务后，这里按时间列出它的每一步。"));
      return;
    }
    if (this.error) {
      this.list.append(el("div", "trace-empty error", this.error));
    }
    const entries = this.entries();
    if (entries.length === 0) {
      this.list.append(el("div", "trace-empty", this.data ? "还没有轨迹：发一句话之后，这里会列出每一步。" : "正在读取轨迹…"));
      return;
    }
    let previousTurn = null;
    let shown = 0;
    for (const entry of entries) {
      if (this.query && !matches(entry, this.query)) {
        continue;
      }
      const firstOfTurn = entry.turn !== previousTurn;
      previousTurn = entry.turn;
      this.list.append(this.row(entry, firstOfTurn));
      shown += 1;
    }
    if (shown === 0) {
      this.list.append(el("div", "trace-empty", "没有匹配的步骤"));
    }
    scrollParent.scrollTop = followBottom ? scrollParent.scrollHeight : previousTop;
  }

  row(entry, firstOfTurn) {
    const wrapper = el("div", `trace-item kind-${entry.kind}`);
    wrapper.dataset.entryId = String(entry.id);
    if (firstOfTurn && entry.turn > 0) {
      wrapper.classList.add("first-of-turn");
    }
    if (entry.id === this.selectedId) {
      wrapper.classList.add("selected");
    }
    const row = el("button", "trace-row");
    row.type = "button";
    const turnLabel = el("span", "trace-turn", firstOfTurn && entry.turn > 0 ? `${entry.turn} 轮` : "");
    const badge = el("span", `trace-badge kind-${entry.kind}`, KIND_LABELS[entry.kind] || entry.kind);
    row.append(turnLabel, badge, this.rowText(entry));
    const seconds = entry.start !== null && entry.start !== undefined ? durationSeconds(entry) : 0;
    if (seconds >= 0.05 || isRunning(entry)) {
      row.append(el("span", "trace-duration", isRunning(entry) ? "进行中" : formatSeconds(seconds)));
    }
    row.addEventListener("click", () => {
      if (this.expanded.has(entry.id)) {
        this.expanded.delete(entry.id);
      } else {
        this.expanded.add(entry.id);
      }
      this.selectedId = entry.id;
      this.renderList();
    });
    wrapper.append(row);
    if (this.expanded.has(entry.id)) {
      wrapper.append(this.detail(entry));
    }
    if (this.showChildren && entry.children?.length) {
      for (const child of entry.children) {
        const childRow = el("div", "trace-child");
        childRow.append(el("span", "trace-badge kind-child", "子调用"), el("span", "trace-child-name", child.name), el("span", "trace-child-summary", child.summary));
        wrapper.append(childRow);
      }
    }
    return wrapper;
  }

  rowText(entry) {
    const text = el("span", "trace-text");
    if (entry.kind === "tool") {
      text.classList.add("mono");
      text.append(el("strong", "", entry.name));
      if (entry.text) {
        text.append(el("span", "trace-args", ` ${entry.text}`));
      }
      if (entry.result) {
        text.append(el("span", "trace-arrow", " → "), el("span", entry.is_error || entry.denied ? "trace-result failed" : "trace-result", firstLine(entry.result)));
      }
      return text;
    }
    text.textContent = entry.text;
    return text;
  }

  detail(entry) {
    const detail = el("div", "trace-detail");
    if (entry.kind === "tool") {
      if (entry.arguments) {
        detail.append(el("div", "trace-detail-label", "参数"), el("pre", "trace-detail-text", entry.arguments));
      }
      detail.append(el("div", "trace-detail-label", entry.denied ? "被拒绝" : entry.is_error ? "出错" : "结果"));
      detail.append(el("pre", "trace-detail-text", entry.result || (isRunning(entry) ? "（还在执行）" : "（没有输出）")));
    } else {
      detail.append(el("pre", "trace-detail-text", entry.detail || entry.text));
    }
    const meta = [];
    if (entry.kind === "assistant") {
      if (entry.name) {
        meta.push(entry.name);
      }
      if (entry.reasoning_tokens || entry.output_tokens) {
        meta.push(`思考 ${entry.reasoning_tokens} / 输出 ${entry.output_tokens} token`);
      }
    }
    if (meta.length) {
      detail.append(el("div", "trace-detail-meta", meta.join(" · ")));
    }
    return detail;
  }

  // 导出原文：取这个任务每次调用的完整请求和回复，拼成 JSONL 让浏览器 / 桌面端存下来
  async exportCalls() {
    const taskId = this.taskId;
    if (!taskId) {
      return;
    }
    this.notice = "正在导出…";
    this.render();
    try {
      const { calls } = await requestJson(`/api/sessions/${encodeURIComponent(taskId)}/calls`);
      if (!calls?.length) {
        this.notice = "这个任务没有模型调用原文可以导出";
      } else {
        downloadText(`pocketagent-calls-${taskId}.jsonl`, calls.map((call) => JSON.stringify(call)).join("\n") + "\n");
        this.notice = `已导出 ${calls.length} 次调用`;
      }
    } catch (error) {
      this.notice = `导出失败：${error.message || error}`;
    }
    this.render();
    window.setTimeout(() => {
      this.notice = "";
      this.render();
    }, 4000);
  }

  // 时间轴上点了一个色块：列表里选中那一步并滚过去（被搜索过滤掉的先清掉搜索）
  select(entryId) {
    this.selectedId = entryId;
    let row = this.list.querySelector(`[data-entry-id="${entryId}"]`);
    if (!row && this.query) {
      this.query = "";
      this.search.value = "";
    }
    this.renderList();
    row = this.list.querySelector(`[data-entry-id="${entryId}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }
}

// 手机 App（android/）的 WebView 存不了 blob 下载。那边挂了一个原生接口，把内容交给它写进「下载」目录。
// 口令是 App 在页面加载完后注入的（见 MainActivity.AndroidBridge）；别的环境没有这个接口，返回 false。
function saveThroughAndroidApp(fileName, text) {
  const bridge = window.PocketAgentAndroid;
  if (!bridge || typeof bridge.saveTextFile !== "function") {
    return false;
  }
  return bridge.saveTextFile(window.__pocketAgentBridgeKey || "", fileName, text) === true;
}

function downloadText(fileName, text) {
  if (saveThroughAndroidApp(fileName, text)) {
    return;
  }
  const url = URL.createObjectURL(new Blob([text], { type: "application/x-ndjson" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function durationSeconds(entry) {
  const end = entry.end ?? Date.now() / 1000;
  return Math.max(0, end - entry.start);
}

function isRunning(entry) {
  return entry.kind === "tool" && (entry.end === null || entry.end === undefined);
}

function formatSeconds(seconds) {
  if (seconds < 60) {
    return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

function blockTitle(entry) {
  const parts = [KIND_LABELS[entry.kind] || entry.kind];
  if (entry.kind === "tool" && entry.name) {
    parts.push(entry.name);
  }
  if (entry.start !== null && entry.start !== undefined) {
    parts.push(isRunning(entry) ? "进行中" : formatSeconds(durationSeconds(entry)));
  }
  if (entry.kind === "assistant" && (entry.reasoning_tokens || entry.output_tokens)) {
    parts.push(`思考 ${entry.reasoning_tokens} / 输出 ${entry.output_tokens} token`);
  }
  return `${parts.join(" · ")}\n${entry.text}`;
}

function firstLine(text) {
  const line = String(text || "").split("\n").find((part) => part.trim()) || "";
  return line.length > 200 ? `${line.slice(0, 200)}…` : line;
}

function matches(entry, query) {
  const haystack = [entry.text, entry.detail, entry.name, entry.arguments, entry.result, ...(entry.children || []).map((child) => `${child.name} ${child.summary}`)]
    .join("\n")
    .toLowerCase();
  return haystack.includes(query);
}

// 右侧栏顶部的「概览 / 轨迹」页签。load / save 是各端自己的本地偏好读写；onChange(tab) 在切换后调
export function initPanelTabs(panel, { load, save, onChange }) {
  const tabs = [...panel.querySelectorAll("[data-panel-tab]")];
  const panes = [...panel.querySelectorAll("[data-panel-pane]")];
  const select = (name, remember = true) => {
    for (const tab of tabs) {
      const active = tab.dataset.panelTab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    }
    for (const pane of panes) {
      pane.classList.toggle("hidden", pane.dataset.panelPane !== name);
    }
    panel.classList.toggle("trace-active", name === "trace");
    if (remember) {
      save("rightPanelTab", name);
    }
    onChange(name);
  };
  for (const tab of tabs) {
    tab.addEventListener("click", () => select(tab.dataset.panelTab));
  }
  const saved = load("rightPanelTab", "overview");
  select(tabs.some((tab) => tab.dataset.panelTab === saved) ? saved : "overview", false);
  return select;
}
