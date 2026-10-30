// 任务页：中间的对话。一个任务一条 WebSocket，协议见 pocketagent/server/__init__.py。

import { attachmentUrl, requestJson, sendJson, sessionFileUrl, socketUrl, uploadFiles } from "../core/api.js";
import { looksLikeCommand, loadSkillNames, runCommand } from "../core/commands.js";
import { Composer } from "./composer.js";
import { button, el, icon, isImagePath, renderDiff, show } from "../ui/dom.js";
import { renderMarkdown } from "../ui/markdown.js";
import { ActivityFold, dissolveFolds } from "./activity-fold.js";
import { renderQuestionCard } from "./questions.js";
import { emit, reloadTasks, state } from "../core/state.js";

const elements = {};

const chat = {
  socket: null,
  reconnectDelay: 1000,
  running: false,
  status: null,
  composer: null,
  // 正在流式输出的回复块和思考块
  currentAssistant: null,
  currentReasoning: null,
  // 当前这一块折叠：一轮里的工具调用和思考都收进去，AI 一说话就收尾、另起一块
  fold: null,
  foldEnabled: true,
  // 每次工具调用落在哪一块折叠里，结果回来时要把数字记到那一块上
  callFolds: new Map(),
  toolCards: new Map(),
  approvalCards: new Map(),
  questionCards: new Map(),
  // 计划卡：建议切计划模式 / 提交计划等批准
  planCards: new Map(),
  // 「任务计划」面板：todo_write 维护，一张卡片原地更新
  todoPanel: null,
  // 本轮从哪个子元素开始画的，用来判断图片是不是已经在回复里显示过
  turnStartChildCount: null,
  // 本轮最后一条收尾的回复块，turn_end 判断要不要挂「被掐断」角标用
  lastReply: null,
  // 新任务页发的第一句话：等连上、拿到快照后再发
  pendingFirstMessage: null,
  // 对话末尾的「等待中」提示
  activity: null,
};

const TOOL_STATUS_ICONS = { running: "●", ok: "✓", error: "✗", denied: "⊘" };

function compactionNotice(event) {
  const sizes = `约 ${event.tokens_before} → ${event.tokens_after} tokens`;
  if (event.reason === "overflow") {
    return `模型说上下文超长，已压缩后重试（${sizes}）`;
  }
  if (event.reason === "manual") {
    return `已按你的要求压缩上下文（${sizes}）`;
  }
  return event.background ? `上下文已在后台压缩，没打断对话（${sizes}）` : `上下文已压缩（${sizes}）`;
}
const FILE_TOOLS = new Set(["edit_file", "write_file", "bash"]);
const TURN_END_NOTICES = {
  interrupted: ["已中断，可以接着说", "warning"],
  denied: ["调用被拒绝，这一轮先停下。告诉 agent 接下来怎么做。", "warning"],
};

// 回复被上游掐断时的角标，挂在回复内容末尾。
// 不给这个标志，用户看到的就是一句没说完的话，会以为模型只是话说到一半。
const CUT_OFF_BADGES = {
  filtered: ["内容审核中断", "上游的内容审核在句子中间掐断了这次生成，下面是已经输出的部分"],
  incomplete: ["输出中断", "连接中途断了，这次回复不完整"],
  truncated: ["超出长度上限", "回复太长被截断，后面没有写完"],
};

function buildCutOffBadge(stopReason) {
  const badge = CUT_OFF_BADGES[stopReason];
  if (!badge) {
    return null;
  }
  const element = el("div", `reply-cut-off ${stopReason}`);
  element.append(el("span", "reply-cut-off-title", `■ ${badge[0]}`), el("span", "reply-cut-off-note", badge[1]));
  return element;
}

// 这一轮是被上游掐断的：角标挂在最后一条回复末尾；一条回复都没有就退化成一行提示
function markCutOffReply(stopReason) {
  const labels = CUT_OFF_BADGES[stopReason];
  if (!labels) {
    return;
  }
  if (!chat.lastReply) {
    addNotice(`${labels[0]}：${labels[1]}`, "warning");
    return;
  }
  chat.lastReply.appendChild(buildCutOffBadge(stopReason));
  if (isNearBottom()) {
    scrollToBottom();
  }
}

export function initChat({ onDeleted }) {
  elements.view = document.getElementById("chat-view");
  elements.title = document.getElementById("chat-title");
  elements.menuButton = document.getElementById("chat-menu-button");
  elements.menu = document.getElementById("chat-menu");
  elements.transcript = document.getElementById("transcript");
  elements.transcriptInner = document.getElementById("transcript-inner");
  elements.banner = document.getElementById("connection-banner");
  elements.pendingInputs = document.getElementById("pending-inputs");

  chat.composer = new Composer(document.getElementById("chat-composer"), {
    kind: "chat",
    placeholder: "接着说…  Enter 发送，Shift+Enter 换行，图片可以直接粘贴",
    onSubmit: sendUserMessage,
    onStop: interrupt,
    onModeChange: (mode) => sendToServer({ type: "set_mode", mode }),
    onModelChange: (modelId) => sendToServer({ type: "set_model", model_id: modelId }),
    onAgentModeChange: (mode) => sendToServer({ type: "set_agent_mode", mode }),
  });

  elements.menuButton.addEventListener("click", (event) => {
    event.stopPropagation();
    elements.menu.classList.toggle("hidden");
  });
  document.addEventListener("click", () => elements.menu.classList.add("hidden"));
  elements.menu.querySelector('[data-action="rename"]').addEventListener("click", startRename);
  elements.menu.querySelector('[data-action="compact"]').addEventListener("click", () => sendToServer({ type: "compact" }));
  elements.menu.querySelector('[data-action="delete"]').addEventListener("click", () => deleteTask(onDeleted));
}

export function updateChatOptions(info) {
  chat.composer.setModes(info.modes);
  chat.composer.setModels(info.models, info.fallback_model);
  chat.composer.setCommands(info.commands || []);
  setFoldEnabled(info.fold_tool_calls !== false);
  if (chat.status) {
    applyStatus(chat.status);
  }
}

// 设置里关掉「工具调用收进折叠」：已经画出来的折叠就地摊平，之后平铺；重新打开时只影响后面
function setFoldEnabled(enabled) {
  if (enabled === chat.foldEnabled) {
    return;
  }
  chat.foldEnabled = enabled;
  closeActivityFold();
  if (!enabled) {
    dissolveFolds(elements.transcriptInner);
  }
}

export function isChatRunning() {
  return chat.running;
}

export function chatComposer() {
  return chat.composer;
}

