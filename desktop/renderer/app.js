// 桌面端入口：等后端起来 → 读配置 → 三个视图的路由和所有模块接线。

import {
  SessionSocket,
  attachmentUrl,
  getBackendState,
  loadSkillNames,
  looksLikeCommand,
  onBackendLog,
  onBackendState,
  openExternal,
  request,
  restartBackend,
  runCommand,
  sendJson,
  uploadFiles,
} from "./api.js";
import { closeEditor, initAutomations, isAutomationEditorOpen, showAutomations } from "./automations.js";
import { ChatView } from "./chat.js";
import { Composer } from "./composer.js";
import { clear, el, show } from "./dom.js";
import { closeFilePreview, citationText, initFilePreview, isFilePreviewOpen } from "./filepreview.js";
import { closeFolderPicker, isFolderPickerOpen } from "./folderpicker.js";
import { icon } from "./icons.js";
import { initResizers } from "./resizers.js";
import { closeSettings, initSettings, isSettingsOpen, openSettings } from "./settings.js";
import { initSidebar, setActiveView } from "./sidebar.js";
import { emit, loadPreference, on, savePreference, state } from "./state.js";
import { RightPanel } from "./rightpanel.js";

const TASK_POLL_MILLISECONDS = 5000;

const elements = {
  app: document.getElementById("app"),
  views: {
    new: document.getElementById("new-task-view"),
    chat: document.getElementById("chat-view"),
    automations: document.getElementById("automations-view"),
  },
  greeting: document.getElementById("greeting"),
  newTaskComposer: document.getElementById("new-task-composer"),
  newTaskHint: document.getElementById("new-task-hint"),
  chatTitle: document.getElementById("chat-title"),
  chatMenuButton: document.getElementById("chat-menu-button"),
  chatMenu: document.getElementById("chat-menu"),
  transcript: document.getElementById("transcript"),
  transcriptInner: document.getElementById("transcript-inner"),
  connectionBanner: document.getElementById("connection-banner"),
  pendingInputs: document.getElementById("pending-inputs"),
  chatComposer: document.getElementById("chat-composer"),
  toggleSidebarButton: document.getElementById("toggle-sidebar-button"),
  toggleRightPanel: document.getElementById("toggle-right-panel"),
  overviewCard: document.getElementById("overview-card"),
  changeList: document.getElementById("change-list"),
  changesCount: document.getElementById("changes-count"),
  outputList: document.getElementById("output-list"),
  checkpointList: document.getElementById("checkpoint-list"),
  refreshChanges: document.getElementById("refresh-changes"),
  toast: document.getElementById("toast"),
  startupOverlay: document.getElementById("startup-overlay"),
  startupTitle: document.getElementById("startup-title"),
  startupDetail: document.getElementById("startup-detail"),
  startupActions: document.getElementById("startup-actions"),
  startupLog: document.getElementById("startup-log"),
  restartButton: document.getElementById("restart-button"),
};

const runtime = {
  backend: null,
  backendReady: false,
  socket: null,
  lastChatStatus: null,
  pendingFirstMessages: new Map(),
  pollingTimer: null,
  toastTimer: null,
};

const chat = new ChatView({
  transcript: elements.transcript,
  inner: elements.transcriptInner,
  callbacks: {
    onStatus: applyChatStatus,
    onSnapshot: handleSnapshot,
    onTurnEnd: (event) => {
      rightPanel.handleTurnEnd(event.final_text);
      refreshTasks().catch(() => {});
    },
    onApproval: (requestId, answer) => runtime.socket?.send({ type: "approval", request_id: requestId, answer }),
    onQuestionAnswer: (requestId, answers) =>
      runtime.socket?.send({ type: "question_answer", request_id: requestId, answers }),
    onReturnedInputs: (inputs) => chatComposer.restoreTexts(inputs.map((input) => input.text)),
    onOpenAttachment: (attachment) => openAttachment(attachment),
    onModelResponded: () => rightPanel.handleModelResponded(),
    onFilesChanged: () => {
      emit("files-changed", state.currentTaskId);
      rightPanel.scheduleChangesRefresh();
    },
  },
});

