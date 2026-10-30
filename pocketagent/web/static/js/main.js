// 入口：页面路由（#/new、#/task/<id>、#/automations）、把各模块接起来、侧栏和右侧栏的开关。

import { sendJson } from "./core/api.js";
import { closeEditor, initAutomations, isAutomationEditorOpen, showAutomations } from "./automations/automations.js";
import { chatComposer, initChat, interrupt, isChatRunning, leaveTask, openTask, updateChatOptions } from "./task/chat.js";
import { looksLikeCommand, loadSkillNames, runCommand } from "./core/commands.js";
import { Composer, SCRATCH_WORKSPACE } from "./task/composer.js";
import { applyAppearance, el, icon, show } from "./ui/dom.js";
import { citationText, closeFilePreview, initFilePreview, isFilePreviewOpen } from "./sidebar/filepreview.js";
import { closeFolderPicker, isFolderPickerOpen } from "./sidebar/folderpicker.js";
import { initResizers } from "./ui/resizers.js";
import { initRightPanel, setRightPanelVisible, showTaskInPanel } from "./task/rightpanel.js";
import { closeSettings, initSettings, isSettingsOpen, openSettings } from "./settings/settings.js";
import { initSidebar, setActiveView } from "./sidebar/sidebar.js";
import { loadPreference, on, reloadInfo, reloadTasks, savePreference, state } from "./core/state.js";

const TASK_POLL_MILLISECONDS = 5000;

const views = {
  new: document.getElementById("new-task-view"),
  chat: document.getElementById("chat-view"),
  automations: document.getElementById("automations-view"),
};
const app = document.getElementById("app");
let newTaskComposer = null;
// 上次看到的默认工作区，用来判断用户是不是自己挑过输入框里的工作区
let lastDefaultWorkspace = null;

// ===================== 路由 =====================

function navigate(hash) {
  if (window.location.hash === hash) {
    route();
  } else {
    window.location.hash = hash;
  }
}