// ===================== 打开 / 离开任务 =====================

export function openTask(taskId, firstMessage) {
  if (firstMessage) {
    chat.pendingFirstMessage = firstMessage;
  }
  if (chat.socket && state.currentTaskId === taskId) {
    return;
  }
  closeSocket();
  state.currentTaskId = taskId;
  clearTranscript();
  chat.status = null;
  elements.title.textContent = "";
  connect(taskId);
}

export function leaveTask() {
  closeSocket();
  state.currentTaskId = null;
  chat.status = null;
}

function closeSocket() {
  if (chat.socket) {
    const previous = chat.socket;
    chat.socket = null;
    previous.close();
  }
}

function connect(taskId) {
  const socket = new WebSocket(socketUrl(taskId));
  chat.socket = socket;
  socket.addEventListener("open", () => {
    show(elements.banner, false);
    chat.reconnectDelay = 1000;
  });
  socket.addEventListener("message", (event) => handleServerMessage(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    if (chat.socket !== socket) {
      return;
    }
    show(elements.banner, true);
    setTimeout(() => {
      if (chat.socket === socket) {
        connect(taskId);
      }
    }, chat.reconnectDelay);
    chat.reconnectDelay = Math.min(chat.reconnectDelay * 2, 10000);
  });
}

function sendToServer(message) {
  if (!chat.socket || chat.socket.readyState !== WebSocket.OPEN) {
    addNotice("还没连上服务，稍等再试", "warning");
    return false;
  }
  chat.socket.send(JSON.stringify(message));
  return true;
}

async function sendUserMessage({ text, files, attachmentIds = [] }) {
  if (!chat.socket || chat.socket.readyState !== WebSocket.OPEN) {
    addNotice("还没连上服务，稍等再试", "warning");
    return false;
  }
  // 斜杠命令本地消化，不进对话（/技能名 除外，它要交给 agent）
  if (text && files.length === 0 && attachmentIds.length === 0 && looksLikeCommand(text)) {
    const outcome = await runCommand(text, { sessionId: state.currentTaskId }).catch((error) => ({
      handled: true,
      text: `命令执行失败：${error.message}`,
      kind: "error",
    }));
    if (outcome.handled) {
      showCommandOutput(outcome);
      return true;
    }
  }
  let attachments = [];
  try {
    attachments = await uploadFiles(state.currentTaskId, files);
  } catch (error) {
    addNotice(`附件上传失败：${error.message}`, "error");
    return false;
  }
  return sendToServer({
    type: "user_message",
    text,
    attachment_ids: [...attachments.map((item) => item.id), ...attachmentIds],
  });
}

// 命令的输出画在对话里；有跳转 / 开新会话动作的接着做
function showCommandOutput(outcome) {
  if (outcome.text) {
    appendToTranscript(el("div", `notice command-output ${outcome.kind || ""}`, outcome.text));
  }
  if (outcome.action === "new_session") {
    // 和命令行 /new 一样：同工作区开个新任务
    const status = chat.status || {};
    sendJson("api/sessions", "POST", {
      workspace: status.ephemeral ? null : status.workspace,
      ephemeral: Boolean(status.ephemeral),
      model_id: status.model_id || null,
      mode: status.mode || null,
    })
      .then((created) => {
        window.location.hash = `#/task/${encodeURIComponent(created.id)}`;
      })
      .catch((error) => addNotice(`开新会话失败：${error.message}`, "error"));
    return;
  }
  if (outcome.action === "open_session" && outcome.argument) {
    window.location.hash = `#/task/${encodeURIComponent(outcome.argument)}`;
  }
  if (outcome.action === "switch_agent_mode" && outcome.argument) {
    chat.composer.setAgentMode(outcome.argument);
  }
}

export function interrupt() {
  if (chat.running) {
    sendToServer({ type: "interrupt" });
  }
}

// ===================== 服务端消息 =====================

function handleServerMessage(message) {
  switch (message.type) {
    case "snapshot":
      renderSnapshot(message);
      sendPendingFirstMessage();
      break;
    case "state": {
      const wasRunning = chat.running;
      applyStatus(message);
      if (wasRunning && !message.running) {
        emit("turn-finished", state.currentTaskId);
      }
      break;
    }
    case "event":
      handleAgentEvent(message.event);
      break;
    case "approval_request":
      showApproval(message);
      refreshTaskList();
      break;
    case "approval_resolved":
      resolveApproval(message.request_id);
      refreshTaskList();
      break;
    case "question_request":
      showQuestions(message);
      refreshTaskList();
      break;
    case "question_resolved":
      resolveQuestions(message.request_id);
      refreshTaskList();
      break;
    case "plan_request":
      showPlanRequest(message);
      refreshTaskList();
      break;
    case "plan_resolved":
      resolvePlan(message);
      refreshTaskList();
      break;
    case "returned_inputs":
      chat.composer.restoreTexts(message.inputs.map((input) => input.text));
      break;
    case "error":
      addNotice(message.message, "error");
      break;
    default:
      break;
  }
}

// 侧栏的「等你回答 / 等你确认」角标要马上变，不用等 5 秒一次的轮询
function refreshTaskList() {
  reloadTasks().catch(() => {});
}

async function sendPendingFirstMessage() {
  const firstMessage = chat.pendingFirstMessage;
  if (!firstMessage) {
    return;
  }
  chat.pendingFirstMessage = null;
  const sent = await sendUserMessage(firstMessage);
  if (!sent) {
    chat.composer.restoreTexts([firstMessage.text]);
  }
}

function applyStatus(status) {
  chat.status = status;
  chat.running = status.running;
  elements.title.textContent = status.title;
  document.title = `${status.title} · PocketAgent`;
  chat.composer.setMode(status.mode);
  chat.composer.setModel(status.model_id);
  chat.composer.setAgentMode(status.agent_mode || "agent");
  chat.composer.setRunning(status.running, state.info?.busy_input_mode);
  chat.composer.setContext(status.context_tokens, status.context_window, status.compact_threshold);
  chat.composer.setCompacting(status.compacting);
  chat.composer.setWorkspaceLabel(status.workspace, status.ephemeral, status.worktree);
  loadSkillNames(status.workspace).then((names) => chat.composer.setSkillNames(names));
  renderPendingInputs(status);
  emit("task-status", status);
}