const chatComposer = new Composer(elements.chatComposer, {
  kind: "chat",
  placeholder: "接着说…  Enter 发送，Shift+Enter 换行，图片可以直接粘贴",
  onSubmit: sendChatMessage,
  onStop: interrupt,
  onModeChange: (mode) => runtime.socket?.send({ type: "set_mode", mode }),
  onModelChange: (modelId) => runtime.socket?.send({ type: "set_model", model_id: modelId }),
});

const newTaskComposer = new Composer(elements.newTaskComposer, {
  kind: "new",
  placeholder: "描述你要做的事，比如「看看这个仓库是做什么的」…  Enter 发送；/ 开头是命令",
  onSubmit: startNewTask,
  onWorkspaceChange: refreshNewTaskSkills,
});

const rightPanel = new RightPanel({
  overviewCard: elements.overviewCard,
  changeList: elements.changeList,
  changesCount: elements.changesCount,
  outputList: elements.outputList,
  checkpointList: elements.checkpointList,
  refreshButton: elements.refreshChanges,
});
rightPanel.onFilesChanged = () => emit("files-changed", state.currentTaskId);

function hideChatMenu() {
  elements.chatMenu.classList.add("hidden");
}

function updateChatMenuVisibility() {
  elements.chatMenuButton.disabled = !state.currentTaskId;
}

// ---- 视图路由 ----

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

function navigate(hash) {
  if (window.location.hash === hash) {
    route();
  } else {
    window.location.hash = hash;
  }
}

function showView(view) {
  for (const [name, element] of Object.entries(elements.views)) {
    show(element, name === view);
  }
  elements.app.classList.toggle("in-task", view === "chat");
  setActiveView(view === "chat" ? "task" : view);
  hideChatMenu();
  updateChatMenuVisibility();
}

function route() {
  if (!runtime.backendReady) {
    return;
  }
  const target = parseRoute();
  if (target.view !== "chat") {
    leaveTask();
  }
  if (target.view === "chat") {
    showView("chat");
    openTask(target.taskId);
    return;
  }
  if (target.view === "automations") {
    showView("automations");
    document.title = "自动化 · PocketAgent";
    showAutomations();
    return;
  }
  showView("new");
  document.title = "新任务 · PocketAgent";
  prepareNewTask(target.workspace);
}

// ---- 新任务页 ----

function prepareNewTask(workspacePath) {
  const info = state.info;
  if (!info) {
    return;
  }
  elements.greeting.textContent = `${info.user_name || "我"}，今天要做点什么？`;
  newTaskComposer.setModes(info.modes);
  newTaskComposer.setMode(info.default_mode);
  newTaskComposer.setModels(info.models, info.fallback_model);
  newTaskComposer.setDefaultModel(info.default_model);
  newTaskComposer.setWorkspaces(info.workspaces, workspacePath || info.default_workspace);
  newTaskComposer.setCommands(info.commands || []);

  elements.newTaskHint.replaceChildren();
  elements.newTaskHint.classList.remove("error");
  if ((info.models || []).length === 0) {
    const link = el("button", "link-button", "去设置里添加");
    link.type = "button";
    link.addEventListener("click", () => openSettings("models"));
    elements.newTaskHint.append("还没配置模型，", link);
  }
  refreshNewTaskSkills();
  newTaskComposer.focus();
}

function refreshNewTaskSkills() {
  const choice = newTaskComposer.getWorkspaceChoice();
  loadSkillNames(choice.workspace).then((names) => newTaskComposer.setSkillNames(names));
}

async function startNewTask({ text, files }) {
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
    created = await sendJson("/api/sessions", "POST", {
      workspace: choice.workspace,
      ephemeral: choice.ephemeral,
      model_id: newTaskComposer.getModel(),
      mode: newTaskComposer.getMode(),
    });
  } catch (error) {
    window.alert(`新建任务失败：${error.message}`);
    return false;
  }
  runtime.pendingFirstMessages.set(created.id, { text, files });
  navigate(`#/task/${encodeURIComponent(created.id)}`);
  return true;
}

