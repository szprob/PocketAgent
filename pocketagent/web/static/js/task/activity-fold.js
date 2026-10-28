// 一轮里的工具调用和思考收进一个可折叠的块：AI 说的话永远在外面，
// 说了话就把这一块收尾，后面再开新的一块。折叠头上一行预览说清这块干了什么。
// 和桌面端 renderer/activity-fold.js 是同一套逻辑，改一边记得改另一边。

import { el, icon } from "../ui/dom.js";

// 折叠头上「思考中 / 正在运行」后面那句最多显示几个字
const LIVE_PREVIEW_CHARS = 40;

// 文件类工具按路径去重：同一个文件读两次算一个
const FILE_BUCKETS = {
  read_file: "read",
  write_file: "write",
  edit_file: "edit",
  delete_file: "delete",
  remove_file: "delete",
};

// 其余工具记成次数
const COUNT_BUCKETS = {
  bash: "run",
  glob: "search",
  grep: "search",
  list_dir: "search",
  web_search: "web",
  web_fetch: "web",
};

// 自己另有界面的工具不统计：任务计划有面板、提问有问答卡
const HIDDEN_TOOLS = new Set(["todo_write", "ask_user"]);

// 预览里各类的先后顺序：先说动了哪些文件，再说跑了什么
const BUCKET_LABELS = [
  ["read", (count) => `读取 ${count} 个文件`],
  ["created", (count) => `新建 ${count} 个文件`],
  ["edited", (count) => `修改 ${count} 个文件`],
  ["deleted", (count) => `删除 ${count} 个文件`],
  ["run", (count) => `运行 ${count} 条命令`],
  ["search", (count) => `查找 ${count} 次`],
  ["web", (count) => `联网 ${count} 次`],
  ["other", (count) => `其他 ${count} 次`],
];

export class ActivityFold {
  constructor() {
    this.element = el("details", "activity-fold");
    const header = el("summary", "fold-header");
    this.mark = el("span", "fold-mark", "●");
    this.title = el("span", "fold-title");
    this.live = el("span", "fold-live");
    this.note = el("span", "fold-note");
    this.chevron = icon("chevronRight", "fold-chevron");
    header.append(this.mark, this.title, this.live, this.note, this.chevron);
    this.body = el("div", "fold-body");
    this.element.append(header, this.body);

    this.readPaths = new Set();
    this.createdPaths = new Set();
    this.editedPaths = new Set();
    this.deletedPaths = new Set();
    this.counts = { run: 0, search: 0, web: 0, other: 0 };
    this.failures = 0;
    // 还没拿到结果的调用：call_id → { name, path }
    this.calls = new Map();
    // 正在跑的调用：call_id → 折叠头上显示的那一句
    this.running = new Map();
    this.thinking = null;
    this.thinkingSeconds = 0;
    this.thinkingChars = 0;
    // 这一批工具从什么时候开始跑的，用来在折叠头上跳秒
    this.stepStartedAt = 0;
    this.timer = null;
    this.refresh();
  }

  append(node) {
    this.body.append(node);
  }

  isEmpty() {
    return this.body.children.length === 0;
  }

  // ---- 工具调用 ----

  recordCall(callId, name, summary) {
    if (HIDDEN_TOOLS.has(name)) {
      return;
    }
    if (!this.running.size) {
      this.stepStartedAt = performance.now();
    }
    this.calls.set(callId, { name, path: String(summary || "") });
    this.running.set(callId, callLabel(name, summary));
    this.startClock();
    this.refresh();
  }

  recordResult(callId, { name, content, isError, denied }) {
    if (HIDDEN_TOOLS.has(name)) {
      return;
    }
    const call = this.calls.get(callId) || { name, path: "" };
    this.calls.delete(callId);
    this.running.delete(callId);
    if (!this.running.size) {
      this.stepStartedAt = 0;
    }
    if (denied || isError) {
      // 出错的不算进「读了几个 / 改了几个」，单独记一笔，免得预览骗人
      this.failures += 1;
    } else {
      this.countCall(call, String(content || ""));
    }
    this.refresh();
  }