function renderPendingInputs(status) {
  elements.pendingInputs.replaceChildren();
  status.interjections.forEach((input) => {
    const row = el("div", "pending-input");
    row.append(el("span", "pending-badge", "插话"), el("span", "pending-text", input.text || "（附件）"), el("span", "pending-note", "模型下一步会看到"));
    elements.pendingInputs.appendChild(row);
  });
  status.queued.forEach((input, index) => {
    const row = el("div", "pending-input");
    const remove = button("pending-remove", "", "close", "从队列里删掉");
    remove.addEventListener("click", () => sendToServer({ type: "remove_queued", index }));
    row.append(el("span", "pending-badge queued", "排队"), el("span", "pending-text", input.text || "（附件）"), remove);
    elements.pendingInputs.appendChild(row);
  });
  show(elements.pendingInputs, status.interjections.length + status.queued.length > 0);
}

function handleAgentEvent(event) {
  switch (event.type) {
    case "turn_start":
      finishStreamingBlocks();
      closeActivityFold();
      chat.turnStartChildCount = elements.transcriptInner.children.length;
      chat.lastReply = null;
      addUserMessage(event.user_text, event.attachments, "", event.message_id);
      showActivity("等待模型响应");
      break;
    case "interjection_applied":
      finishStreamingBlocks();
      addUserMessage(event.user_text, event.attachments, "插话", event.message_id);
      showActivity("等待模型响应");
      // 已经交给模型了，从输入框上方的「待发」里拿掉
      if (chat.status?.interjections.length) {
        chat.status.interjections.shift();
        renderPendingInputs(chat.status);
      }
      break;
    case "text_delta":
      appendAssistantText(event.text);
      break;
    case "reasoning_delta":
      appendReasoning(event.text);
      break;
    case "llm_response":
      finishStreamingBlocks();
      emit("model-responded", state.currentTaskId);
      break;
    case "llm_retry":
      discardPartialAssistant();
      addNotice(`模型调用失败（${event.error}），${event.wait_seconds} 秒后第 ${event.attempt} 次尝试`, "warning");
      showActivity("重试中");
      break;
    case "llm_truncated":
      finishStreamingBlocks();
      if (event.gave_up) {
        addNotice("回复多次超出输出长度上限，先停下了", "warning");
      } else if (event.had_tool_calls) {
        addNotice("回复超出输出长度上限被截断，工具调用不完整没有执行，已让模型拆小重来", "warning");
      } else {
        addNotice("回复超出输出长度上限被截断，已让模型接着写", "warning");
      }
      showActivity("等待模型响应");
      break;
    case "tool_call":
      finishStreamingBlocks();
      hideActivity();
      addToolCard(event.call_id, event.name, event.summary);
      break;
    case "tool_output_delta":
      appendToolOutput(event.call_id, event.text);
      break;
    case "subagent_progress":
      addSubagentStep(event.call_id, event);
      break;
    case "tool_review":
      setToolReview(event.call_id, { allow: event.allow, reason: event.reason });
      break;
    case "tool_result":
      setToolResult(event.call_id, event.content, event.is_error, event.denied, event.name);
      if (chat.running && !event.denied) {
        showActivity("等待模型响应");
      }
      if (FILE_TOOLS.has(event.name)) {
        emit("files-changed", state.currentTaskId);
      }
      if (!event.denied && event.content) {
        emit("task-links", { taskId: state.currentTaskId, links: extractLocalLinks(event.content) });
      }
      break;
    case "todos_updated":
      renderTodoPanel(event.todos);
      break;
    case "checkpoint":
      // 每轮开始前拍的检查点，右侧栏的检查点列表跟着刷新
      emit("files-changed", state.currentTaskId);
      break;
    case "compaction_waiting":
      showActivity("上下文快满了，等后台压缩做完");
      break;
    case "compaction_failed":
      addNotice(`后台压缩失败：${event.message}（下次超过阈值会再试）`, "warning");
      break;
    case "compaction":
      addNotice(compactionNotice(event));
      if (chat.running) {
        showActivity("等待模型响应");
      }
      break;
    case "step_limit":
      addNotice(`这一轮已调用模型 ${event.max_steps} 次，达到上限，先停下`, "warning");
      break;
    case "error":
      addNotice(event.message, "error");
      break;
    case "turn_end": {
      finishStreamingBlocks();
      closeActivityFold();
      hideActivity();
      markCutOffReply(event.stop_reason);
      const notice = TURN_END_NOTICES[event.stop_reason];
      if (notice) {
        addNotice(notice[0], notice[1]);
      }
      emit("turn-ended", { taskId: state.currentTaskId, finalText: event.final_text });
      showTurnImages();
      break;
    }
    default:
      break;
  }
}

// ===================== 对话区渲染 =====================

function isNearBottom() {
  const transcript = elements.transcript;
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 120;
}

function scrollToBottom() {
  elements.transcript.scrollTop = elements.transcript.scrollHeight;
}

function appendToTranscript(element) {
  const shouldScroll = isNearBottom();
  elements.transcriptInner.querySelector(".empty-chat")?.remove();
  // 「等待中」提示始终在最下面
  const activityElement = chat.activity?.element.isConnected ? chat.activity.element : null;
  elements.transcriptInner.insertBefore(element, activityElement);
  if (shouldScroll) {
    scrollToBottom();
  }
  return element;
}

// 工具卡片 / 思考块放哪：折叠开着就塞进当前这一块，关掉就平铺进对话
function appendFolded(element) {
  if (!chat.foldEnabled) {
    appendToTranscript(element);
    return null;
  }
  if (!chat.fold || !chat.fold.element.isConnected) {
    chat.fold = new ActivityFold();
    appendToTranscript(chat.fold.element);
  }
  const shouldScroll = isNearBottom();
  chat.fold.append(element);
  if (shouldScroll) {
    scrollToBottom();
  }
  return chat.fold;
}

// AI 说话了，或者来了要露在外面的东西（提示、审批卡、计划面板）：这一块收尾，后面再开新的一块
function closeActivityFold() {
  if (!chat.fold) {
    return;
  }
  chat.fold.finish();
  chat.fold = null;
}

function clearTranscript() {
  hideActivity();
  if (chat.currentReasoning) {
    clearInterval(chat.currentReasoning.timer);
  }
  elements.transcriptInner.replaceChildren();
  chat.currentAssistant = null;
  chat.currentReasoning = null;
  chat.fold = null;
  chat.callFolds.clear();
  chat.turnStartChildCount = null;
  chat.lastReply = null;
  chat.toolCards.clear();
  chat.approvalCards.clear();
  chat.questionCards.clear();
  chat.planCards.clear();
  chat.todoPanel = null;
  show(elements.pendingInputs, false);
}

