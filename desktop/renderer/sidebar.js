// 左侧栏：新任务、自动化、工作区（点开是文件树）、最近任务，底部是账户和设置。

import { request, sendJson } from "./api.js";
import { button, el, formatRelativeTime, workspaceName } from "./dom.js";
import { icon } from "./icons.js";
import { openFilePreview } from "./filepreview.js";
import { initFolderPicker, openFolderPicker } from "./folderpicker.js";
import { loadPreference, on, savePreference, state } from "./state.js";

const RECENT_TASK_LIMIT = 50;
const WAITING_MARKS = {
  question: { mark: "?", label: "等你回答问题" },
  approval: { mark: "!", label: "等你确认操作" },
};

const elements = {};
let callbacks = {};
let activeView = "new";
let collapsedSections = new Set(loadPreference("collapsedSections", []));
let filesRefreshTimer = null;

// 文件树：工作区 →（目录相对路径 → 子项）；展开了的目录；正在加载的目录
const tree = {
  children: new Map(),
  expandedDirs: new Set(),
  loading: new Set(),
};

export function initSidebar(handlers) {
  callbacks = handlers;
  for (const path of loadPreference("expandedWorkspaces", [])) {
    state.expandedWorkspaces.add(path);
  }
  elements.workspaceList = document.getElementById("workspace-list");
  elements.recentList = document.getElementById("recent-list");
  elements.workspaceCount = document.getElementById("workspace-count");
  elements.recentCount = document.getElementById("recent-count");
  elements.navNewTask = document.getElementById("nav-new-task");
  elements.navAutomations = document.getElementById("nav-automations");
  elements.accountName = document.getElementById("account-name");
  elements.avatar = document.getElementById("avatar");

  elements.navNewTask.prepend(icon("plus"));
  elements.navAutomations.prepend(icon("clock"));
  document.getElementById("add-workspace").append(icon("plus"));
  document.getElementById("open-settings").append(icon("settings"));

  elements.navNewTask.addEventListener("click", () => callbacks.onNewTask(null));
  elements.navAutomations.addEventListener("click", () => callbacks.onAutomations());
  document.getElementById("account-button").addEventListener("click", () => callbacks.onSettings("usage"));
  document.getElementById("open-settings").addEventListener("click", () => callbacks.onSettings("general"));
  document.getElementById("add-workspace").addEventListener("click", openFolderPicker);
  initFolderPicker((addedPath) => {
    if (addedPath) {
      state.expandedWorkspaces.add(addedPath);
      saveExpanded();
    }
    callbacks.onWorkspaceAdded?.(addedPath);
  });

  for (const section of document.querySelectorAll(".sidebar-section")) {
    const toggle = section.querySelector(".section-toggle");
    const apply = () => {
      const isCollapsed = collapsedSections.has(section.dataset.section);
      section.classList.toggle("collapsed", isCollapsed);
      toggle.setAttribute("aria-expanded", String(!isCollapsed));
    };
    toggle.addEventListener("click", () => {
      if (collapsedSections.has(section.dataset.section)) {
        collapsedSections.delete(section.dataset.section);
      } else {
        collapsedSections.add(section.dataset.section);
      }
      savePreference("collapsedSections", [...collapsedSections]);
      apply();
    });
    apply();
  }

  on("tasks-changed", () => render());
  on("info-changed", () => render());
  on("files-changed", (taskId) => {
    clearTimeout(filesRefreshTimer);
    filesRefreshTimer = setTimeout(() => {
      const task = state.tasks.find((each) => each.id === taskId);
      const workspaces = task ? [task.workspace] : [...state.expandedWorkspaces];
      workspaces.forEach(reloadLoadedDirectories);
    }, 800);
  });
}

export function setActiveView(view) {
  activeView = view;
  render();
}

function saveExpanded() {
  savePreference("expandedWorkspaces", [...state.expandedWorkspaces]);
}

function treeKey(workspacePath, relativePath) {
  return `${workspacePath}\u0000${relativePath}`;
}

export function render() {
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
  const name = state.info.user_name || "我";
  elements.accountName.textContent = name;
  elements.avatar.textContent = name.slice(0, 1).toUpperCase();
}

// ---- 工作区 + 文件树 ----

function renderWorkspaces() {
  elements.workspaceList.replaceChildren();
  const workspaces = state.info.workspaces || [];
  elements.workspaceCount.textContent = workspaces.length ? String(workspaces.length) : "";
  if (workspaces.length === 0) {
    elements.workspaceList.append(el("div", "sidebar-empty", "还没有工作区，点 + 添加一个目录"));
    return;
  }
  for (const workspace of workspaces) {
    elements.workspaceList.append(renderWorkspace(workspace));
  }
}

