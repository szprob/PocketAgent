// 右侧栏：任务概要（token 用量、上下文）、文件变更（点开看 diff、可撤销）、产出（新建的文件）、检查点。
// 变更来自影子 git：任务开始时给工作区拍一张，之后不管是 edit_file 还是 bash 改的都看得到。

import { requestJson, sendJson } from "./api.js";
import { button, copyText, el, formatClock, formatTokens, icon, renderDiff, workspaceName } from "./dom.js";
import { emit, on, savePreference } from "./state.js";

const elements = {};
const panel = {
  taskId: null,
  status: null,
  usage: null,
  latestReply: "",
  updatedAt: null,
  changes: [],
  checkpoints: [],
  checkpointsEnabled: true,
  checkpointsDisabledReason: "",
  expandedPath: null,
  refreshTimer: null,
  taskRefreshTimer: null,
};

const STATUS_LABELS = { added: "新建", modified: "修改", deleted: "删除" };
const CHECKPOINT_KINDS = { baseline: "任务开始", turn: "这一轮之前", safety: "撤销 / 恢复之前" };

export function initRightPanel() {
  elements.overview = document.getElementById("overview-card");
  elements.changeList = document.getElementById("change-list");
  elements.changesCount = document.getElementById("changes-count");
  elements.outputList = document.getElementById("output-list");
  elements.checkpointList = document.getElementById("checkpoint-list");
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
  panel.checkpoints = [];
  panel.checkpointsEnabled = true;
  panel.checkpointsDisabledReason = "";
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
    // 只要概要（用量、最新回复），不拉整份历史
    const overview = await requestJson(`api/sessions/${encodeURIComponent(taskId)}/overview`);
    if (taskId !== panel.taskId) {
      return;
    }
    panel.usage = overview.usage;
    panel.latestReply = overview.latest_reply || panel.latestReply;
    panel.updatedAt = overview.updated_at || panel.updatedAt;
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
      applyChangesPayload(result);
      renderChanges();
    }
  } catch (error) {
    // 任务刚被删掉之类，忽略
  }
}

function applyChangesPayload(result) {
  panel.changes = result.changes;
  panel.checkpoints = result.checkpoints || [];
  panel.checkpointsEnabled = result.enabled;
  panel.checkpointsDisabledReason = result.disabled_reason || "";
}

function fullPath(relativePath) {
  const workspace = panel.status?.workspace || "";
  return workspace ? `${workspace.replace(/\/$/, "")}/${relativePath}` : relativePath;
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
    // 临时任务的工作区是 ~/.pocketagent/scratch 下的目录，直接显示目录名看不出是什么
    overviewRow(
      "工作区",
      status.ephemeral ? "临时任务" : workspaceName(status.workspace),
      status.ephemeral ? `临时目录 ${status.workspace}` : status.workspace
    ),
    overviewRow("模型", status.model)
  );
  if (panel.usage) {
    const usage = panel.usage;
    rows.appendChild(
      overviewRow(
        "本任务用量",
        `${formatTokens(usage.total)} tokens · ${usage.calls} 次`,
        `输入 ${usage.prompt}，输出 ${usage.completion}，思考 ${usage.reasoning}${usage.estimated_calls ? `（其中 ${usage.estimated_calls} 次是估算）` : ""}`
      )
    );
    rows.appendChild(overviewRow("缓存命中", cacheHitText(usage), "输入里命中缓存的比例；有的接口不缓存或不报，就是 0"));
    if (usage.cost > 0) {
      rows.appendChild(overviewRow("费用", `$${usage.cost.toFixed(4)}`, "接口直接报的费用（OpenRouter 会报）"));
    }
  }
  const percent = status.context_window ? Math.round((status.context_tokens / status.context_window) * 100) : 0;
  rows.appendChild(overviewRow("上下文", `${percent}%（约 ${formatTokens(status.context_tokens)} / ${formatTokens(status.context_window)}）`));
  card.appendChild(rows);
}

function cacheHitText(usage) {
  if (usage.cache_hit_rate === null || usage.cache_hit_rate === undefined) {
    return "—";
  }
  return `${Math.round(usage.cache_hit_rate * 100)}%（${formatTokens(usage.cached)}）`;
}