function renderAttachments(attachments) {
  const list = el("div", "message-attachments");
  for (const attachment of attachments) {
    const url = attachmentUrl(state.currentTaskId, attachment.id);
    const link = el("a", `message-attachment ${attachment.kind}`);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = attachment.name;
    if (attachment.kind === "image") {
      const image = el("img");
      image.src = url;
      image.alt = attachment.name;
      image.loading = "lazy";
      link.appendChild(image);
    } else {
      link.append(icon("file"), el("span", "", attachment.name));
    }
    list.appendChild(link);
  }
  return list;
}

function addUserMessage(text, attachments, badge, messageId) {
  closeActivityFold();
  const message = el("div", "message-user");
  if (badge) {
    message.appendChild(el("span", "message-badge", badge));
  }
  if (attachments && attachments.length > 0) {
    message.appendChild(renderAttachments(attachments));
  }
  if (text) {
    message.appendChild(el("div", "message-text", text));
  }
  if (messageId) {
    message.appendChild(buildRewindButton(messageId));
  }
  appendToTranscript(message);
}

// 用户消息上的「恢复到这里」：把这句之后的内容全删掉，原话放回输入框重新编辑
function buildRewindButton(messageId) {
  const rewind = button("message-rewind", "恢复到这里", "undo", "撤销这句话之后的对话，并把这句话放回输入框");
  rewind.addEventListener("click", () => rewindTo(messageId));
  return rewind;
}

async function rewindTo(messageId) {
  if (chat.running) {
    addNotice("任务还在运行，等它停下再恢复", "warning");
    return;
  }
  const confirmed = window.confirm(
    "恢复到这里？\n这句话之后的对话会全部删掉，这句话和附件放回输入框。工作区里的文件不动。"
  );
  if (!confirmed) {
    return;
  }
  const taskId = state.currentTaskId;
  let result;
  try {
    result = await sendJson(`api/sessions/${encodeURIComponent(taskId)}/rewind`, "POST", { message_id: messageId });
  } catch (error) {
    if (error.status === 409 && taskId === state.currentTaskId) {
      // 对话已经变了（比如压缩过），把界面拉回服务端的样子再提示
      const snapshot = await requestJson(`api/sessions/${encodeURIComponent(taskId)}`).catch(() => null);
      if (snapshot && taskId === state.currentTaskId) {
        renderSnapshot(snapshot);
      }
    }
    addNotice(`恢复失败：${error.message}`, "error");
    return;
  }
  if (taskId !== state.currentTaskId) {
    return;
  }
  renderSnapshot(result.snapshot);
  chat.composer.restoreMessage(result.message);
}

function addAssistantMessage(text, finish = "") {
  closeActivityFold();
  const element = el("div", "message-assistant");
  element.innerHTML = renderMarkdown(text, { imageUrl: chatImageUrl });
  const badge = buildCutOffBadge(finish);
  if (badge) {
    // 历史里重放时也把「这条是被掐断的」标出来
    element.appendChild(badge);
  }
  appendToTranscript(element);
}

function addSummaryCard(text) {
  closeActivityFold();
  const details = el("details", "summary-card");
  details.appendChild(el("summary", "", "之前的对话已压缩，点开看摘要"));
  const body = el("div", "message-assistant");
  body.innerHTML = renderMarkdown(text, { imageUrl: chatImageUrl });
  details.appendChild(body);
  appendToTranscript(details);
}

// 回复里的 ![说明](路径)：工作区内的相对路径换成会话取图地址，临时任务也能显示
function chatImageUrl(source) {
  const workspace = chat.status?.workspace;
  if (!workspace) {
    return null;
  }
  let relative = source;
  const root = workspace.replace(/\/+$/, "");
  if (relative.startsWith(`${root}/`)) {
    relative = relative.slice(root.length + 1);
  }
  if (relative.startsWith("./")) {
    relative = relative.slice(2);
  }
  if (!relative || relative.startsWith("/") || relative.startsWith("~") || /^[a-z][a-z0-9+.-]*:/i.test(relative)) {
    return null;
  }
  return sessionFileUrl(state.currentTaskId, relative);
}

// 一轮结束时，把本轮新增 / 改过的图片补在回复下面；回复里已经贴过的就不重复
async function showTurnImages() {
  const taskId = state.currentTaskId;
  if (!taskId) {
    return;
  }
  let result;
  try {
    result = await requestJson(`api/sessions/${encodeURIComponent(taskId)}/changes/turn`);
  } catch (error) {
    return;
  }
  if (taskId !== state.currentTaskId) {
    return;
  }
  const seen = new Set();
  const images = [];
  for (const change of result.changes || []) {
    if (change.status === "deleted" || !isImagePath(change.path) || seen.has(change.path)) {
      continue;
    }
    seen.add(change.path);
    images.push(change.path);
  }
  if (images.length === 0) {
    return;
  }
  const shown = shownImageUrlsSince(chat.turnStartChildCount);
  const missing = images.filter((path) => !shown.has(sessionFileUrl(taskId, path)));
  if (missing.length > 0) {
    appendToTranscript(renderTurnImages(taskId, missing));
  }
}

function shownImageUrlsSince(startIndex) {
  const urls = new Set();
  const children = [...elements.transcriptInner.children];
  const start = Number.isInteger(startIndex) ? startIndex : 0;
  for (const child of children.slice(start)) {
    child.querySelectorAll("img").forEach((image) => {
      const source = image.getAttribute("src");
      if (source) {
        urls.add(source);
      }
    });
  }
  return urls;
}

function renderTurnImages(taskId, paths) {
  const wrapper = el("div", "turn-images");
  wrapper.appendChild(el("div", "turn-images-title", "本轮产出的图片"));
  const grid = el("div", "turn-images-grid");
  for (const path of paths) {
    const url = sessionFileUrl(taskId, path);
    const link = el("a", "turn-image");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = path;
    const image = el("img");
    image.src = url;
    image.alt = path;
    image.loading = "lazy";
    link.append(image, el("span", "turn-image-path", path));
    grid.appendChild(link);
  }
  wrapper.appendChild(grid);
  return wrapper;
}

function addNotice(text, kind) {
  closeActivityFold();
  appendToTranscript(el("div", `notice ${kind || ""}`, text));
}

// ---- 流式输出：打字机节奏 ----
// 网关常常一阵一阵地推（实测几十个片段同时到，然后停 100ms），直接画会一顿一顿的。
// 收到的字先进缓冲，每帧按积压量放出一部分：积压少就慢慢出，积压多就加快追上。

const TYPEWRITER_FRAMES_TO_CATCH_UP = 8;
const TYPEWRITER_MIN_CHARS_PER_FRAME = 2;
const REASONING_PREVIEW_CHARS = 48;