function statusMark(task) {
  const waiting = WAITING_MARKS[task?.waiting];
  if (waiting) {
    const badge = el("span", "waiting-badge", waiting.mark);
    badge.title = waiting.label;
    return badge;
  }
  if (task?.running) {
    const dot = el("span", "running-dot");
    dot.title = "运行中";
    return dot;
  }
  return null;
}

function workspaceStatusMark(tasks) {
  return statusMark(tasks.find((task) => task.waiting)) || statusMark(tasks.find((task) => task.running));
}

function renderWorkspace(workspace) {
  const group = el("div", "workspace-group");
  const expanded = state.expandedWorkspaces.has(workspace.path);
  const isDefault = workspace.path === state.info.default_workspace;

  const header = el("div", "workspace-row");
  header.classList.toggle("missing", !workspace.exists);
  header.title = workspace.exists ? workspace.path : `${workspace.path}（目录不存在）`;

  const toggle = el("button", "workspace-toggle");
  toggle.type = "button";
  const title = el("span", "workspace-title");
  title.append(el("span", "workspace-name", workspace.name));
  if (isDefault) {
    title.append(el("span", "workspace-badge", "默认"));
  }
  toggle.append(icon(expanded ? "chevronDown" : "chevronRight", "tree-chevron"), icon("folder"), title);
  const mark = workspaceStatusMark(state.tasks.filter((task) => task.workspace === workspace.path));
  if (mark) {
    toggle.append(mark);
  }
  toggle.addEventListener("click", () => toggleWorkspace(workspace));

  const makeDefault = iconButton("star", isDefault ? "新任务默认就在这个工作区" : "设为新任务的默认工作区", "row-action");
  makeDefault.classList.toggle("active", isDefault);
  makeDefault.addEventListener("click", () => setDefaultWorkspace(workspace));
  const newTask = iconButton("plus", "在这个工作区开新任务", "row-action");
  newTask.addEventListener("click", () => callbacks.onNewTask(workspace.path));
  const refresh = iconButton("refresh", "刷新文件列表", "row-action");
  refresh.addEventListener("click", () => refreshWorkspace(workspace));
  const remove = iconButton("close", "从列表里移除（目录和任务记录都不动）", "row-action");
  remove.addEventListener("click", () => removeWorkspace(workspace));

  // 星标放最后：平时只有默认工作区的星亮着，要贴在行尾，不能停在一排隐藏按钮的最前面（行中间）
  header.append(toggle, newTask, refresh, remove, makeDefault);
  group.append(header);
  if (expanded && workspace.exists) {
    group.append(renderDirectory(workspace.path, "", 1));
  }
  return group;
}

function iconButton(name, title, className) {
  const node = el("button", className);
  node.type = "button";
  node.append(icon(name));
  node.title = title;
  node.setAttribute("aria-label", title);
  return node;
}

function toggleWorkspace(workspace) {
  if (state.expandedWorkspaces.has(workspace.path)) {
    state.expandedWorkspaces.delete(workspace.path);
  } else {
    state.expandedWorkspaces.add(workspace.path);
    loadDirectory(workspace.path, "");
  }
  saveExpanded();
  render();
}

function refreshWorkspace(workspace) {
  for (const key of [...tree.children.keys()]) {
    if (key.startsWith(`${workspace.path}\u0000`)) {
      tree.children.delete(key);
    }
  }
  state.expandedWorkspaces.add(workspace.path);
  loadDirectory(workspace.path, "");
  for (const key of tree.expandedDirs) {
    const [workspacePath, relativePath] = key.split("\u0000");
    if (workspacePath === workspace.path) {
      loadDirectory(workspacePath, relativePath);
    }
  }
  render();
}

async function reloadLoadedDirectories(workspacePath) {
  const keys = [...tree.children.keys()].filter((key) => key.startsWith(`${workspacePath}\u0000`));
  await Promise.all(
    keys.map(async (key) => {
      const relativePath = key.split("\u0000")[1];
      try {
        const query = `workspace=${encodeURIComponent(workspacePath)}&path=${encodeURIComponent(relativePath)}`;
        const { entries } = await request(`/api/files?${query}`);
        tree.children.set(key, entries);
      } catch (error) {
        tree.children.delete(key);
      }
    }),
  );
  render();
}

async function loadDirectory(workspacePath, relativePath) {
  const key = treeKey(workspacePath, relativePath);
  if (tree.children.has(key) || tree.loading.has(key)) {
    return;
  }
  tree.loading.add(key);
  try {
    const query = `workspace=${encodeURIComponent(workspacePath)}&path=${encodeURIComponent(relativePath)}`;
    const { entries } = await request(`/api/files?${query}`);
    tree.children.set(key, entries);
  } catch (error) {
    tree.children.set(key, [{ name: `读不了：${error.message}`, path: "", is_dir: false, error: true }]);
  } finally {
    tree.loading.delete(key);
    render();
  }
}

