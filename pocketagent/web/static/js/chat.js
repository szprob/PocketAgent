// 任务页：中间的对话。一个任务一条 WebSocket，协议见 pocketagent/web/server.py 开头。

import { attachmentUrl, requestJson, sendJson, socketUrl, uploadFiles } from "./api.js";
import { Composer } from "./composer.js";
import { button, el, icon, renderDiff, show } from "./dom.js";
import { renderMarkdown } from "./markdown.js";
import { renderQuestionCard } from "./questions.js";
import { emit, reloadTasks, state } from "./state.js";

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
  toolCards: new Map(),
  approvalCards: new Map(),
  questionCards: new Map(),
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
  if (chat.status) {
    applyStatus(chat.status);
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

async function sendUserMessage({ text, files }) {
  if (!chat.socket || chat.socket.readyState !== WebSocket.OPEN) {
    addNotice("还没连上服务，稍等再试", "warning");
    return false;
  }
  let attachments = [];
  try {
    attachments = await uploadFiles(state.currentTaskId, files);
  } catch (error) {
    addNotice(`附件上传失败：${error.message}`, "error");
    return false;
  }
  return sendToServer({ type: "user_message", text, attachment_ids: attachments.map((item) => item.id) });
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
      break;
    case "approval_resolved":
      resolveApproval(message.request_id);
      break;
    case "question_request":
      showQuestions(message);
      break;
    case "question_resolved":
      resolveQuestions(message.request_id);
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
  chat.composer.setRunning(status.running, state.info?.busy_input_mode);
  chat.composer.setContext(status.context_tokens, status.context_window, status.compact_threshold);
  chat.composer.setCompacting(status.compacting);
  chat.composer.setWorkspaceLabel(status.workspace);
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
      addUserMessage(event.user_text, event.attachments);
      showActivity("等待模型响应");
      break;
    case "interjection_applied":
      finishStreamingBlocks();
      addUserMessage(event.user_text, event.attachments, "插话");
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
    case "tool_review":
      setToolReview(event.call_id, { allow: event.allow, reason: event.reason });
      break;
    case "tool_result":
      setToolResult(event.call_id, event.content, event.is_error, event.denied);
      if (chat.running && !event.denied) {
        showActivity("等待模型响应");
      }
      if (FILE_TOOLS.has(event.name)) {
        emit("files-changed", state.currentTaskId);
      }
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
      hideActivity();
      const notice = TURN_END_NOTICES[event.stop_reason];
      if (notice) {
        addNotice(notice[0], notice[1]);
      }
      emit("turn-ended", { taskId: state.currentTaskId, finalText: event.final_text });
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

function clearTranscript() {
  hideActivity();
  if (chat.currentReasoning) {
    clearInterval(chat.currentReasoning.timer);
  }
  elements.transcriptInner.replaceChildren();
  chat.currentAssistant = null;
  chat.currentReasoning = null;
  chat.toolCards.clear();
  chat.approvalCards.clear();
  chat.questionCards.clear();
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

function addUserMessage(text, attachments, badge) {
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
  appendToTranscript(message);
}

function addAssistantMessage(text) {
  const element = el("div", "message-assistant");
  element.innerHTML = renderMarkdown(text);
  appendToTranscript(element);
}

function addSummaryCard(text) {
  const details = el("details", "summary-card");
  details.appendChild(el("summary", "", "之前的对话已压缩，点开看摘要"));
  const body = el("div", "message-assistant");
  body.innerHTML = renderMarkdown(text);
  details.appendChild(body);
  appendToTranscript(details);
}

function addNotice(text, kind) {
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
  block.element.innerHTML = renderMarkdown(block.shown);
  if (shouldScroll) {
    scrollToBottom();
  }
}

function flushAssistantBlock(block) {
  block.shown += block.pending;
  block.pending = "";
  renderStreamingBlock(block);
  block.element.classList.remove("streaming");
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
    appendToTranscript(details);
    const block = { details, label, preview, bodyText, length: 0, tail: "", startedAt: performance.now() };
    // 计时和最新一句每 0.1 秒刷新一次，思考很长时也不卡
    block.timer = setInterval(() => updateReasoningLabel(block), 100);
    chat.currentReasoning = block;
  }
  const block = chat.currentReasoning;
  block.bodyText.appendData(delta);
  block.length += delta.length;
  block.tail = (block.tail + delta).slice(-REASONING_PREVIEW_CHARS * 2);
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
  header.addEventListener("click", () => card.classList.toggle("expanded"));
  const body = el("div", "tool-body");
  body.appendChild(el("pre", "", "运行中…"));
  card.append(header, body);
  setToolStatus(card, "running");
  chat.toolCards.set(callId, card);
  appendToTranscript(card);
  return card;
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

function setToolResult(callId, content, isError, denied) {
  const card = chat.toolCards.get(callId);
  if (!card) {
    return;
  }
  card.querySelector(".tool-body pre").textContent = content || "（无输出）";
  let status = "ok";
  if (denied) {
    status = "denied";
  } else if (isError) {
    status = "error";
  }
  setToolStatus(card, status);
}

function showApproval(request) {
  if (chat.approvalCards.has(request.request_id)) {
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

function resolveApproval(requestId) {
  chat.approvalCards.get(requestId)?.remove();
  chat.approvalCards.delete(requestId);
}

function showQuestions(request) {
  if (chat.questionCards.has(request.request_id)) {
    return;
  }
  hideActivity();
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

function renderHistoryItem(item) {
  if (item.kind === "user") {
    addUserMessage(item.text, item.attachments);
  } else if (item.kind === "assistant") {
    addAssistantMessage(item.text);
  } else if (item.kind === "summary") {
    addSummaryCard(item.text);
  } else if (item.kind === "notice") {
    addNotice(item.text, "warning");
  } else if (item.kind === "tool") {
    addToolCard(item.call_id, item.name, item.summary);
    setToolReview(item.call_id, item.review);
    if (item.result !== null) {
      setToolResult(item.call_id, item.result, item.is_error, item.denied);
    }
  }
}

function renderSnapshot(snapshot) {
  clearTranscript();
  snapshot.items.forEach(renderHistoryItem);
  if (snapshot.items.length === 0) {
    elements.transcriptInner.appendChild(el("div", "empty-chat", "说说要做什么，agent 会在这个工作区里读代码、改文件、跑命令。"));
  }
  snapshot.pending_approvals.forEach(showApproval);
  (snapshot.pending_questions || []).forEach(showQuestions);
  applyStatus(snapshot);
  if (snapshot.running && snapshot.pending_approvals.length === 0 && !(snapshot.pending_questions || []).length) {
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
