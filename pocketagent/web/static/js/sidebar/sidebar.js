// 左侧栏：新任务、自动化、工作区（点开是文件树）、最近任务（全部历史），底部是账户和设置。

import { requestJson, sendJson } from "../core/api.js";
import { button, el, icon, relativeTime, workspaceName } from "../ui/dom.js";
import { openFilePreview } from "./filepreview.js";
import { initFolderPicker, openFolderPicker } from "./folderpicker.js";
import { loadPreference, on, reloadInfo, savePreference, state } from "../core/state.js";

const RECENT_TASK_LIMIT = 50;

const elements = {};
let callbacks = {};
// 当前所在的页面，用来高亮：new / automations / task
let activeView = "new";
// 文件树：工作区 → (目录相对路径 → 子项)；展开了的目录；正在加载的目录
const tree = {
  children: new Map(),
  expandedDirs: new Set(),
  loading: new Set(),
};
// 上次画侧栏用的数据，没变就不重画（任务列表每 5 秒轮询一次）
let lastSignature = "";
// 任务改了文件后，过一会儿把那个工作区已经展开的目录静默刷新一遍
let filesRefreshTimer = null;

export function initSidebar(handlers) {
  callbacks = handlers;
  elements.workspaceList = document.getElementById("workspace-list");
  elements.recentList = document.getElementById("recent-list");
  elements.navNewTask = document.getElementById("nav-new-task");
  elements.navAutomations = document.getElementById("nav-automations");
  elements.accountName = document.getElementById("account-name");
  elements.avatar = document.getElementById("avatar");
  elements.recentCount = document.getElementById("recent-count");
  initCollapsibleSections();

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
    callbacks.onNewTask(addedPath);
  });

  on("tasks-changed", () => render());
  on("info-changed", () => render(true));
  on("files-changed", (taskId) => {
    clearTimeout(filesRefreshTimer);
    filesRefreshTimer = setTimeout(() => {
      // 刚建的任务可能还不在任务列表里，那就把展开着的工作区都刷一遍
      const task = state.tasks.find((each) => each.id === taskId);
      const workspaces = task ? [task.workspace] : [...state.expandedWorkspaces];
      workspaces.forEach(reloadLoadedDirectories);
    }, 800);
  });
}

function initCollapsibleSections() {
  // 折叠状态记在浏览器里，下次打开还是那样
  const collapsed = new Set(loadPreference("collapsedSections", []));
  for (const section of document.querySelectorAll(".sidebar-section")) {
    const toggle = section.querySelector(".section-toggle");
    const apply = () => {
      const isCollapsed = collapsed.has(section.dataset.section);
      section.classList.toggle("collapsed", isCollapsed);
      toggle.setAttribute("aria-expanded", String(!isCollapsed));
    };
    toggle.addEventListener("click", () => {
      if (collapsed.has(section.dataset.section)) {
        collapsed.delete(section.dataset.section);
      } else {
        collapsed.add(section.dataset.section);
      }
      savePreference("collapsedSections", [...collapsed]);
      apply();
    });
    apply();
  }
}

export function setActiveView(view) {
  activeView = view;
  render();
}

function treeKey(workspacePath, relativePath) {
  return `${workspacePath}\u0000${relativePath}`;
}

function signature() {
  return JSON.stringify([
    activeView,
    state.currentTaskId,
    state.tasks,
    state.info.workspaces,
    state.info.default_workspace,
    state.info.user_name,
    [...state.expandedWorkspaces],
    [...tree.expandedDirs],
    [...tree.loading],
    tree.children.size,
  ]);
}