// ---- 变更和产出 ----

function renderChanges() {
  elements.changeList.replaceChildren();
  elements.outputList.replaceChildren();
  elements.changesCount.textContent = panel.changes.length ? String(panel.changes.length) : "";
  if (!panel.checkpointsEnabled) {
    elements.changeList.appendChild(el("div", "panel-empty", panel.checkpointsDisabledReason || "检查点关掉了，看不到变更"));
  } else if (panel.changes.length === 0) {
    elements.changeList.appendChild(el("div", "panel-empty", "从任务开始到现在，工作区里还没有文件变化"));
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
  renderCheckpoints();
}

function renderChangeRow(change) {
  const wrapper = el("div", "change-item");
  const row = el("button", "change-row");
  row.type = "button";
  row.title = fullPath(change.path);
  const counts = el("span", "change-counts");
  if (change.binary) {
    counts.textContent = "二进制";
  } else {
    counts.append(el("span", "count-add", `+${change.additions}`), el("span", "count-remove", `−${change.deletions}`));
  }
  row.append(el("span", `change-status ${change.status}`, STATUS_LABELS[change.status] || change.status), el("span", "change-path", change.path), counts);
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
  const action = change.status === "added" ? "删除这个新建的文件" : "把文件恢复成任务开始时的样子";
  if (!window.confirm(`${action}？\n${change.path}\n\n撤销前会先拍一张检查点，撤销本身也能撤销。`)) {
    return;
  }
  try {
    const result = await sendJson(`api/sessions/${encodeURIComponent(panel.taskId)}/changes/revert`, "POST", { path: change.path });
    applyChangesPayload(result);
    panel.expandedPath = null;
    renderChanges();
    emit("files-changed", panel.taskId);
  } catch (error) {
    window.alert(`撤销失败：${error.message}`);
  }
}

function renderOutputRow(output) {
  const row = el("div", "output-row");
  const copy = button("icon-button small", "", "copy", "复制路径");
  copy.addEventListener("click", async () => {
    const copied = await copyText(fullPath(output.path));
    copy.title = copied ? "已复制" : "复制失败";
  });
  row.append(icon("file", "output-icon"), el("span", "output-path", output.path), copy);
  row.title = fullPath(output.path);
  return row;
}

// ---- 检查点 ----

function renderCheckpoints() {
  elements.checkpointList.replaceChildren();
  if (panel.checkpoints.length === 0) {
    elements.checkpointList.appendChild(el("div", "panel-empty", "每一轮开始前会给工作区拍一张，可以整体恢复到那时"));
    return;
  }
  // 新的在上面
  for (const checkpoint of [...panel.checkpoints].reverse()) {
    elements.checkpointList.appendChild(renderCheckpointRow(checkpoint));
  }
}

function renderCheckpointRow(checkpoint) {
  const row = el("div", "checkpoint-row");
  const text = el("div", "checkpoint-text");
  text.append(
    el("div", "checkpoint-label", checkpoint.label),
    el("div", "checkpoint-meta", `${CHECKPOINT_KINDS[checkpoint.kind] || checkpoint.kind} · ${formatClock(checkpoint.created_at)}`)
  );
  text.title = `${checkpoint.label}\n${checkpoint.commit.slice(0, 12)}`;
  const restore = button("ghost-button small", "恢复到这里");
  restore.addEventListener("click", () => restoreCheckpoint(checkpoint));
  row.append(text, restore);
  return row;
}

async function restoreCheckpoint(checkpoint) {
  const message = `把整个工作区恢复到「${checkpoint.label}」时的样子？\n\n恢复前会先拍一张「恢复之前的样子」，恢复本身也能撤销。`;
  if (!window.confirm(message)) {
    return;
  }
  try {
    const result = await sendJson(`api/sessions/${encodeURIComponent(panel.taskId)}/checkpoints/restore`, "POST", { commit: checkpoint.commit });
    applyChangesPayload(result);
    panel.expandedPath = null;
    renderChanges();
    emit("files-changed", panel.taskId);
  } catch (error) {
    window.alert(`恢复失败：${error.message}`);
  }
}

