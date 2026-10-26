// 右侧栏：任务概要（token 用量、上下文）、文件变更（点开看 diff、可撤销）、产出（新建的文件）。

import { requestJson, sendJson } from "./api.js";
import { button, copyText, el, formatClock, formatTokens, icon, renderDiff, workspaceName } from "./dom.js";
import { on, savePreference, state } from "./state.js";

const elements = {};
const panel = {
  taskId: null,
  status: null,
  usage: null,
  latestReply: "",
  updatedAt: null,
  changes: [],
  expandedPath: null,
  refreshTimer: null,
  taskRefreshTimer: null,
};

const STATUS_LABELS = { added: "新建", modified: "修改", deleted: "删除" };

export function initRightPanel() {
  elements.overview = document.getElementById("overview-card");
  elements.changeList = document.getElementById("change-list");
  elements.changesCount = document.getElementById("changes-count");
  elements.outputList = document.getElementById("output-list");
  document.getElementById("refresh-changes").addEventListener("click", () => refreshChanges());

  on("task-status", (status) => {
    panel.status = status;
    renderOverview();
  });
  on("turn-ended", ({ taskId, finalText }) => {
    if (taskId !== panel.taskId) {
      return;
    }
    if (finalText) {
      panel.latestReply = finalText;
    }
    panel.updatedAt = new Date().toISOString();
    refreshTask();
  });
  on("files-changed", (taskId) => {
    if (taskId === panel.taskId) {
      scheduleChangesRefresh();
    }
  });
  // 一轮里每次模型回复后刷新用量，长任务跑着时也能看到 token 在涨
  on("model-responded", (taskId) => {
    if (taskId === panel.taskId) {
      scheduleTaskRefresh();
    }
  });
}

export function setRightPanelVisible(visible, { remember = true } = {}) {
  document.getElementById("app").classList.toggle("right-panel-hidden", !visible);
  if (remember) {
    savePreference("rightPanel", visible);
  }
}

export function showTaskInPanel(taskId) {
  panel.taskId = taskId;
  panel.status = null;
  panel.usage = null;
  panel.latestReply = "";
  panel.updatedAt = null;
  panel.changes = [];
  panel.expandedPath = null;
  renderOverview();
  renderChanges();
  refreshTask();
}

async function refreshTask() {
  const taskId = panel.taskId;
  if (!taskId) {
    return;
  }
  try {
    const snapshot = await requestJson(`api/sessions/${encodeURIComponent(taskId)}`);
    if (taskId !== panel.taskId) {
      return;
    }
    panel.usage = snapshot.usage;
    const replies = snapshot.items.filter((item) => item.kind === "assistant");
    panel.latestReply = panel.latestReply || replies.at(-1)?.text || "";
    const task = state.tasks.find((each) => each.id === taskId);
    panel.updatedAt = panel.updatedAt || task?.updated_at || null;
  } catch (error) {
    return;
  }
  renderOverview();
  await refreshChanges();
}

function scheduleTaskRefresh() {
  clearTimeout(panel.taskRefreshTimer);
  panel.taskRefreshTimer = setTimeout(refreshTask, 600);
}

function scheduleChangesRefresh() {
  clearTimeout(panel.refreshTimer);
  panel.refreshTimer = setTimeout(refreshChanges, 400);
}

async function refreshChanges() {
  const taskId = panel.taskId;
  if (!taskId) {
    return;
  }
  try {
    const result = await requestJson(`api/sessions/${encodeURIComponent(taskId)}/changes`);
    if (taskId === panel.taskId) {
      panel.changes = result.changes;
      renderChanges();
    }
  } catch (error) {
    // 任务刚被删掉之类，忽略
  }
}

// ---- 概要 ----

function overviewRow(label, value, title) {
  const row = el("div", "overview-row");
  const valueElement = el("span", "overview-value", value);
  if (title) {
    valueElement.title = title;
  }
  row.append(el("span", "overview-label", label), valueElement);
  return row;
}