function showNewTaskCommandOutput(outcome) {
  elements.newTaskHint.textContent = outcome.text || "";
  elements.newTaskHint.classList.toggle("error", outcome.kind === "error");
  if (outcome.action === "open_session" && outcome.argument) {
    navigate(`#/task/${encodeURIComponent(outcome.argument)}`);
    return;
  }
  if (outcome.action === "switch_model" && outcome.argument) {
    newTaskComposer.setModel(outcome.argument);
  }
  if (outcome.action === "switch_mode" && outcome.argument) {
    newTaskComposer.setMode(outcome.argument);
  }
}

function sendPendingFirstMessage(sessionId) {
  const pending = runtime.pendingFirstMessages.get(sessionId);
  if (!pending) {
    return;
  }
  runtime.pendingFirstMessages.delete(sessionId);
  const text = pending.text || "";
  const files = pending.files || [];
  if (files.length === 0) {
    runtime.socket?.send({ type: "user_message", text });
    return;
  }
  uploadFiles(sessionId, files)
    .then((attachments) => {
      runtime.socket?.send({
        type: "user_message",
        text,
        attachment_ids: attachments.map((item) => item.id),
      });
    })
    .catch((error) => {
      chat.addNotice(`附件上传失败：${error.message}`, "error");
      chatComposer.restoreTexts([text]);
    });
}

// ---- 任务页 ----

async function openTask(taskId) {
  if (!runtime.backendReady) {
    return;
  }
  if (state.currentTaskId === taskId && runtime.socket) {
    return;
  }
  closeSocket();
  state.currentTaskId = taskId;
  chat.reset();
  chatComposer.clearSessionState();
  elements.chatTitle.textContent = "加载中…";
  elements.connectionBanner.classList.add("hidden");
  rightPanel.showTask(taskId);

  const socket = new SessionSocket(taskId, {
    onMessage: (message) => {
      chat.handleMessage(message);
      if (message.type === "snapshot") {
        refreshTasks().catch(() => {});
      }
      if (message.type === "approval_request" || message.type === "question_request") {
        refreshTasks().catch(() => {});
      }
    },
    onOpen: () => {
      elements.connectionBanner.classList.add("hidden");
    },
    onClose: () => {
      if (state.currentTaskId === taskId) {
        elements.connectionBanner.classList.remove("hidden");
      }
    },
  });
  runtime.socket = socket;
  socket.connect(runtime.backend);
  setActiveView("task");
  updateChatMenuVisibility();
}

function closeSocket() {
  if (runtime.socket) {
    const socket = runtime.socket;
    runtime.socket = null;
    socket.close();
  }
}

function leaveTask() {
  closeSocket();
  state.currentTaskId = null;
  runtime.lastChatStatus = null;
  chat.reset();
  rightPanel.clear();
  elements.connectionBanner.classList.add("hidden");
}

function applyChatStatus(status) {
  runtime.lastChatStatus = status;
  elements.chatTitle.textContent = status.title || "任务";
  document.title = `${status.title || "PocketAgent"} · PocketAgent`;
  chatComposer.setMode(status.mode);
  chatComposer.setModel(status.model_id);
  chatComposer.setRunning(status.running, state.info?.busy_input_mode);
  chatComposer.setContext(status.context_tokens, status.context_window, status.compact_threshold);
  chatComposer.setCompacting(status.compacting);
  chatComposer.setWorkspaceLabel(status.workspace, status.ephemeral);
  loadSkillNames(status.workspace).then((names) => chatComposer.setSkillNames(names));
  renderPendingInputs(status);
  rightPanel.updateStatus(status);
}

function handleSnapshot(snapshot) {
  applyChatStatus(snapshot);
  sendPendingFirstMessage(snapshot.session_id);
  refreshTasks().catch(() => {});
}

function renderPendingInputs(status) {
  clear(elements.pendingInputs);
  const interjections = status.interjections || [];
  const queued = status.queued || [];

  for (const input of interjections) {
    const row = el("div", "pending-input");
    row.append(
      el("span", "pending-badge", "插话"),
      el("span", "pending-text", input.text || "（附件）"),
      el("span", "pending-note", "模型下一步会看到"),
    );
    elements.pendingInputs.append(row);
  }
  queued.forEach((input, index) => {
    const row = el("div", "pending-input");
    const remove = el("button", "pending-remove", "✕");
    remove.title = "从队列里删掉";
    remove.addEventListener("click", () => runtime.socket?.send({ type: "remove_queued", index }));
    row.append(el("span", "pending-badge queued", "排队"), el("span", "pending-text", input.text || "（附件）"), remove);
    elements.pendingInputs.append(row);
  });
  show(elements.pendingInputs, interjections.length + queued.length > 0);
}