function appendAssistantText(delta) {
  hideActivity();
  if (!chat.currentAssistant) {
    // 模型开口说话：前面那一块工具调用收尾，这段话落在折叠外面，后面再开新的一块
    closeActivityFold();
    const element = appendToTranscript(el("div", "message-assistant streaming"));
    chat.currentAssistant = { element, shown: "", pending: "", frameScheduled: false };
  }
  const block = chat.currentAssistant;
  block.pending += delta;
  scheduleTypewriter(block);
}

function scheduleTypewriter(block) {
  if (block.frameScheduled) {
    return;
  }
  block.frameScheduled = true;
  requestAnimationFrame(() => typewriterFrame(block));
}

function typewriterFrame(block) {
  block.frameScheduled = false;
  if (!block.pending) {
    return;
  }
  const take = Math.max(TYPEWRITER_MIN_CHARS_PER_FRAME, Math.ceil(block.pending.length / TYPEWRITER_FRAMES_TO_CATCH_UP));
  block.shown += block.pending.slice(0, take);
  block.pending = block.pending.slice(take);
  renderStreamingBlock(block);
  if (block.pending) {
    scheduleTypewriter(block);
  }
}

function renderStreamingBlock(block) {
  const shouldScroll = isNearBottom();
  block.element.innerHTML = renderMarkdown(block.shown, { imageUrl: chatImageUrl });
  if (shouldScroll) {
    scrollToBottom();
  }
}

function flushAssistantBlock(block) {
  block.shown += block.pending;
  block.pending = "";
  renderStreamingBlock(block);
  block.element.classList.remove("streaming");
  chat.lastReply = block.element;
}

function elapsedSeconds(startedAt) {
  return ((performance.now() - startedAt) / 1000).toFixed(1);
}

function appendReasoning(delta) {
  hideActivity();
  if (!chat.currentReasoning) {
    const details = el("details", "reasoning live");
    const summary = el("summary");
    const label = el("span", "reasoning-label", "思考中");
    const preview = el("span", "reasoning-preview");
    summary.append(label, preview);
    const body = el("div", "reasoning-body");
    const bodyText = document.createTextNode("");
    body.appendChild(bodyText);
    details.append(summary, body);
    // 思考也收进折叠：折叠头上实时显示「思考中 3.2s」和最新一句
    const fold = appendFolded(details);
    fold?.startThinking();
    const block = { details, label, preview, bodyText, fold, length: 0, tail: "", startedAt: performance.now() };
    // 计时和最新一句每 0.1 秒刷新一次，思考很长时也不卡
    block.timer = setInterval(() => updateReasoningLabel(block), 100);
    chat.currentReasoning = block;
  }
  const block = chat.currentReasoning;
  block.bodyText.appendData(delta);
  block.length += delta.length;
  block.tail = (block.tail + delta).slice(-REASONING_PREVIEW_CHARS * 2);
  block.fold?.updateThinking(delta);
}

function updateReasoningLabel(block) {
  block.label.textContent = `思考中 ${elapsedSeconds(block.startedAt)}s`;
  block.preview.textContent = block.tail.replace(/\s+/g, " ").trim().slice(-REASONING_PREVIEW_CHARS);
}

function finishReasoningBlock(block) {
  clearInterval(block.timer);
  block.details.classList.remove("live");
  block.label.textContent = `思考了 ${elapsedSeconds(block.startedAt)}s（${block.length} 字）`;
  block.preview.textContent = "";
  block.fold?.finishThinking();
}

function finishStreamingBlocks() {
  if (chat.currentReasoning) {
    finishReasoningBlock(chat.currentReasoning);
  }
  if (chat.currentAssistant) {
    flushAssistantBlock(chat.currentAssistant);
  }
  chat.currentAssistant = null;
  chat.currentReasoning = null;
}

function discardPartialAssistant() {
  // 模型调用失败要重试时，丢掉已经流出来的半截回复
  if (chat.currentReasoning) {
    clearInterval(chat.currentReasoning.timer);
    chat.currentReasoning.details.remove();
  }
  chat.currentAssistant?.element.remove();
  chat.currentAssistant = null;
  chat.currentReasoning = null;
  // 半截回复丢了以后折叠可能空了，别留一个空壳
  if (chat.fold && chat.fold.isEmpty()) {
    chat.fold.element.remove();
    chat.fold = null;
  }
}

// ---- 「等待中」提示：等第一个字、工具跑完等模型、等后台压缩时，让人知道没卡住 ----

function showActivity(text) {
  if (!chat.activity) {
    const element = el("div", "activity");
    const label = el("span", "activity-label");
    const seconds = el("span", "activity-seconds");
    element.append(el("span", "activity-dots"), label, seconds);
    chat.activity = { element, label, seconds, startedAt: 0, timer: null };
  }
  const activity = chat.activity;
  activity.label.textContent = text;
  activity.startedAt = performance.now();
  activity.seconds.textContent = "";
  clearInterval(activity.timer);
  activity.timer = setInterval(() => {
    activity.seconds.textContent = `${elapsedSeconds(activity.startedAt)}s`;
  }, 200);
  const shouldScroll = isNearBottom();
  elements.transcriptInner.appendChild(activity.element);
  if (shouldScroll) {
    scrollToBottom();
  }
}

function hideActivity() {
  if (!chat.activity) {
    return;
  }
  clearInterval(chat.activity.timer);
  chat.activity.element.remove();
}

function setToolStatus(card, status) {
  card.classList.remove("status-running", "status-ok", "status-error", "status-denied");
  card.classList.add(`status-${status}`);
  card.querySelector(".tool-status").textContent = TOOL_STATUS_ICONS[status];
}

function addToolCard(callId, name, summary) {
  const card = el("div", "tool-card");
  const header = el("button", "tool-header");
  header.type = "button";
  header.append(
    el("span", "tool-status"),
    el("span", "tool-name", name),
    el("span", "tool-summary", summary),
    el("span", "tool-review hidden"),
    icon("chevronRight", "tool-chevron")
  );
  header.addEventListener("click", () => {
    card.classList.toggle("expanded");
    // 用户自己点过就听用户的：跑完不再自动收起
    delete card.dataset.autoExpanded;
  });
  const body = el("div", "tool-body");
  body.appendChild(el("pre", "", "运行中…"));
  card.append(header, body);
  setToolStatus(card, "running");
  chat.toolCards.set(callId, card);
  const fold = appendFolded(card);
  if (fold) {
    fold.recordCall(callId, name, summary);
    chat.callFolds.set(callId, fold);
  }
  return card;
}