  countCall(call, content) {
    const fileBucket = FILE_BUCKETS[call.name];
    if (fileBucket === "read") {
      this.readPaths.add(call.path);
      return;
    }
    if (fileBucket === "edit") {
      this.editedPaths.add(call.path);
      return;
    }
    if (fileBucket === "delete") {
      this.deletedPaths.add(call.path);
      return;
    }
    if (fileBucket === "write") {
      // write_file 的回执里说清了是新建还是覆盖
      if (content.startsWith("已新建")) {
        this.createdPaths.add(call.path);
      } else {
        this.editedPaths.add(call.path);
      }
      return;
    }
    const bucket = COUNT_BUCKETS[call.name] || "other";
    this.counts[bucket] += 1;
  }

  // ---- 思考 ----

  startThinking() {
    if (this.thinking) {
      return;
    }
    this.thinking = { startedAt: performance.now(), chars: 0, tail: "" };
    this.startClock();
    this.refresh();
  }

  updateThinking(delta) {
    if (!this.thinking) {
      this.startThinking();
    }
    this.thinking.chars += delta.length;
    this.thinking.tail = (this.thinking.tail + delta).slice(-LIVE_PREVIEW_CHARS * 2);
    this.refresh();
  }

  finishThinking() {
    if (!this.thinking) {
      return;
    }
    this.thinkingSeconds = (performance.now() - this.thinking.startedAt) / 1000;
    this.thinkingChars = this.thinking.chars;
    this.thinking = null;
    this.refresh();
  }

  // ---- 收尾和重绘 ----

  // 这一块不再是「当前这一块」了：停表，跑完没跑完都定格
  finish() {
    this.stopClock();
    this.refresh();
  }

  isRunning() {
    return Boolean(this.thinking) || this.running.size > 0;
  }

  summaryParts() {
    const parts = [];
    const sizes = {
      read: this.readPaths.size,
      created: this.createdPaths.size,
      edited: this.editedPaths.size,
      deleted: this.deletedPaths.size,
      ...this.counts,
    };
    for (const [bucket, label] of BUCKET_LABELS) {
      if (sizes[bucket]) {
        parts.push(label(sizes[bucket]));
      }
    }
    return parts;
  }

  fallbackTitle() {
    // 正在思考时标题留空，让下面那句「思考中 3.2s · 后面的话」独占一行
    if (this.thinking) {
      return "";
    }
    if (this.running.size > 0) {
      return "正在调用工具";
    }
    if (this.thinkingSeconds) {
      return `思考了 ${this.thinkingSeconds.toFixed(0)}s（${this.thinkingChars} 字）`;
    }
    return "工具调用";
  }

  liveText() {
    if (this.thinking) {
      const seconds = ((performance.now() - this.thinking.startedAt) / 1000).toFixed(1);
      const tail = this.thinking.tail.replace(/\s+/g, " ").trim().slice(-LIVE_PREVIEW_CHARS);
      return `思考中 ${seconds}s${tail ? ` · ${tail}` : ""}`;
    }
    if (this.running.size > 0) {
      const labels = [...this.running.values()];
      const more = labels.length > 1 ? ` 等 ${labels.length} 个` : "";
      const seconds = this.stepStartedAt ? ((performance.now() - this.stepStartedAt) / 1000).toFixed(1) : "";
      return `${labels[labels.length - 1]}${more}${seconds ? ` · ${seconds}s` : ""}`;
    }
    return "";
  }

  refresh() {
    const parts = this.summaryParts();
    this.title.textContent = parts.length ? parts.join(" · ") : this.fallbackTitle();
    this.live.textContent = this.liveText();
    this.note.textContent = this.failures ? `${this.failures} 个出错` : "";
    const state = this.failures > 0 ? "error" : this.isRunning() ? "running" : "done";
    this.element.dataset.state = state;
    this.mark.textContent = state === "running" ? "●" : state === "error" ? "✗" : "✓";
    if (!this.isRunning()) {
      this.stopClock();
    }
  }

  startClock() {
    if (this.timer) {
      return;
    }
    this.timer = setInterval(() => this.refresh(), 200);
  }

  stopClock() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

function callLabel(name, summary) {
  const text = String(summary || "").trim();
  return text ? `${name} · ${text}` : name;
}

// 关掉这个选项时把已经画出来的折叠摊平：内容按原顺序放回对话里
export function dissolveFolds(container) {
  for (const fold of container.querySelectorAll(".activity-fold")) {
    const body = fold.querySelector(".fold-body");
    for (const child of [...body.children]) {
      fold.parentNode.insertBefore(child, fold);
    }
    fold.remove();
  }
}