async function sendChatMessage({ text, files }) {
  if (!runtime.socket) {
    chat.addNotice("还没连上服务，稍等再试", "warning");
    return false;
  }
  if (text && files.length === 0 && looksLikeCommand(text)) {
    const outcome = await runCommand(text, { sessionId: state.currentTaskId }).catch((error) => ({
      handled: true,
      text: `命令执行失败：${error.message}`,
      kind: "error",
    }));
    if (outcome.handled) {
      handleCommandOutcome(outcome);
      return true;
    }
  }

  let attachments = [];
  try {
    attachments = await uploadFiles(state.currentTaskId, files);
  } catch (error) {
    chat.addNotice(`附件上传失败：${error.message}`, "error");
    return false;
  }
  runtime.socket.send({
    type: "user_message",
    text,
    attachment_ids: attachments.map((item) => item.id),
  });
  return true;
}

function handleCommandOutcome(outcome) {
  if (outcome.text) {
    chat.addCommandOutput(outcome.text, outcome.kind);
  }
  if (outcome.action === "new_session") {
    const status = runtime.lastChatStatus || {};
    sendJson("/api/sessions", "POST", {
      workspace: status.ephemeral ? null : status.workspace,
      ephemeral: Boolean(status.ephemeral),
      model_id: status.model_id || null,
      mode: status.mode || null,
    })
      .then((created) => navigate(`#/task/${encodeURIComponent(created.id)}`))
      .catch((error) => chat.addNotice(`开新任务失败：${error.message}`, "error"));
  }
  if (outcome.action === "open_session" && outcome.argument) {
    navigate(`#/task/${encodeURIComponent(outcome.argument)}`);
  }
}

function interrupt() {
  runtime.socket?.send({ type: "interrupt" });
}

async function openAttachment(attachment) {
  const backend = runtime.backend;
  if (!backend || backend.status !== "ready" || !state.currentTaskId) {
    return;
  }
  const url = `${backend.baseUrl}${attachmentUrl(state.currentTaskId, attachment.id)}?token=${encodeURIComponent(backend.token)}`;
  openExternal(url);
}

// 标题菜单：重命名 / 压缩 / 删除