function renderDirectory(workspacePath, relativePath, depth) {
  const container = el("div", "file-tree");
  const key = treeKey(workspacePath, relativePath);
  const entries = tree.children.get(key);
  if (!entries) {
    loadDirectory(workspacePath, relativePath);
    container.append(treeNote("加载中…", depth));
    return container;
  }
  if (entries.length === 0) {
    container.append(treeNote("空目录", depth));
  }
  for (const entry of entries) {
    container.append(
      entry.is_dir ? renderDirectoryRow(workspacePath, entry, depth) : renderFileRow(workspacePath, entry, depth),
    );
  }
  return container;
}

function treeNote(text, depth) {
  const note = el("div", "tree-note", text);
  note.style.paddingLeft = `${depth * 14 + 22}px`;
  return note;
}

function renderDirectoryRow(workspacePath, entry, depth) {
  const wrapper = el("div");
  const key = treeKey(workspacePath, entry.path);
  const expanded = tree.expandedDirs.has(key);
  const row = el("button", "tree-row");
  row.type = "button";
  row.style.paddingLeft = `${depth * 14 + 4}px`;
  row.title = entry.path;
  row.append(icon(expanded ? "chevronDown" : "chevronRight", "tree-chevron"), icon("folder"), el("span", "tree-name", entry.name));
  row.addEventListener("click", () => {
    if (expanded) {
      tree.expandedDirs.delete(key);
    } else {
      tree.expandedDirs.add(key);
      loadDirectory(workspacePath, entry.path);
    }
    render();
  });
  wrapper.append(row);
  if (expanded) {
    wrapper.append(renderDirectory(workspacePath, entry.path, depth + 1));
  }
  return wrapper;
}

function renderFileRow(workspacePath, entry, depth) {
  const row = el("button", `tree-row file${entry.error ? " error" : ""}`);
  row.type = "button";
  row.style.paddingLeft = `${depth * 14 + 18}px`;
  row.title = entry.path;
  row.append(icon("file"), el("span", "tree-name", entry.name));
  if (!entry.error) {
    row.addEventListener("click", () => openFilePreview(workspacePath, entry.path));
  }
  return row;
}

async function removeWorkspace(workspace) {
  if (!window.confirm(`从工作区列表里移除「${workspace.name}」？\n只是不显示，目录和任务记录都不动。`)) {
    return;
  }
  await sendJson("/api/workspaces", "DELETE", { path: workspace.path });
  await callbacks.onReloadInfo?.();
}

async function setDefaultWorkspace(workspace) {
  await sendJson("/api/workspaces/default", "POST", { path: workspace.path });
  await callbacks.onReloadInfo?.();
}

// ---- 最近任务 ----

function renderRecentTasks() {
  elements.recentList.replaceChildren();
  const tasks = state.tasks || [];
  elements.recentCount.textContent = tasks.length ? String(tasks.length) : "";
  if (tasks.length === 0) {
    elements.recentList.append(el("div", "sidebar-empty", "还没有任务"));
    return;
  }
  for (const task of tasks.slice(0, RECENT_TASK_LIMIT)) {
    elements.recentList.append(taskRow(task));
  }
}

function taskRow(task) {
  const item = el("div", "task-item");
  const row = el("button", "task-row");
  row.type = "button";
  row.classList.toggle("active", activeView === "task" && task.id === state.currentTaskId);

  const text = el("span", "task-text");
  text.append(el("span", "task-title", task.title));
  const place = task.ephemeral ? "临时" : workspaceName(task.workspace);
  text.append(el("span", "task-meta", `${place} · ${formatRelativeTime(task.updated_at)}`));

  if (task.source && task.source.startsWith("automation:")) {
    row.append(icon("clock", "task-kind"));
  }
  row.append(text);
  const mark = statusMark(task);
  if (mark) {
    row.append(mark);
    row.title = mark.title || "运行中";
  }
  row.addEventListener("click", () => callbacks.onOpenTask(task.id));
  item.append(row);

  if (!task.running) {
    const remove = iconButton("close", "删除这个任务", "row-action task-delete");
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteTask(task);
    });
    item.append(remove);
  }
  return item;
}

async function deleteTask(task) {
  if (!window.confirm(`删除任务「${task.title}」？\n对话记录、附件和检查点都会删掉，工作区里的文件不动。`)) {
    return;
  }
  try {
    await sendJson(`/api/sessions/${encodeURIComponent(task.id)}`, "DELETE");
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
    return;
  }
  callbacks.onTaskDeleted?.(task.id);
}
