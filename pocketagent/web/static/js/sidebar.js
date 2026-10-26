// 左侧栏：新任务、自动化、工作区（点开看这个工作区的任务）、最近任务（全部历史），底部是账户和设置。

import { sendJson } from "./api.js";
import { button, el, icon, relativeTime, show, workspaceName } from "./dom.js";
import { on, reloadInfo, savePreference, state } from "./state.js";

const RECENT_TASK_LIMIT = 40;
const WORKSPACE_TASK_LIMIT = 8;

const elements = {};
let callbacks = {};
// 当前所在的页面，用来高亮：new / automations / task
let activeView = "new";

export function initSidebar(handlers) {
  callbacks = handlers;
  elements.workspaceList = document.getElementById("workspace-list");
  elements.recentList = document.getElementById("recent-list");
  elements.navNewTask = document.getElementById("nav-new-task");
  elements.navAutomations = document.getElementById("nav-automations");
  elements.accountName = document.getElementById("account-name");
  elements.avatar = document.getElementById("avatar");

  elements.navNewTask.addEventListener("click", () => callbacks.onNewTask(null));
  elements.navAutomations.addEventListener("click", () => callbacks.onAutomations());
  document.getElementById("account-button").addEventListener("click", () => callbacks.onSettings("usage"));
  document.getElementById("open-settings").addEventListener("click", () => callbacks.onSettings("models"));
  document.getElementById("add-workspace").addEventListener("click", openWorkspaceDialog);
  bindWorkspaceDialog();

  on("tasks-changed", render);
  on("info-changed", render);
}

export function setActiveView(view) {
  activeView = view;
  render();
}

function render() {
  if (!state.info) {
    return;
  }
  elements.navNewTask.classList.toggle("active", activeView === "new");
  elements.navAutomations.classList.toggle("active", activeView === "automations");
  renderAccount();
  renderWorkspaces();
  renderRecentTasks();
}

function renderAccount() {
  const name = state.info.user_name;
  elements.accountName.textContent = name;
  elements.avatar.textContent = (name || "?").slice(0, 1).toUpperCase();
}

// ---- 任务行 ----

function taskRow(task, showWorkspace) {
  const row = el("button", "task-row");
  row.type = "button";
  row.classList.toggle("active", activeView === "task" && task.id === state.currentTaskId);
  const title = el("span", "task-title", task.title);
  const meta = el("span", "task-meta");
  if (showWorkspace) {
    meta.textContent = `${workspaceName(task.workspace)} · ${relativeTime(task.updated_at)}`;
  } else {
    meta.textContent = relativeTime(task.updated_at);
  }
  const text = el("span", "task-text");
  text.append(title, meta);
  if (task.source && task.source.startsWith("automation:")) {
    row.appendChild(icon("clock", "task-kind"));
  }
  row.appendChild(text);
  if (task.running) {
    row.appendChild(el("span", "running-dot"));
    row.title = "运行中";
  }
  row.addEventListener("click", () => callbacks.onOpenTask(task.id));
  return row;
}

// ---- 工作区 ----

function tasksIn(workspacePath) {
  return state.tasks.filter((task) => task.workspace === workspacePath);
}

function renderWorkspaces() {
  elements.workspaceList.replaceChildren();
  if (state.info.workspaces.length === 0) {
    elements.workspaceList.appendChild(el("div", "sidebar-empty", "还没有工作区，点 + 添加"));
    return;
  }
  for (const workspace of state.info.workspaces) {
    elements.workspaceList.appendChild(renderWorkspace(workspace));
  }
}