function startRename() {
  hideChatMenu();
  if (!state.currentTaskId) {
    return;
  }
  const input = el("input", "title-input");
  input.value = elements.chatTitle.textContent;
  input.style.width = "320px";
  input.style.height = "28px";
  elements.chatTitle.replaceChildren(input);
  input.focus();
  input.select();

  let finished = false;
  const finish = async (save) => {
    if (finished) {
      return;
    }
    finished = true;
    const title = input.value.trim();
    elements.chatTitle.textContent = runtime.lastChatStatus?.title || "";
    if (save && title && title !== runtime.lastChatStatus?.title) {
      try {
        const result = await sendJson(`/api/sessions/${encodeURIComponent(state.currentTaskId)}`, "PATCH", { title });
        elements.chatTitle.textContent = result.title || title;
        await refreshTasks();
      } catch (error) {
        chat.addNotice(`重命名失败：${error.message}`, "error");
      }
    }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      finish(true);
    } else if (event.key === "Escape") {
      event.stopPropagation();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
}

async function deleteCurrentTask() {
  hideChatMenu();
  const taskId = state.currentTaskId;
  if (!taskId) {
    return;
  }
  if (!window.confirm("删除这个任务？对话记录、附件和变更备份都会删掉，工作区里的文件不动。")) {
    return;
  }
  try {
    await sendJson(`/api/sessions/${encodeURIComponent(taskId)}`, "DELETE");
  } catch (error) {
    chat.addNotice(`删除失败：${error.message}`, "error");
    return;
  }
  leaveTask();
  navigate("#/new");
  await refreshTasks();
}

// ---- 后端状态 ----

function updateBackendIndicator(backend) {
  runtime.backend = backend;
}

function handleBackendState(backend) {
  const wasReady = runtime.backendReady;
  runtime.backend = backend;
  runtime.backendReady = backend.status === "ready";

  if (backend.status === "ready") {
    hideStartup();
    if (!wasReady) {
      bootstrap().catch((error) => showStartupError(`初始化失败：${error.message}`));
    }
    return;
  }

  if (runtime.socket) {
    closeSocket();
  }
  if (backend.status === "exited") {
    showStartupError(backend.error || "后端已退出");
    return;
  }
  showStartup("正在启动后端…", "第一次启动要读取模型配置，稍等几秒。");
}

async function bootstrap() {
  await reloadInfo();
  await refreshTasks();
  route();
  startTaskPolling();
}

async function reloadInfo() {
  state.info = await request("/api/info");
  emit("info-changed");
  applyInfoToComposers();
}

function applyInfoToComposers() {
  const info = state.info;
  if (!info) {
    return;
  }
  for (const composer of [newTaskComposer, chatComposer]) {
    composer.setModes(info.modes);
    composer.setModels(info.models, info.fallback_model);
    composer.setCommands(info.commands || []);
  }
  if (runtime.lastChatStatus) {
    applyChatStatus(runtime.lastChatStatus);
  }
}

async function refreshTasks() {
  const data = await request("/api/sessions");
  state.tasks = data.sessions || [];
  emit("tasks-changed");
}

function startTaskPolling() {
  if (runtime.pollingTimer) {
    return;
  }
  runtime.pollingTimer = setInterval(() => {
    if (runtime.backendReady && document.visibilityState === "visible") {
      refreshTasks().catch(() => {});
    }
  }, TASK_POLL_MILLISECONDS);
}

function showStartup(title, detail) {
  elements.startupOverlay.classList.remove("hidden");
  elements.startupTitle.textContent = title;
  elements.startupDetail.textContent = detail;
  elements.startupActions.classList.add("hidden");
}

function showStartupError(message) {
  elements.startupOverlay.classList.remove("hidden");
  elements.startupTitle.textContent = "后端没起来";
  elements.startupDetail.textContent = message;
  elements.startupActions.classList.remove("hidden");
}

function hideStartup() {
  elements.startupOverlay.classList.add("hidden");
}

function appendStartupLog(line) {
  const lines = `${elements.startupLog.textContent}${line}\n`.split("\n");
  elements.startupLog.textContent = lines.slice(-300).join("\n");
  elements.startupLog.scrollTop = elements.startupLog.scrollHeight;
}

function toast(text, kind = "") {
  elements.toast.textContent = text;
  elements.toast.className = `toast ${kind}`.trim();
  show(elements.toast, true);
  clearTimeout(runtime.toastTimer);
  runtime.toastTimer = setTimeout(() => show(elements.toast, false), 2400);
}

// ---- 布局 ----

function toggleSidebar() {
  const hidden = elements.app.classList.toggle("sidebar-hidden");
  savePreference("sidebarHidden", hidden);
}

function bindLayoutControls() {
  elements.app.classList.toggle("sidebar-hidden", loadPreference("sidebarHidden", false));
  elements.app.classList.toggle("right-panel-hidden", !loadPreference("rightPanel", true));

  // 很窄的窗口先收起侧栏（恢复宽度后回到记住的偏好），别让内容挤成一团
  const narrowQuery = window.matchMedia("(max-width: 960px)");
  let collapsedByNarrow = false;
  const applyNarrow = (matches) => {
    if (matches) {
      elements.app.classList.add("sidebar-hidden");
      collapsedByNarrow = true;
      return;
    }
    if (collapsedByNarrow) {
      collapsedByNarrow = false;
      elements.app.classList.toggle("sidebar-hidden", loadPreference("sidebarHidden", false));
      savePreference("sidebarHidden", false);
    }
  };
  narrowQuery.addEventListener("change", (event) => applyNarrow(event.matches));
  applyNarrow(narrowQuery.matches);

  elements.toggleSidebarButton.addEventListener("click", toggleSidebar);
  for (const opener of document.querySelectorAll("[data-open-sidebar]")) {
    opener.addEventListener("click", () => {
      elements.app.classList.remove("sidebar-hidden");
      savePreference("sidebarHidden", false);
    });
  }
  elements.toggleRightPanel.addEventListener("click", () => {
    const hidden = elements.app.classList.toggle("right-panel-hidden");
    savePreference("rightPanel", !hidden);
  });
}

function fillStaticIcons() {
  elements.toggleSidebarButton.append(icon("panelLeft"));
  for (const opener of document.querySelectorAll("[data-open-sidebar]")) {
    opener.append(icon("panelLeft"));
  }
  elements.chatMenuButton.append(icon("more"));
  elements.toggleRightPanel.append(icon("panelRight"));
  document.getElementById("new-automation").prepend(icon("plus"));
  document.getElementById("settings-close").append(icon("close"));
  document.getElementById("file-close").append(icon("close"));
}

function bindChatMenu() {
  elements.chatMenuButton.addEventListener("click", (event) => {
    event.stopPropagation();
    elements.chatMenu.classList.toggle("hidden");
  });
  document.addEventListener("click", () => hideChatMenu());
  elements.chatMenu.querySelector('[data-action="rename"]').addEventListener("click", startRename);
  elements.chatMenu.querySelector('[data-action="compact"]').addEventListener("click", () => {
    hideChatMenu();
    runtime.socket?.send({ type: "compact" });
  });
  elements.chatMenu.querySelector('[data-action="delete"]').addEventListener("click", deleteCurrentTask);
}

function bindGlobalShortcuts() {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (isSettingsOpen()) {
        closeSettings();
        return;
      }
      if (isFilePreviewOpen()) {
        closeFilePreview();
        return;
      }
      if (isFolderPickerOpen()) {
        closeFolderPicker();
        return;
      }
      if (isAutomationEditorOpen()) {
        closeEditor();
        return;
      }
      if (chat.running) {
        interrupt();
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
      event.preventDefault();
      navigate("#/new");
    }
  });
}