function parseRoute() {
  const hash = window.location.hash.replace(/^#/, "");
  const [path, query] = hash.split("?");
  const params = new URLSearchParams(query || "");
  const taskMatch = path.match(/^\/task\/(.+)$/);
  if (taskMatch) {
    return { view: "chat", taskId: decodeURIComponent(taskMatch[1]) };
  }
  if (path === "/automations") {
    return { view: "automations" };
  }
  return { view: "new", workspace: params.get("workspace") };
}

function showView(view) {
  for (const [name, element] of Object.entries(views)) {
    show(element, name === view);
  }
  app.classList.toggle("in-task", view === "chat");
  setActiveView(view === "chat" ? "task" : view);
  closeMobileSidebar();
}

function route() {
  const target = parseRoute();
  if (target.view !== "chat") {
    leaveTask();
  }
  if (target.view === "chat") {
    openTask(target.taskId, pendingFirstMessages.get(target.taskId));
    pendingFirstMessages.delete(target.taskId);
    showView("chat");
    showTaskInPanel(target.taskId);
  } else if (target.view === "automations") {
    showView("automations");
    document.title = "自动化 · PocketAgent";
    showAutomations();
  } else {
    showView("new");
    document.title = "新任务 · PocketAgent";
    prepareNewTask(target.workspace);
  }
}

// ===================== 新任务页 =====================

// 新任务页发的第一句话：建好任务、跳到任务页、连上之后再发
const pendingFirstMessages = new Map();

function initNewTask() {
  newTaskComposer = new Composer(document.getElementById("new-task-composer"), {
    kind: "new",
    placeholder: "描述你要做的事，比如「看看这个仓库是做什么的」…  Enter 发送；/ 开头是命令",
    onSubmit: startNewTask,
    onWorkspaceChange: refreshNewTaskSkills,
  });
}

function prepareNewTask(workspacePath) {
  const info = state.info;
  document.getElementById("greeting").textContent = `${info.user_name}，今天要做点什么？`;
  newTaskComposer.setModes(info.modes);
  newTaskComposer.setMode(info.default_mode);
  newTaskComposer.setModels(info.models, info.fallback_model);
  newTaskComposer.setDefaultModel(info.default_model);
  newTaskComposer.setWorkspaces(info.workspaces, workspacePath || info.default_workspace);
  newTaskComposer.setCommands(info.commands || []);
  const hint = document.getElementById("new-task-hint");
  hint.replaceChildren();
  hint.classList.remove("error", "command-output");
  if (info.models.length === 0) {
    const settingsLink = el("button", "link-button", "去设置里添加");
    settingsLink.type = "button";
    settingsLink.addEventListener("click", () => openSettings("models"));
    hint.append("还没配置模型，", settingsLink);
  }
  refreshNewTaskSkills();
  newTaskComposer.focus();
}

// 新任务页的命令补全要列当前选中工作区里的技能
function refreshNewTaskSkills() {
  const choice = newTaskComposer.getWorkspaceChoice();
  loadSkillNames(choice.workspace).then((names) => newTaskComposer.setSkillNames(names));
}

async function startNewTask({ text, files }) {
  // 斜杠命令（比如 /help、/skills）在新任务页也能用：不用先建任务
  if (text && files.length === 0 && looksLikeCommand(text)) {
    const choice = newTaskComposer.getWorkspaceChoice();
    const outcome = await runCommand(text, { workspace: choice.workspace }).catch((error) => ({
      handled: true,
      text: `命令执行失败：${error.message}`,
      kind: "error",
    }));
    if (outcome.handled) {
      showNewTaskCommandOutput(outcome);
      return true;
    }
  }
  const choice = newTaskComposer.getWorkspaceChoice();
  let created;
  try {
    created = await sendJson("api/sessions", "POST", {
      workspace: choice.workspace,
      ephemeral: choice.ephemeral,
      model_id: newTaskComposer.getModel(),
      mode: newTaskComposer.getMode(),
      agent_mode: newTaskComposer.getAgentMode(),
    });
  } catch (error) {
    window.alert(`新建任务失败：${error.message}`);
    return false;
  }
  pendingFirstMessages.set(created.id, { text, files });
  navigate(`#/task/${encodeURIComponent(created.id)}`);
  return true;
}

// 新任务页的命令输出：没有对话区，放在输入框下面的提示行里
function showNewTaskCommandOutput(outcome) {
  const hint = document.getElementById("new-task-hint");
  hint.textContent = outcome.text || "";
  hint.classList.toggle("error", outcome.kind === "error");
  hint.classList.toggle("command-output", Boolean(outcome.text));
  if (outcome.action === "open_session" && outcome.argument) {
    navigate(`#/task/${encodeURIComponent(outcome.argument)}`);
    return;
  }
  // /model、/mode 在新任务页改的是输入框上的选择器
  if (outcome.action === "switch_model" && outcome.argument) {
    newTaskComposer.setModel(outcome.argument);
  }
  if (outcome.action === "switch_mode" && outcome.argument) {
    newTaskComposer.setMode(outcome.argument);
  }
  if (outcome.action === "switch_agent_mode" && outcome.argument) {
    newTaskComposer.setAgentMode(outcome.argument);
  }
}

// ===================== 侧栏 / 右侧栏开关 =====================

function isMobileLayout() {
  return window.matchMedia("(max-width: 860px)").matches;
}

function toggleSidebar() {
  if (isMobileLayout()) {
    app.classList.toggle("sidebar-open");
    return;
  }
  const hidden = !app.classList.contains("sidebar-hidden");
  app.classList.toggle("sidebar-hidden", hidden);
  savePreference("sidebarHidden", hidden);
}

function closeMobileSidebar() {
  app.classList.remove("sidebar-open");
}

function bindLayoutControls() {
  app.classList.toggle("sidebar-hidden", loadPreference("sidebarHidden", false));
  // 手机上右侧栏是抽屉，默认收起，也不改电脑上记住的偏好
  if (isMobileLayout()) {
    setRightPanelVisible(false, { remember: false });
  } else {
    setRightPanelVisible(loadPreference("rightPanel", true));
  }
  for (const toggle of document.querySelectorAll('[data-action="toggle-sidebar"]')) {
    toggle.addEventListener("click", toggleSidebar);
  }
  // 手机上点遮罩：收起左侧栏和右侧抽屉
  document.getElementById("sidebar-backdrop").addEventListener("click", () => {
    closeMobileSidebar();
    setRightPanelVisible(false, { remember: false });
  });
  // 窗口缩到手机宽度时右侧栏变成抽屉，先收起来，免得盖住对话
  window.matchMedia("(max-width: 860px)").addEventListener("change", (query) => {
    if (query.matches) {
      setRightPanelVisible(false, { remember: false });
    } else {
      setRightPanelVisible(loadPreference("rightPanel", true), { remember: false });
    }
  });
  document.getElementById("toggle-right-panel").addEventListener("click", () => {
    setRightPanelVisible(app.classList.contains("right-panel-hidden"), { remember: !isMobileLayout() });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    if (isSettingsOpen()) {
      closeSettings();
    } else if (isFilePreviewOpen()) {
      closeFilePreview();
    } else if (isFolderPickerOpen()) {
      closeFolderPicker();
    } else if (isAutomationEditorOpen()) {
      closeEditor();
    } else if (isChatRunning()) {
      interrupt();
    }
  });
}

// ===================== 启动 =====================

// 页面里静态写的按钮只带 data-icon，图标在这里统一补上
function fillStaticIcons() {
  for (const element of document.querySelectorAll("[data-icon]")) {
    element.prepend(icon(element.dataset.icon));
  }
}

function onInfoChanged(info) {
  applyAppearance(info);
  updateChatOptions(info);
  if (parseRoute().view === "new") {
    const choice = newTaskComposer.getWorkspaceChoice();
    // 用户自己挑过（包括「临时任务」）就别动；还是旧默认就跟着新的默认走
    let keep = null;
    if (choice.ephemeral) {
      keep = SCRATCH_WORKSPACE;
    } else if (choice.workspace && choice.workspace !== lastDefaultWorkspace) {
      keep = choice.workspace;
    }
    prepareNewTask(keep || info.default_workspace);
  }
  lastDefaultWorkspace = info.default_workspace;
}

// 文件预览里点「在对话中引用」：任务页就放进任务的输入框；否则去新任务页，并切到这个文件所在的工作区
function citeFile({ workspace, path }) {
  const view = parseRoute().view;
  if (view === "chat") {
    chatComposer().insertText(citationText(path));
    return;
  }
  if (view !== "new") {
    navigate(`#/new?workspace=${encodeURIComponent(workspace)}`);
  } else {
    newTaskComposer.setWorkspaces(state.info.workspaces, workspace);
  }
  newTaskComposer.insertText(citationText(path));
}

function startTaskPolling() {
  // 侧栏的运行中标记、自动化跑出来的新任务靠轮询刷新
  setInterval(() => {
    if (document.visibilityState === "visible") {
      reloadTasks().catch(() => {});
    }
  }, TASK_POLL_MILLISECONDS);
}

async function init() {
  fillStaticIcons();
  state.expandedWorkspaces = new Set(loadPreference("expandedWorkspaces", []));
  initSidebar({
    onNewTask: (workspace) => navigate(workspace ? `#/new?workspace=${encodeURIComponent(workspace)}` : "#/new"),
    onOpenTask: (taskId) => navigate(`#/task/${encodeURIComponent(taskId)}`),
    onAutomations: () => navigate("#/automations"),
    onSettings: (tab) => openSettings(tab),
    onTaskDeleted: async (taskId) => {
      // 删的是正开着的任务：先断开它，回到新任务页
      if (taskId === state.currentTaskId) {
        leaveTask();
        navigate("#/new");
      }
      await reloadTasks();
    },
  });
  initChat({ onDeleted: () => navigate("#/new") });
  initRightPanel();
  initSettings();
  initAutomations({ onOpenTask: (taskId) => navigate(`#/task/${encodeURIComponent(taskId)}`) });
  initNewTask();
  initFilePreview();
  bindLayoutControls();
  initResizers();

  await reloadInfo();
  applyAppearance(state.info);
  lastDefaultWorkspace = state.info.default_workspace;
  if (state.expandedWorkspaces.size === 0 && state.info.workspaces.length > 0) {
    state.expandedWorkspaces.add(state.info.default_workspace || state.info.workspaces[0].path);
  }
  await reloadTasks();
  on("info-changed", onInfoChanged);
  on("turn-finished", () => reloadTasks());
  on("cite-file", citeFile);
  updateChatOptions(state.info);
  window.addEventListener("hashchange", route);
  route();
  startTaskPolling();
}

init().catch((error) => {
  document.body.prepend(el("div", "fatal-error", `页面初始化失败：${error.message}`));
});