function renderWorkspace(workspace) {
  const group = el("div", "workspace-group");
  const expanded = state.expandedWorkspaces.has(workspace.path);
  const tasks = tasksIn(workspace.path);
  const header = el("div", "workspace-row");
  header.classList.toggle("missing", !workspace.exists);
  header.title = workspace.exists ? workspace.path : `${workspace.path}（目录不存在）`;
  const toggle = el("button", "workspace-toggle");
  toggle.type = "button";
  toggle.append(icon(expanded ? "chevronDown" : "chevronRight", "workspace-chevron"), icon("folder"), el("span", "workspace-name", workspace.name));
  if (tasks.some((task) => task.running)) {
    toggle.appendChild(el("span", "running-dot"));
  }
  toggle.appendChild(el("span", "workspace-count", tasks.length ? String(tasks.length) : ""));
  toggle.addEventListener("click", () => {
    if (expanded) {
      state.expandedWorkspaces.delete(workspace.path);
    } else {
      state.expandedWorkspaces.add(workspace.path);
    }
    savePreference("expandedWorkspaces", [...state.expandedWorkspaces]);
    render();
  });
  const newTask = button("row-action", "", "plus", "在这个工作区开新任务");
  newTask.addEventListener("click", () => callbacks.onNewTask(workspace.path));
  const remove = button("row-action", "", "close", "从列表里移除（任务记录不删）");
  remove.addEventListener("click", () => removeWorkspace(workspace));
  header.append(toggle, newTask, remove);
  group.appendChild(header);

  if (expanded) {
    const list = el("div", "workspace-tasks");
    if (tasks.length === 0) {
      list.appendChild(el("div", "sidebar-empty", "还没有任务"));
    }
    for (const task of tasks.slice(0, WORKSPACE_TASK_LIMIT)) {
      list.appendChild(taskRow(task, false));
    }
    if (tasks.length > WORKSPACE_TASK_LIMIT) {
      list.appendChild(el("div", "sidebar-empty", `还有 ${tasks.length - WORKSPACE_TASK_LIMIT} 个，在「最近任务」里看`));
    }
    group.appendChild(list);
  }
  return group;
}

async function removeWorkspace(workspace) {
  if (!window.confirm(`从工作区列表里移除「${workspace.name}」？\n只是不显示，目录和任务记录都不动。`)) {
    return;
  }
  await sendJson("api/workspaces", "DELETE", { path: workspace.path });
  await reloadInfo();
}

// ---- 最近任务 ----

function renderRecentTasks() {
  elements.recentList.replaceChildren();
  if (state.tasks.length === 0) {
    elements.recentList.appendChild(el("div", "sidebar-empty", "还没有任务"));
    return;
  }
  for (const task of state.tasks.slice(0, RECENT_TASK_LIMIT)) {
    elements.recentList.appendChild(taskRow(task, true));
  }
}

// ---- 添加工作区 ----

function bindWorkspaceDialog() {
  elements.workspaceModal = document.getElementById("workspace-modal");
  elements.workspaceInput = document.getElementById("workspace-path-input");
  elements.workspaceError = document.getElementById("workspace-error");
  document.getElementById("workspace-cancel").addEventListener("click", closeWorkspaceDialog);
  document.getElementById("workspace-save").addEventListener("click", saveWorkspace);
  elements.workspaceInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      saveWorkspace();
    }
  });
  elements.workspaceModal.addEventListener("click", (event) => {
    if (event.target === elements.workspaceModal) {
      closeWorkspaceDialog();
    }
  });
}

function openWorkspaceDialog() {
  elements.workspaceInput.value = "";
  elements.workspaceError.textContent = "";
  show(elements.workspaceModal, true);
  elements.workspaceInput.focus();
}

function closeWorkspaceDialog() {
  show(elements.workspaceModal, false);
}

async function saveWorkspace() {
  const path = elements.workspaceInput.value.trim();
  if (!path) {
    return;
  }
  try {
    await sendJson("api/workspaces", "POST", { path });
  } catch (error) {
    elements.workspaceError.textContent = error.message;
    return;
  }
  closeWorkspaceDialog();
  const info = await reloadInfo();
  const added = info.workspaces.find((workspace) => workspace.path.endsWith(path.replace(/\/+$/, "").replace(/^~/, "")));
  if (added) {
    state.expandedWorkspaces.add(added.path);
  }
  callbacks.onNewTask(added ? added.path : null);
}
