// 右侧栏：任务概要（token 用量、上下文）、文件变更（点开看 diff、可撤销）、产出（新建的文件）、检查点。
// 变更来自影子 git：任务开始时给工作区拍一张，之后不管是 edit_file 还是 bash 改的都看得到。

import { requestJson, sendJson, sessionFileUrl } from "../core/api.js";
import { button, copyText, el, formatClock, formatNumber, formatTokens, icon, isImagePath, renderDiff, workspaceName } from "../ui/dom.js";
import { emit, loadPreference, on, savePreference } from "../core/state.js";

const elements = {};
const panel = {
  taskId: null,
  status: null,
  usage: null,
  turns: 0,
  latestReply: "",
  updatedAt: null,
  changes: [],
  checkpoints: [],
  checkpointsEnabled: true,
  checkpointsDisabledReason: "",
  expandedPath: null,
  refreshTimer: null,
  taskRefreshTimer: null,
  links: [],
  previewUrl: "",
};

const STATUS_LABELS = { added: "新建", modified: "修改", deleted: "删除" };
const CHECKPOINT_KINDS = { baseline: "任务开始", turn: "这一轮之前", safety: "撤销 / 恢复之前" };
const MAX_LINKS = 8;

export function initRightPanel() {
  elements.overview = document.getElementById("overview-card");
  elements.changeList = document.getElementById("change-list");
  elements.changesCount = document.getElementById("changes-count");
  elements.outputList = document.getElementById("output-list");
  elements.checkpointList = document.getElementById("checkpoint-list");
  document.getElementById("refresh-changes").addEventListener("click", () => refreshChanges());
  initPreview();

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
  // 命令输出里发现的本地服务地址，收进预览的候选链接
  on("task-links", ({ taskId, links }) => {
    if (taskId !== panel.taskId || links.length === 0) {
      return;
    }
    for (const link of links) {
      panel.links = [link, ...panel.links.filter((existing) => existing !== link)].slice(0, MAX_LINKS);
    }
    renderPreviewLinks();
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
  panel.turns = 0;
  panel.toolCalls = 0;
  panel.latestReply = "";
  panel.updatedAt = null;
  panel.changes = [];
  panel.checkpoints = [];
  panel.checkpointsEnabled = true;
  panel.checkpointsDisabledReason = "";
  panel.expandedPath = null;
  panel.links = [];
  renderOverview();
  renderChanges();
  renderPreviewLinks();
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
    panel.turns = overview.turns ?? 0;
    panel.toolCalls = overview.tool_calls ?? 0;
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

// detail 是数值下面那行小字：一行放不下「总和 + 拆分」的时候，拆成两行显示，比挤在一行被截断清楚
function overviewRow(label, value, title, detail) {
  const row = el("div", "overview-row");
  const values = el("div", "overview-values");
  values.appendChild(el("span", "overview-value", value));
  if (detail) {
    values.appendChild(el("span", "overview-detail", detail));
  }
  if (title) {
    values.title = title;
  }
  row.append(el("span", "overview-label", label), values);
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
  if (status.worktree) {
    rows.append(
      overviewRow("隔离分支", status.worktree.branch, `基线 ${status.worktree.base_commit}`),
      overviewRow("来源工作区", workspaceName(status.worktree.origin), status.worktree.origin)
    );
  }
  if (panel.usage) {
    const usage = panel.usage;
    rows.appendChild(
      overviewRow(
        "本任务用量",
        `输入 ${formatTokens(usage.prompt)} + 输出 ${formatTokens(usage.completion)}`,
        `输入 ${formatNumber(usage.prompt)} + 输出 ${formatNumber(usage.completion)}，共 ${formatNumber(usage.total)} tokens，${usage.calls} 次调用模型，思考 ${formatNumber(usage.reasoning)}${usage.estimated_calls ? `（其中 ${usage.estimated_calls} 次是估算）` : ""}`
      )
    );
    rows.appendChild(
      overviewRow(
        "总轮次",
        `${panel.turns} 轮 · 工具调用 ${panel.toolCalls} 次`,
        "用户说一句话到 agent 停下来算一轮（干活时的插话不算）；工具调用是这个任务里模型调工具的总次数，被拒绝的也算，子 agent 内部的不算"
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
  const meta = el("div", "output-meta");
  meta.append(icon("file", "output-icon"), el("span", "output-path", output.path), copy);
  if (isImagePath(output.path)) {
    const url = sessionFileUrl(panel.taskId, output.path);
    const link = el("a", "output-thumb");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = `打开原图：${output.path}`;
    const image = el("img");
    image.src = url;
    image.alt = output.path;
    image.loading = "lazy";
    link.appendChild(image);
    row.classList.add("with-image");
    row.append(link, meta);
  } else {
    row.appendChild(meta);
  }
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


// ---- 预览 ----

// iframe 里只能放 http/https，别的写法（javascript: 之类）一律当输入错误
function normalizePreviewUrl(raw) {
  const text = raw.trim();
  if (!text) {
    return "";
  }
  const url = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(text) ? text : `http://${text}`;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "";
    }
    return parsed.href;
  } catch {
    return "";
  }
}

function initPreview() {
  elements.previewInput = document.getElementById("preview-url");
  elements.previewLinks = document.getElementById("preview-links");
  elements.previewFrameWrap = document.getElementById("preview-frame-wrap");
  elements.previewFrame = document.getElementById("preview-frame");
  elements.previewEmpty = document.getElementById("preview-empty");
  elements.previewOpenWindow = document.getElementById("preview-open-window");

  elements.previewInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      openPreview(elements.previewInput.value);
    }
  });
  document.getElementById("preview-go").addEventListener("click", () => openPreview(elements.previewInput.value));
  elements.previewOpenWindow.addEventListener("click", () => {
    window.open(panel.previewUrl, "_blank", "noopener");
  });

  // 上次预览的地址带回来，切换任务后不用重新输
  panel.previewUrl = loadPreference("previewUrl", "");
  elements.previewInput.value = panel.previewUrl;
  if (panel.previewUrl) {
    elements.previewFrame.src = panel.previewUrl;
  }
  renderPreviewLinks();
}

function openPreview(raw) {
  const url = normalizePreviewUrl(raw);
  if (!url) {
    elements.previewInput.focus();
    return;
  }
  panel.previewUrl = url;
  elements.previewInput.value = url;
  savePreference("previewUrl", url);
  elements.previewFrame.src = url;
  renderPreviewLinks();
}

function renderPreviewLinks() {
  elements.previewLinks.replaceChildren();
  elements.previewLinks.classList.toggle("hidden", panel.links.length === 0);
  for (const link of panel.links) {
    const chip = el("button", "preview-link", link.replace(/^https?:\/\//, ""));
    chip.type = "button";
    chip.title = `打开 ${link}`;
    chip.addEventListener("click", () => openPreview(link));
    elements.previewLinks.appendChild(chip);
  }
  const hasPreview = Boolean(panel.previewUrl);
  elements.previewFrameWrap.classList.toggle("hidden", !hasPreview);
  elements.previewOpenWindow.classList.toggle("hidden", !hasPreview);
  elements.previewEmpty.classList.toggle("hidden", hasPreview);
}