function renderOverview() {
  const card = elements.overview;
  card.replaceChildren();
  const status = panel.status;
  if (!status) {
    card.appendChild(el("div", "muted", "加载中…"));
    return;
  }
  const head = el("div", "overview-head");
  head.append(icon("sparkle", "overview-icon"), el("span", "overview-updated", panel.updatedAt ? `更新于 ${formatClock(panel.updatedAt)}` : "新任务"));
  card.append(head, el("div", "overview-title", status.title));
  if (panel.latestReply) {
    card.appendChild(el("div", "overview-reply", panel.latestReply.replace(/[#*`>|]/g, "").slice(0, 160)));
  }
  const rows = el("div", "overview-rows");
  rows.append(
    overviewRow("工作区", workspaceName(status.workspace), status.workspace),
    overviewRow("模型", status.model)
  );
  if (panel.usage) {
    const usage = panel.usage;
    rows.appendChild(
      overviewRow(
        "本任务用量",
        `${formatTokens(usage.total)} tokens · ${usage.calls} 次`,
        `输入 ${usage.prompt}，输出 ${usage.completion}${usage.estimated_calls ? `（其中 ${usage.estimated_calls} 次是估算）` : ""}`
      )
    );
  }
  const percent = status.context_window ? Math.round((status.context_tokens / status.context_window) * 100) : 0;
  rows.appendChild(overviewRow("上下文", `${percent}%（约 ${formatTokens(status.context_tokens)} / ${formatTokens(status.context_window)}）`));
  card.appendChild(rows);
}

// ---- 变更和产出 ----

function renderChanges() {
  elements.changeList.replaceChildren();
  elements.outputList.replaceChildren();
  elements.changesCount.textContent = panel.changes.length ? String(panel.changes.length) : "";
  if (panel.changes.length === 0) {
    elements.changeList.appendChild(el("div", "panel-empty", "这个任务还没改过文件（只统计 edit_file / write_file）"));
  }
  for (const change of panel.changes) {
    elements.changeList.appendChild(renderChangeRow(change));
  }
  const outputs = panel.changes.filter((change) => change.status === "added");
  if (outputs.length === 0) {
    elements.outputList.appendChild(el("div", "panel-empty", "新建的文件会列在这里"));
  }
  for (const output of outputs) {
    elements.outputList.appendChild(renderOutputRow(output));
  }
}

function renderChangeRow(change) {
  const wrapper = el("div", "change-item");
  const row = el("button", "change-row");
  row.type = "button";
  row.title = change.path;
  const counts = el("span", "change-counts");
  if (change.binary) {
    counts.textContent = "二进制";
  } else {
    counts.append(el("span", "count-add", `+${change.additions}`), el("span", "count-remove", `−${change.deletions}`));
  }
  row.append(el("span", `change-status ${change.status}`, STATUS_LABELS[change.status] || change.status), el("span", "change-path", change.display_path), counts);
  row.addEventListener("click", () => {
    panel.expandedPath = panel.expandedPath === change.path ? null : change.path;
    renderChanges();
  });
  wrapper.appendChild(row);
  if (panel.expandedPath === change.path) {
    wrapper.appendChild(renderDiffPanel(change));
  }
  return wrapper;
}

function renderDiffPanel(change) {
  const container = el("div", "change-diff");
  container.appendChild(el("div", "muted", "加载 diff…"));
  const encodedTask = encodeURIComponent(panel.taskId);
  requestJson(`api/sessions/${encodedTask}/changes/diff?path=${encodeURIComponent(change.path)}`)
    .then((result) => {
      const revert = button("ghost-button small", "撤销这个文件的改动", "undo");
      revert.addEventListener("click", () => revertChange(change));
      container.replaceChildren(renderDiff(result.diff), revert);
    })
    .catch((error) => container.replaceChildren(el("div", "error-text", error.message)));
  return container;
}

async function revertChange(change) {
  const action = change.status === "added" ? "删除这个新建的文件" : "把文件恢复成这个任务改动之前的样子";
  if (!window.confirm(`${action}？\n${change.display_path}`)) {
    return;
  }
  try {
    const result = await sendJson(`api/sessions/${encodeURIComponent(panel.taskId)}/changes/revert`, "POST", { path: change.path });
    panel.changes = result.changes;
    panel.expandedPath = null;
    renderChanges();
  } catch (error) {
    window.alert(`撤销失败：${error.message}`);
  }
}

function renderOutputRow(output) {
  const row = el("div", "output-row");
  const copy = button("icon-button small", "", "copy", "复制路径");
  copy.addEventListener("click", async () => {
    const copied = await copyText(output.path);
    copy.title = copied ? "已复制" : "复制失败";
  });
  row.append(icon("file", "output-icon"), el("span", "output-path", output.display_path), copy);
  row.title = output.path;
  return row;
}