// 子 agent 的进度：在它那张工具卡片下面列出最近几步（样式先写在这里，整理目录时挪进 style.css）
const SUBAGENT_STEPS_SHOWN = 4;

function addSubagentStep(callId, step) {
  const card = chat.toolCards.get(callId);
  if (!card) {
    return;
  }
  let steps = card.querySelector(".subagent-steps");
  if (!steps) {
    steps = el("div", "subagent-steps");
    steps.style.cssText = "padding: 2px 12px 8px 36px; font-size: 12px; color: var(--text-muted); font-family: var(--mono);";
    steps.dataset.count = "0";
    card.insertBefore(steps, card.querySelector(".tool-body"));
  }
  const count = Number(steps.dataset.count) + (step.step === "tool" ? 1 : 0);
  steps.dataset.count = String(count);
  const line = el("div", "subagent-step", `${step.step === "tool_error" ? "✗" : "↳"} ${step.name}  ${step.summary}`);
  line.style.cssText = "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;";
  if (step.step === "tool_error") {
    line.style.color = "var(--warning)";
  }
  steps.querySelector(".subagent-count")?.remove();
  steps.appendChild(line);
  const lines = [...steps.querySelectorAll(".subagent-step")];
  lines.slice(0, Math.max(0, lines.length - SUBAGENT_STEPS_SHOWN)).forEach((each) => each.remove());
  if (count > SUBAGENT_STEPS_SHOWN) {
    steps.prepend(el("div", "subagent-count", `子 agent 已经调了 ${count} 次工具，下面是最近几步`));
  }
}

function setToolReview(callId, review) {
  const card = chat.toolCards.get(callId);
  if (!card || !review) {
    return;
  }
  const badge = card.querySelector(".tool-review");
  badge.textContent = review.allow ? "AI 已放行" : "AI 未放行";
  badge.title = review.reason;
  badge.classList.toggle("refused", !review.allow);
  show(badge, true);
}

function appendToolOutput(callId, text) {
  const card = chat.toolCards.get(callId);
  if (!card || !text) {
    return;
  }
  const output = card.querySelector(".tool-body pre");
  if (!output) {
    return;
  }
  if (output.dataset.streaming !== "1") {
    // 第一次收到输出：清掉「运行中…」，展开卡片，不然流式输出来在折叠的面板里看不见；跑完再收起（见 setToolResult）
    output.textContent = "";
    output.dataset.streaming = "1";
    if (!card.classList.contains("expanded")) {
      card.classList.add("expanded");
      card.dataset.autoExpanded = "1";
    }
  }
  const stickToBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 24;
  output.append(document.createTextNode(text));
  if (stickToBottom) {
    output.scrollTop = output.scrollHeight;
  }
  if (isNearBottom()) {
    scrollToBottom();
  }
}

function setToolResult(callId, content, isError, denied, name) {
  // 折叠头上的数字按结果记：出错 / 被拒的不算进「读了几个、改了几个」
  const fold = chat.callFolds.get(callId);
  chat.callFolds.delete(callId);
  fold?.recordResult(callId, { name, content, isError, denied });
  const card = chat.toolCards.get(callId);
  if (!card) {
    return;
  }
  const output = card.querySelector(".tool-body pre");
  // 流式期间显示的是增量；最后换成服务端截断后的完整结果，不重复追加
  output.textContent = content || "（无输出）";
  delete output.dataset.streaming;
  let status = "ok";
  if (denied) {
    status = "denied";
  } else if (isError) {
    status = "error";
  }
  setToolStatus(card, status);
  // 只为看实时输出才自动展开的卡片，跑完默认收起；出错的留着展开，方便直接看到原因
  if (card.dataset.autoExpanded && status !== "error") {
    card.classList.remove("expanded");
  }
  delete card.dataset.autoExpanded;
}

// ---- 「任务计划」面板：todo_write 发的 todos_updated 事件驱动，一张卡片原地更新 ----
const TODO_STATUS_ICONS = { pending: "○", in_progress: "●", completed: "✓" };

function renderTodoPanel(todos) {
  if (!Array.isArray(todos) || todos.length === 0) {
    chat.todoPanel?.remove();
    chat.todoPanel = null;
    return;
  }
  if (!chat.todoPanel || !chat.todoPanel.isConnected) {
    const panel = el("div", "todo-panel");
    const header = el("div", "todo-header");
    header.append(icon("listCheck", "todo-icon"), el("span", "todo-title"), el("span", "todo-progress"));
    panel.append(header, el("div", "todo-items"));
    chat.todoPanel = panel;
    // 计划面板一直露在外面，不跟着工具调用收进折叠
    closeActivityFold();
    appendToTranscript(panel);
  }
  const done = todos.filter((item) => item.status === "completed").length;
  chat.todoPanel.querySelector(".todo-title").textContent = "任务计划";
  chat.todoPanel.querySelector(".todo-progress").textContent = `${done}/${todos.length}`;
  const items = chat.todoPanel.querySelector(".todo-items");
  items.replaceChildren();
  for (const item of todos) {
    const row = el("div", `todo-item ${item.status}`);
    const label = item.status === "in_progress" ? item.active_form || item.content : item.content;
    row.append(el("span", "todo-mark", TODO_STATUS_ICONS[item.status] || "○"), el("span", "todo-text", label));
    items.appendChild(row);
  }
  if (isNearBottom()) {
    scrollToBottom();
  }
}

function showApproval(request) {
  if (chat.approvalCards.has(request.request_id)) {
    return;
  }
  // 审批卡要露在外面，前面那一块折叠先收尾
  closeActivityFold();
  // 一批里好几项要问时合成一张卡：每项单独选，最后一起提交
  if (Array.isArray(request.items)) {
    showBatchApproval(request);
    return;
  }
  const card = el("div", "approval-card");
  const title = el("div", "approval-title");
  title.append(icon("shield"), "需要你确认：", el("code", "", request.tool_name));
  card.appendChild(title);
  const reviewNote = request.summary.match(/（AI 审批没有放行：(.*)）$/);
  if (reviewNote) {
    card.appendChild(el("div", "approval-note", `AI 审批没有放行：${reviewNote[1]}`));
  }
  // 工作目录外、沙箱外、密钥目录：多问这一句的原因
  for (const note of request.notes || []) {
    card.appendChild(el("div", "approval-note approval-warning", note));
  }
  card.appendChild(renderDiff(request.preview || request.summary || "", "approval-preview"));

  const actions = el("div", "approval-actions");
  const choices = [{ label: "允许", answer: "allow_once", className: "primary-button" }];
  // 碰到密钥目录时不给「总是允许」
  if (request.always_label) {
    choices.push({ label: request.always_label, answer: "allow_always", className: "ghost-button" });
  }
  // 访问工作目录外时可以连父目录一起放行，之后兄弟目录也不再问
  if (request.always_parents_label) {
    choices.push({ label: request.always_parents_label, answer: "allow_always_parents", className: "ghost-button" });
  }
  choices.push({ label: "拒绝", answer: "deny", className: "ghost-button danger-text" });
  for (const choice of choices) {
    const choiceButton = button(choice.className, choice.label);
    choiceButton.addEventListener("click", () => {
      sendToServer({ type: "approval", request_id: request.request_id, answer: choice.answer });
      actions.querySelectorAll("button").forEach((each) => (each.disabled = true));
    });
    actions.appendChild(choiceButton);
  }
  card.appendChild(actions);
  chat.approvalCards.set(request.request_id, card);
  appendToTranscript(card);
  scrollToBottom();
}