// ---- 启动 ----

function initSidebarHandlers() {
  initSidebar({
    onNewTask: (workspace) => navigate(workspace ? `#/new?workspace=${encodeURIComponent(workspace)}` : "#/new"),
    onOpenTask: (taskId) => navigate(`#/task/${encodeURIComponent(taskId)}`),
    onAutomations: () => navigate("#/automations"),
    onSettings: (tab) => openSettings(tab),
    onTaskDeleted: async (taskId) => {
      if (taskId === state.currentTaskId) {
        leaveTask();
        navigate("#/new");
      }
      await refreshTasks();
    },
    onReloadInfo: () => reloadInfo(),
    onWorkspaceAdded: async (path) => {
      await reloadInfo();
      navigate(path ? `#/new?workspace=${encodeURIComponent(path)}` : "#/new");
    },
  });
}

function initFileCite() {
  on("cite-file", ({ workspace, path }) => {
    const target = parseRoute();
    if (target.view === "chat") {
      chatComposer.insertText(citationText(path));
      return;
    }
    if (target.view !== "new") {
      navigate(`#/new?workspace=${encodeURIComponent(workspace)}`);
      return;
    }
    newTaskComposer.setWorkspaces(state.info.workspaces, workspace);
    newTaskComposer.insertText(citationText(path));
  });
}

async function init() {
  if (navigator.userAgent.includes("Mac")) {
    document.body.classList.add("platform-darwin");
  }
  fillStaticIcons();
  bindLayoutControls();
  bindChatMenu();
  bindGlobalShortcuts();
  initSidebarHandlers();
  initFilePreview();
  initFileCite();
  initAutomations({ onOpenTask: (taskId) => navigate(`#/task/${encodeURIComponent(taskId)}`) });
  initSettings({
    onSaved: async () => {
      await reloadInfo();
      await refreshTasks();
      if (parseRoute().view === "new") {
        prepareNewTask(null);
      }
      toast("设置已保存");
    },
  });
  initResizers();
  updateChatMenuVisibility();

  elements.restartButton.addEventListener("click", () => {
    elements.startupActions.classList.add("hidden");
    restartBackend();
  });

  window.addEventListener("hashchange", route);
  onBackendState(handleBackendState);
  onBackendLog(appendStartupLog);

  try {
    const backend = await getBackendState();
    handleBackendState(backend);
  } catch (error) {
    showStartupError(`拿不到后端状态：${error.message}`);
  }
}

init().catch((error) => {
  document.body.prepend(el("div", "error-text", `页面初始化失败：${error.message}`));
});