function render(force = false) {
  if (!state.info) {
    return;
  }
  const current = signature();
  if (!force && current === lastSignature) {
    return;
  }
  lastSignature = current;
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

// ---- 工作区和文件树 ----

function saveExpanded() {
  savePreference("expandedWorkspaces", [...state.expandedWorkspaces]);
}

function renderWorkspaces() {
  elements.workspaceList.replaceChildren();
  if (state.info.workspaces.length === 0) {
    elements.workspaceList.appendChild(el("div", "sidebar-empty", "还没有工作区，点 + 添加一个目录"));
    return;
  }
  for (const workspace of state.info.workspaces) {
    elements.workspaceList.appendChild(renderWorkspace(workspace));
  }
}

// 侧栏上的状态标记：在等用户（回答问题 / 确认操作）用静态的琥珀色角标，
// 只有真的在跑才用蓝色呼吸灯——呼吸灯一直闪，会让人以为它还在干活。
const WAITING_MARKS = {
  question: { mark: "?", label: "等你回答问题" },
  approval: { mark: "!", label: "等你确认操作" },
};

function statusMark(task) {
  const waiting = WAITING_MARKS[task?.waiting];
  if (waiting) {
    const badge = el("span", "waiting-badge", waiting.mark);
    badge.title = waiting.label;
    return badge;
  }
  if (task?.running) {
    return el("span", "running-dot");
  }
  return null;
}

// 一个工作区里可能有好几个任务：等用户的优先，其次才是在跑的
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
    title.appendChild(el("span", "workspace-badge", "默认"));
  }
  toggle.append(icon(expanded ? "chevronDown" : "chevronRight", "tree-chevron"), icon("folder"), title);
  const mark = workspaceStatusMark(state.tasks.filter((task) => task.workspace === workspace.path));
  if (mark) {
    toggle.appendChild(mark);
  }
  toggle.addEventListener("click", () => toggleWorkspace(workspace));
  const makeDefault = button("row-action", "", "star", isDefault ? "新任务默认就在这个工作区" : "设为新任务的默认工作区");
  makeDefault.classList.toggle("active", isDefault);
  makeDefault.addEventListener("click", () => setDefaultWorkspace(workspace));
  const newTask = button("row-action", "", "plus", "在这个工作区开新任务");
  newTask.addEventListener("click", () => callbacks.onNewTask(workspace.path));
  const refresh = button("row-action", "", "refresh", "刷新文件列表");
  refresh.addEventListener("click", () => refreshWorkspace(workspace));
  const remove = button("row-action", "", "close", "从列表里移除（目录和任务记录都不动）");
  remove.addEventListener("click", () => removeWorkspace(workspace));
  // 星标放最后：悬停冒出其余按钮时，默认工作区的星还停在行尾，不跳位置（和桌面端一致）
  header.append(toggle, newTask, refresh, remove, makeDefault);
  group.appendChild(header);
  if (expanded && workspace.exists) {
    group.appendChild(renderDirectory(workspace.path, "", 1));
  }
  return group;
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
  render(true);
}

async function reloadLoadedDirectories(workspacePath) {
  // 不先清空，新列表到了直接换上，不会闪「加载中」
  const keys = [...tree.children.keys()].filter((key) => key.startsWith(`${workspacePath}\u0000`));
  await Promise.all(
    keys.map(async (key) => {
      const relativePath = key.split("\u0000")[1];
      try {
        const query = `workspace=${encodeURIComponent(workspacePath)}&path=${encodeURIComponent(relativePath)}`;
        const { entries } = await requestJson(`api/files?${query}`);
        tree.children.set(key, entries);
      } catch (error) {
        // 目录被删了：去掉缓存，展开时再重新加载
        tree.children.delete(key);
      }
    })
  );
  render(true);
}

async function loadDirectory(workspacePath, relativePath) {
  const key = treeKey(workspacePath, relativePath);
  if (tree.children.has(key) || tree.loading.has(key)) {
    return;
  }
  // 「加载中…」已经由调用方画出来了，这里不能同步重画（可能正在画侧栏的过程中被调用）
  tree.loading.add(key);
  try {
    const query = `workspace=${encodeURIComponent(workspacePath)}&path=${encodeURIComponent(relativePath)}`;
    const { entries } = await requestJson(`api/files?${query}`);
    tree.children.set(key, entries);
  } catch (error) {
    tree.children.set(key, [{ name: `读不了：${error.message}`, path: "", is_dir: false, error: true }]);
  } finally {
    tree.loading.delete(key);
    render(true);
  }
}