// ---- 合并的审批卡：一批调用里要问的几项放在一起，每项一个下拉框（允许 / 总是允许 / 拒绝） ----

function showBatchApproval(request) {
  const card = el("div", "approval-card approval-batch");
  const title = el("div", "approval-title");
  title.append(icon("shield"), `需要你确认 ${request.items.length} 项操作`);
  card.appendChild(title);
  const choosers = request.items.map((item) => {
    const row = el("div", "approval-item");
    const head = el("div", "approval-item-head");
    const chooser = approvalChooser(item);
    head.append(el("code", "", item.tool_name), el("span", "approval-item-summary", firstLine(item.summary)), chooser);
    row.appendChild(head);
    const reviewNote = item.summary.match(/（AI 审批没有放行：(.*)）$/);
    if (reviewNote) {
      row.appendChild(el("div", "approval-note", `AI 审批没有放行：${reviewNote[1]}`));
    }
    for (const note of item.notes || []) {
      row.appendChild(el("div", "approval-note approval-warning", note));
    }
    // 预览（diff）默认收起，几项摊开太长
    const preview = el("details", "approval-item-preview");
    preview.append(el("summary", "", "看预览"), renderDiff(item.preview || item.summary || "", "approval-preview"));
    row.appendChild(preview);
    card.appendChild(row);
    return chooser;
  });

  const actions = el("div", "approval-actions");
  const submit = (answers) => {
    sendToServer({ type: "approval", request_id: request.request_id, answers });
    actions.querySelectorAll("button").forEach((each) => (each.disabled = true));
    choosers.forEach((chooser) => (chooser.disabled = true));
  };
  const submitChosen = button("primary-button", "按上面的选择执行");
  submitChosen.addEventListener("click", () => submit(choosers.map((chooser) => chooser.value)));
  const allowAll = button("ghost-button", "全部允许");
  allowAll.addEventListener("click", () => submit(choosers.map(() => "allow_once")));
  const denyAll = button("ghost-button danger-text", "全部拒绝");
  denyAll.addEventListener("click", () => submit(choosers.map(() => "deny")));
  actions.append(submitChosen, allowAll, denyAll);
  card.appendChild(actions);
  chat.approvalCards.set(request.request_id, card);
  appendToTranscript(card);
  scrollToBottom();
}

function approvalChooser(item) {
  const chooser = el("select", "approval-choice");
  chooser.appendChild(new Option("允许", "allow_once"));
  // 碰到密钥目录时没有「总是允许」
  if (item.always_label) {
    chooser.appendChild(new Option(item.always_label, "allow_always"));
  }
  if (item.always_parents_label) {
    chooser.appendChild(new Option(item.always_parents_label, "allow_always_parents"));
  }
  chooser.appendChild(new Option("拒绝", "deny"));
  return chooser;
}

function firstLine(text) {
  return String(text || "").split("\n")[0];
}

function resolveApproval(requestId) {
  chat.approvalCards.get(requestId)?.remove();
  chat.approvalCards.delete(requestId);
}

function showQuestions(request) {
  if (chat.questionCards.has(request.request_id)) {
    return;
  }
  hideActivity();
  closeActivityFold();
  const card = renderQuestionCard(request, (answers) => {
    sendToServer({ type: "question_answer", request_id: request.request_id, answers });
  });
  chat.questionCards.set(request.request_id, card);
  appendToTranscript(card);
  scrollToBottom();
}

function resolveQuestions(requestId) {
  // 回答过的内容会出现在 ask_user 工具卡片的结果里，这张卡片收起来就行
  chat.questionCards.get(requestId)?.remove();
  chat.questionCards.delete(requestId);
  if (chat.running) {
    showActivity("等待模型响应");
  }
}

// ---- 计划卡：建议切计划模式 / 计划做好等批准 ----

function showPlanRequest(request) {
  if (chat.planCards.has(request.request_id)) {
    return;
  }
  hideActivity();
  closeActivityFold();
  const isSwitch = request.kind === "switch";
  const isDesign = request.mode === "design";
  const card = el("div", `plan-card ${isSwitch ? "plan-switch" : "plan-approval"}`);
  card.dataset.kind = request.kind;
  card.dataset.mode = request.mode || "plan";
  const title = el("div", "plan-title");
  title.append(
    icon(isSwitch ? "listCheck" : isDesign ? "compass" : "file"),
    isSwitch ? "这个任务适合先用计划模式" : isDesign ? "设计方案做好了" : "计划做好了"
  );
  card.appendChild(title);
  if (isSwitch) {
    card.appendChild(el("div", "plan-reason", request.reason || ""));
  } else {
    const body = el("div", "plan-body message-assistant");
    body.innerHTML = renderMarkdown(request.plan || "", { imageUrl: chatImageUrl });
    card.appendChild(body);
  }

  const actions = el("div", "plan-actions");
  const answer = (approve, feedback = "") => {
    sendToServer({ type: "plan_answer", request_id: request.request_id, approve, feedback });
    card.querySelectorAll("button, input").forEach((each) => (each.disabled = true));
  };
  if (isSwitch) {
    const usePlan = button("primary-button", "用计划模式", "listCheck");
    usePlan.addEventListener("click", () => answer(true));
    const skip = button("ghost-button", "直接开始");
    skip.addEventListener("click", () => answer(false));
    actions.append(usePlan, skip);
  } else {
    const feedback = el("input", "plan-feedback");
    feedback.placeholder = "要继续完善的话，写下你的意见（可空）";
    card.appendChild(feedback);
    const execute = button("primary-button", isDesign ? "按设计开始实现" : "按计划执行", isDesign ? "compass" : "play");
    execute.addEventListener("click", () => answer(true));
    const revise = button("ghost-button", "继续完善");
    revise.addEventListener("click", () => answer(false, feedback.value.trim()));
    const cancel = button("ghost-button danger-text", "先不做");
    cancel.addEventListener("click", () => answer(false));
    actions.append(execute, revise, cancel);
  }
  card.appendChild(actions);
  chat.planCards.set(request.request_id, card);
  appendToTranscript(card);
  scrollToBottom();
}