function renderDirectory(workspacePath, relativePath, depth) {
  const container = el("div", "file-tree");
  const key = treeKey(workspacePath, relativePath);
  const entries = tree.children.get(key);
  if (!entries) {
    // 还没加载过（比如刷新页面后恢复了展开状态），现在加载
    loadDirectory(workspacePath, relativePath);
    container.appendChild(treeNote("加载中…", depth));
    return container;
  }
  if (entries.length === 0) {
    container.appendChild(treeNote("空目录", depth));
  }
  for (const entry of entries) {
    container.appendChild(entry.is_dir ? renderDirectoryRow(workspacePath, entry, depth) : renderFileRow(workspacePath, entry, depth));
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
  row.append(icon(expanded ? "chevronDown" : "chevronRight", "tree-chevron"), icon("folder", "tree-icon"), el("span", "tree-name", entry.name));
  row.addEventListener("click", () => {
    if (expanded) {
      tree.expandedDirs.delete(key);
    } else {
      tree.expandedDirs.add(key);
      loadDirectory(workspacePath, entry.path);
    }
    render();
  });
  wrapper.appendChild(row);
  if (expanded) {
    wrapper.appendChild(renderDirectory(workspacePath, entry.path, depth + 1));
  }
  return wrapper;
}

function renderFileRow(workspacePath, entry, depth) {
  const row = el("button", `tree-row file${entry.error ? " error" : ""}`);
  row.type = "button";
  row.style.paddingLeft = `${depth * 14 + 18}px`;
  row.title = entry.path;
  row.append(icon("file", "tree-icon"), el("span", "tree-name", entry.name));
  if (!entry.error) {
    row.addEventListener("click", () => openFilePreview(workspacePath, entry.path));
  }
  return row;
}

async function removeWorkspace(workspace) {
  if (!window.confirm(`从工作区列表里移除「${workspace.name}」？\n只是不显示，目录和任务记录都不动。`)) {
    return;
  }
  await sendJson("api/workspaces", "DELETE", { path: workspace.path });
  await reloadInfo();
}

async function setDefaultWorkspace(workspace) {
  await sendJson("api/workspaces/default", "POST", { path: workspace.path });
  await reloadInfo();
}

// ---- 最近任务（全部历史） ----

function taskRow(task) {
  // 按钮里不能再套按钮，所以外面包一层：整行点开任务，右边悬停出来的 × 删除
  const item = el("div", "task-item");
  const row = el("button", "task-row");
  row.type = "button";
  row.classList.toggle("active", activeView === "task" && task.id === state.currentTaskId);
  const title = el("span", "task-title", task.title);
  // 临时任务没有工作区，写「临时」比显示 scratch 下的目录名清楚
  const place = task.ephemeral ? "临时" : workspaceName(task.workspace);
  const meta = el("span", "task-meta", `${place} · ${relativeTime(task.updated_at)}`);
  const text = el("span", "task-text");
  text.append(title, meta);
  if (task.source && task.source.startsWith("automation:")) {
    row.appendChild(icon("clock", "task-kind"));
  }
  row.appendChild(text);
  const mark = statusMark(task);
  if (mark) {
    row.appendChild(mark);
    row.title = mark.title || "运行中";
  }
  row.addEventListener("click", () => callbacks.onOpenTask(task.id));
  item.appendChild(row);
  if (!task.running) {
    const remove = button("row-action task-delete", "", "close", "删除这个任务");
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteTask(task);
    });
    item.appendChild(remove);
  }
  return item;
}

async function deleteTask(task) {
  if (!window.confirm(`删除任务「${task.title}」？\n对话记录、附件和检查点都会删掉，工作区里的文件不动。`)) {
    return;
  }
  try {
    await sendJson(`api/sessions/${encodeURIComponent(task.id)}`, "DELETE");
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
    return;
  }
  callbacks.onTaskDeleted(task.id);
}

function renderRecentTasks() {
  elements.recentList.replaceChildren();
  elements.recentCount.textContent = state.tasks.length ? String(state.tasks.length) : "";
  if (state.tasks.length === 0) {
    elements.recentList.appendChild(el("div", "sidebar-empty", "还没有任务"));
    return;
  }
  for (const task of state.tasks.slice(0, RECENT_TASK_LIMIT)) {
    elements.recentList.appendChild(taskRow(task));
  }
}