function resolvePlan(message) {
  const card = chat.planCards.get(message.request_id);
  if (!card) {
    return;
  }
  chat.planCards.delete(message.request_id);
  if (card.dataset.kind === "switch") {
    // 只是问要不要切，答复之后没必要留在对话里
    card.remove();
    if (chat.running) {
      showActivity("等待模型响应");
    }
    return;
  }
  // 计划本身留着，把按钮换成结果
  card.querySelector(".plan-actions")?.remove();
  card.querySelector(".plan-feedback")?.remove();
  const note = el("div", "plan-status");
  const isDesign = card.dataset.mode === "design";
  if (message.approved) {
    note.textContent = isDesign ? "已按这个设计方案开始实现" : "已按这个计划开始执行";
  } else if (message.feedback) {
    note.textContent = `继续完善：${message.feedback}`;
  } else {
    note.textContent = isDesign ? "先不实现这个设计方案" : "先不执行这份计划";
  }
  card.appendChild(note);
  if (chat.running) {
    showActivity("等待模型响应");
  }
}

// 刷新 / 切回任务时按会话上的记录恢复最近一份方案
function renderSavedPlan(plan, status, kind) {
  if (!plan) {
    return;
  }
  closeActivityFold();
  const isDesign = kind === "design";
  const card = el("div", "plan-card plan-approval saved");
  const title = el("div", "plan-title");
  const name = isDesign ? "设计" : "计划";
  title.append(icon(isDesign ? "compass" : "file"), `${name}（${status === "approved" ? "已批准" : "未批准"}）`);
  card.appendChild(title);
  const body = el("div", "plan-body message-assistant");
  body.innerHTML = renderMarkdown(plan, { imageUrl: chatImageUrl });
  card.appendChild(body);
  appendToTranscript(card);
}

function renderHistoryItem(item) {
  if (item.kind === "user") {
    addUserMessage(item.text, item.attachments, "", item.message_id);
  } else if (item.kind === "assistant") {
    addAssistantMessage(item.text, item.finish);
  } else if (item.kind === "summary") {
    addSummaryCard(item.text);
  } else if (item.kind === "notice") {
    addNotice(item.text, "warning");
  } else if (item.kind === "tool") {
    addToolCard(item.call_id, item.name, item.summary);
    setToolReview(item.call_id, item.review);
    if (item.result !== null) {
      setToolResult(item.call_id, item.result, item.is_error, item.denied, item.name);
    }
  }
}

function renderSnapshot(snapshot) {
  clearTranscript();
  // 先记下状态再画历史：回复里的本地图片要靠 status.workspace 换成取图地址，切任务 / 刷新时它是空的
  applyStatus(snapshot);
  snapshot.items.forEach(renderHistoryItem);
  if (snapshot.items.length === 0) {
    elements.transcriptInner.appendChild(el("div", "empty-chat", "说说要做什么，agent 会在这个工作区里读代码、改文件、跑命令。"));
  }
  snapshot.pending_approvals.forEach(showApproval);
  (snapshot.pending_questions || []).forEach(showQuestions);
  (snapshot.pending_plans || []).forEach(showPlanRequest);
  // 有等批准的计划卡时不再画一份静态的，免得同一份计划出现两次
  const waitingForPlan = (snapshot.pending_plans || []).some((pending) => pending.kind === "approval");
  if (!waitingForPlan) {
    renderSavedPlan(snapshot.plan, snapshot.plan_status, snapshot.plan_kind);
  }
  // 刷新 / 切回来时从会话上恢复计划面板（面板出现在历史后面，反映当前进度）
  renderTodoPanel(snapshot.todos);
  closeActivityFold();
  if (
    snapshot.running &&
    snapshot.pending_approvals.length === 0 &&
    !(snapshot.pending_questions || []).length &&
    !(snapshot.pending_plans || []).length
  ) {
    // 中途连上来的：前面流式的那段没了，给个提示，后面的照常显示
    showActivity("运行中");
  }
  scrollToBottom();
}

// ===================== 标题菜单 =====================

function startRename() {
  elements.menu.classList.add("hidden");
  const input = el("input", "title-input");
  input.value = elements.title.textContent;
  elements.title.replaceChildren(input);
  input.focus();
  input.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) {
      return;
    }
    finished = true;
    const title = input.value.trim();
    elements.title.textContent = chat.status?.title || "";
    if (save && title) {
      await sendJson(`api/sessions/${encodeURIComponent(state.currentTaskId)}`, "PATCH", { title });
      await reloadTasks();
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

async function deleteTask(onDeleted) {
  elements.menu.classList.add("hidden");
  if (!window.confirm("删除这个任务？对话记录、附件和变更备份都会删掉，工作区里的文件不动。")) {
    return;
  }
  const taskId = state.currentTaskId;
  try {
    await requestJson(`api/sessions/${encodeURIComponent(taskId)}`, { method: "DELETE" });
  } catch (error) {
    addNotice(`删除失败：${error.message}`, "error");
    return;
  }
  leaveTask();
  await reloadTasks();
  onDeleted();
}

// 从命令输出里找本机服务的地址（dev server、API 文档页），给右侧栏的预览用。
// 只认带协议或带端口的，避免把普通提到 localhost 的句子也当成链接。
const LOCAL_LINK_RE = /(?:https?:\/\/)?((?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1)):(\d{2,5})(?:\/[^\s"'`<>)\],;]*)?|(?:https?:\/\/)(localhost)(?:\/[^\s"'`<>)\],;]*)?/gi;

function extractLocalLinks(text) {
  const links = [];
  for (const match of text.matchAll(LOCAL_LINK_RE)) {
    const host = match[1] || match[3];
    const port = match[2] || "";
    const rest = (match[0].slice(match[0].indexOf(host) + host.length + (port ? port.length + 1 : 0)) || "").replace(/[./]+$/, "");
    links.push(`http://localhost${port ? `:${port}` : ""}${rest}`);
  }
  return [...new Set(links)];
}
