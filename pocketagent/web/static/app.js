"use strict";

// 协议见 pocketagent/web/server.py 顶部的说明。所有地址都用相对路径，挂在网关子路径下也能用。

const elements = {
  app: document.getElementById("app"),
  sessionList: document.getElementById("session-list"),
  newSessionButton: document.getElementById("new-session"),
  toggleSidebarButton: document.getElementById("toggle-sidebar"),
  sidebarBackdrop: document.getElementById("sidebar-backdrop"),
  workspace: document.getElementById("workspace"),
  modelSelect: document.getElementById("model-select"),
  modeSelect: document.getElementById("mode-select"),
  compactButton: document.getElementById("compact-button"),
  settingsButton: document.getElementById("settings-button"),
  settingsModal: document.getElementById("settings-modal"),
  settingsClose: document.getElementById("settings-close"),
  settingsCancel: document.getElementById("settings-cancel"),
  settingsSave: document.getElementById("settings-save"),
  settingsMessage: document.getElementById("settings-message"),
  modelList: document.getElementById("model-list"),
  addModelButton: document.getElementById("add-model"),
  modelForm: document.getElementById("model-form"),
  modelFormEmpty: document.getElementById("model-form-empty"),
  modelFormFields: document.getElementById("model-form-fields"),
  apiKeyHint: document.getElementById("api-key-hint"),
  testModelButton: document.getElementById("test-model"),
  makeDefaultButton: document.getElementById("make-default"),
  deleteModelButton: document.getElementById("delete-model"),
  testResult: document.getElementById("test-result"),
  defaultModeSelect: document.getElementById("default-mode-select"),
  maxStepsInput: document.getElementById("max-steps-input"),
  configPath: document.getElementById("config-path"),
  transcript: document.getElementById("transcript"),
  transcriptInner: document.getElementById("transcript-inner"),
  emptyHint: document.getElementById("empty-hint"),
  connectionBanner: document.getElementById("connection-banner"),
  input: document.getElementById("input"),
  sendButton: document.getElementById("send-button"),
  statusText: document.getElementById("status-text"),
};

const MODE_LABELS = {
  read_only: "只读",
  ask: "每次询问",
  auto_edit: "自动改文件",
  full_auto: "全自动",
};

const state = {
  info: null,
  sessionId: null,
  socket: null,
  reconnectDelay: 1000,
  running: false,
  // 正在流式输出的 assistant 文本块和思考块
  currentAssistant: null,
  currentReasoning: null,
  toolCards: new Map(),
  approvalCards: new Map(),
};

// ===================== Markdown（够用的子集，先整体转义再加标签，不会注入 HTML） =====================

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInlineText(escapedText) {
  return escapedText
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderInline(text) {
  // 按反引号切开：奇数段是行内代码，不做加粗、链接处理；最后一个反引号没配对时当普通文字
  const segments = escapeHtml(text).split("`");
  const parts = [];
  segments.forEach((segment, index) => {
    const isOddSegment = index % 2 === 1;
    const hasClosingBacktick = index < segments.length - 1;
    if (isOddSegment && hasClosingBacktick) {
      parts.push(`<code>${segment}</code>`);
    } else if (isOddSegment) {
      parts.push(renderInlineText("`" + segment));
    } else {
      parts.push(renderInlineText(segment));
    }
  });
  return parts.join("");
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line);
}

function splitTableRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isBlockStart(line) {
  return /^```/.test(line) || /^#{1,6}\s/.test(line) || /^\s*([-*]|\d+\.)\s+/.test(line) || /^>\s?/.test(line);
}

function renderMarkdown(source) {
  const lines = source.split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];

    if (/^```/.test(line)) {
      const codeLines = [];
      index += 1;
      // 流式输出时代码块可能还没收尾，读到末尾就算结束
      while (index < lines.length && !/^```/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const pattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
      const items = [];
      while (index < lines.length && pattern.test(lines[index])) {
        items.push(`<li>${renderInline(lines[index].replace(pattern, ""))}</li>`);
        index += 1;
      }
      const tag = ordered ? "ol" : "ul";
      html.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quoteLines.push(renderInline(lines[index].replace(/^>\s?/, "")));
        index += 1;
      }
      html.push(`<blockquote>${quoteLines.join("<br>")}</blockquote>`);
      continue;
    }

    if (line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const headerCells = splitTableRow(line).map((cell) => `<th>${renderInline(cell)}</th>`);
      const bodyRows = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
        const cells = splitTableRow(lines[index]).map((cell) => `<td>${renderInline(cell)}</td>`);
        bodyRows.push(`<tr>${cells.join("")}</tr>`);
        index += 1;
      }
      html.push(`<table><thead><tr>${headerCells.join("")}</tr></thead><tbody>${bodyRows.join("")}</tbody></table>`);
      continue;
    }

    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      html.push("<hr>");
      index += 1;
      continue;
    }

    if (line.trim() === "") {
      index += 1;
      continue;
    }

    const paragraphLines = [];
    while (index < lines.length && lines[index].trim() !== "" && !isBlockStart(lines[index])) {
      paragraphLines.push(renderInline(lines[index]));
      index += 1;
    }
    html.push(`<p>${paragraphLines.join("<br>")}</p>`);
  }
  return html.join("");
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
  elements.emptyHint.classList.add("hidden");
  elements.transcriptInner.appendChild(element);
  if (shouldScroll) {
    scrollToBottom();
  }
  return element;
}

function createElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
}

function clearTranscript() {
  elements.transcriptInner.replaceChildren(elements.emptyHint);
  elements.emptyHint.classList.remove("hidden");
  state.currentAssistant = null;
  state.currentReasoning = null;
  state.toolCards.clear();
  state.approvalCards.clear();
}

function addUserMessage(text) {
  appendToTranscript(createElement("div", "message-user", text));
}

function addAssistantMessage(text) {
  const element = createElement("div", "message-assistant");
  element.innerHTML = renderMarkdown(text);
  appendToTranscript(element);
}

function addSummaryCard(text) {
  const details = createElement("details", "summary-card");
  details.appendChild(createElement("summary", "", "之前的对话已压缩，点开看摘要"));
  const body = createElement("div", "message-assistant");
  body.innerHTML = renderMarkdown(text);
  details.appendChild(body);
  appendToTranscript(details);
}

function addNotice(text, kind) {
  appendToTranscript(createElement("div", `notice ${kind || ""}`, text));
}

function appendAssistantText(delta) {
  if (!state.currentAssistant) {
    const element = appendToTranscript(createElement("div", "message-assistant"));
    state.currentAssistant = { element, text: "", renderScheduled: false };
  }
  const block = state.currentAssistant;
  block.text += delta;
  // 每帧最多重新渲染一次，长回复流式输出时不卡
  if (!block.renderScheduled) {
    block.renderScheduled = true;
    requestAnimationFrame(() => {
      const shouldScroll = isNearBottom();
      block.element.innerHTML = renderMarkdown(block.text);
      block.renderScheduled = false;
      if (shouldScroll) {
        scrollToBottom();
      }
    });
  }
}

function appendReasoning(delta) {
  if (!state.currentReasoning) {
    const details = createElement("details", "reasoning");
    const summary = createElement("summary", "", "思考中…");
    const body = createElement("div", "reasoning-body");
    details.append(summary, body);
    appendToTranscript(details);
    state.currentReasoning = { summary, body, length: 0 };
  }
  const block = state.currentReasoning;
  block.body.textContent += delta;
  block.length += delta.length;
}

function finishStreamingBlocks() {
  if (state.currentReasoning) {
    state.currentReasoning.summary.textContent = `思考过程（${state.currentReasoning.length} 字）`;
  }
  state.currentAssistant = null;
  state.currentReasoning = null;
}

function discardPartialAssistant() {
  // 模型调用失败要重试时，丢掉已经流出来的半截回复
  if (state.currentAssistant) {
    state.currentAssistant.element.remove();
  }
  if (state.currentReasoning) {
    state.currentReasoning.body.parentElement.remove();
  }
  state.currentAssistant = null;
  state.currentReasoning = null;
}

const TOOL_STATUS_ICONS = { running: "●", ok: "✓", error: "✗", denied: "⊘" };

function setToolStatus(card, status) {
  card.classList.remove("status-running", "status-ok", "status-error", "status-denied");
  card.classList.add(`status-${status}`);
  card.querySelector(".tool-status").textContent = TOOL_STATUS_ICONS[status];
}

function addToolCard(callId, name, summary) {
  const card = createElement("div", "tool-card");
  const header = createElement("button", "tool-header");
  header.type = "button";
  header.append(
    createElement("span", "tool-status"),
    createElement("span", "tool-name", name),
    createElement("span", "tool-summary", summary),
    createElement("span", "tool-chevron", "›"),
  );
  header.addEventListener("click", () => card.classList.toggle("expanded"));
  const body = createElement("div", "tool-body");
  body.appendChild(createElement("pre", "", "运行中…"));
  card.append(header, body);
  setToolStatus(card, "running");
  state.toolCards.set(callId, card);
  appendToTranscript(card);
  return card;
}

function setToolResult(callId, content, isError, denied) {
  const card = state.toolCards.get(callId);
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

function renderDiff(preview) {
  const pre = createElement("pre", "approval-preview");
  for (const line of preview.split("\n")) {
    let className = "diff-line";
    if (line.startsWith("+") && !line.startsWith("+++")) {
      className += " diff-add";
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      className += " diff-remove";
    } else if (line.startsWith("@@")) {
      className += " diff-hunk";
    }
    pre.appendChild(createElement("span", className, line || " "));
  }
  return pre;
}

function showApproval(request) {
  if (state.approvalCards.has(request.request_id)) {
    return;
  }
  const card = createElement("div", "approval-card");
  const title = createElement("div", "approval-title");
  title.append("需要确认：", createElement("code", "", request.tool_name));
  card.appendChild(title);
  card.appendChild(renderDiff(request.preview || request.summary || ""));

  const actions = createElement("div", "approval-actions");
  const buttons = [
    { label: "允许", answer: "allow_once", className: "allow" },
    { label: `本会话总是允许 ${request.tool_name}`, answer: "allow_always", className: "" },
    { label: "拒绝", answer: "deny", className: "deny" },
  ];
  for (const definition of buttons) {
    const button = createElement("button", definition.className, definition.label);
    button.type = "button";
    button.addEventListener("click", () => {
      sendToServer({ type: "approval", request_id: request.request_id, answer: definition.answer });
      actions.querySelectorAll("button").forEach((each) => (each.disabled = true));
    });
    actions.appendChild(button);
  }
  card.appendChild(actions);
  state.approvalCards.set(request.request_id, card);
  appendToTranscript(card);
  scrollToBottom();
}

function resolveApproval(requestId) {
  const card = state.approvalCards.get(requestId);
  if (card) {
    card.remove();
    state.approvalCards.delete(requestId);
  }
}

function renderHistoryItem(item) {
  if (item.kind === "user") {
    addUserMessage(item.text);
  } else if (item.kind === "assistant") {
    addAssistantMessage(item.text);
  } else if (item.kind === "summary") {
    addSummaryCard(item.text);
  } else if (item.kind === "tool") {
    addToolCard(item.call_id, item.name, item.summary);
    if (item.result !== null) {
      setToolResult(item.call_id, item.result, false, false);
    }
  }
}

function renderSnapshot(snapshot) {
  clearTranscript();
  snapshot.items.forEach(renderHistoryItem);
  snapshot.pending_approvals.forEach(showApproval);
  setRunning(snapshot.running);
  setMode(snapshot.mode);
  setModel(snapshot.model_id);
  scrollToBottom();
}

// ===================== agent 事件 =====================

const TURN_END_NOTICES = {
  interrupted: ["已中断，可以接着说", "warning"],
  denied: ["调用被拒绝，这一轮先停下。告诉 agent 接下来怎么做。", "warning"],
};

function handleAgentEvent(event) {
  switch (event.type) {
    case "turn_start":
      finishStreamingBlocks();
      addUserMessage(event.user_text);
      break;
    case "text_delta":
      appendAssistantText(event.text);
      break;
    case "reasoning_delta":
      appendReasoning(event.text);
      break;
    case "llm_response":
      finishStreamingBlocks();
      break;
    case "llm_retry":
      discardPartialAssistant();
      addNotice(`模型调用失败，第 ${event.attempt} 次尝试：${event.error}`, "warning");
      break;
    case "tool_call":
      finishStreamingBlocks();
      addToolCard(event.call_id, event.name, event.summary);
      break;
    case "tool_result":
      setToolResult(event.call_id, event.content, event.is_error, event.denied);
      break;
    case "compaction":
      addNotice(`上下文已压缩：约 ${event.tokens_before} → ${event.tokens_after} tokens`);
      break;
    case "step_limit":
      addNotice(`这一轮已调用模型 ${event.max_steps} 次，达到上限，先停下`, "warning");
      break;
    case "error":
      addNotice(event.message, "error");
      break;
    case "turn_end": {
      finishStreamingBlocks();
      const notice = TURN_END_NOTICES[event.stop_reason];
      if (notice) {
        addNotice(notice[0], notice[1]);
      }
      break;
    }
    default:
      break;
  }
}

function handleServerMessage(message) {
  switch (message.type) {
    case "snapshot":
      renderSnapshot(message);
      break;
    case "state": {
      const wasRunning = state.running;
      setRunning(message.running);
      setMode(message.mode);
      setModel(message.model_id);
      if (wasRunning && !message.running) {
        // 一轮结束后会话已存盘，刷新侧栏（新会话第一次出现在列表里）
        loadSessions();
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
    case "error":
      addNotice(message.message, "error");
      break;
    default:
      break;
  }
}

// ===================== 状态和控件 =====================

function setRunning(running) {
  state.running = running;
  elements.sendButton.textContent = running ? "停止" : "发送";
  elements.sendButton.classList.toggle("stop", running);
  elements.compactButton.disabled = running;
  elements.statusText.textContent = running ? "运行中… 按 Esc 或点停止可以中断" : "空闲";
}

function setMode(mode) {
  elements.modeSelect.value = mode;
}

function setModel(modelId) {
  if (modelId) {
    elements.modelSelect.value = modelId;
  }
}

function fillModeOptions(select, modes) {
  select.replaceChildren();
  for (const mode of modes) {
    const option = createElement("option", "", MODE_LABELS[mode] || mode);
    option.value = mode;
    select.appendChild(option);
  }
}

function populateModelSelect(info) {
  const select = elements.modelSelect;
  const currentValue = select.value;
  select.replaceChildren();
  if (info.models.length === 0) {
    // 一个模型都没配：显示从环境变量读到的模型，引导去设置里加
    const option = createElement("option", "", `${info.fallback_model}（环境变量）`);
    option.value = "";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const model of info.models) {
    const option = createElement("option", "", model.name);
    option.value = model.id;
    option.title = model.model;
    select.appendChild(option);
  }
  if (info.models.some((model) => model.id === currentValue)) {
    select.value = currentValue;
  }
}

async function reloadInfo() {
  state.info = await requestJson("api/info");
  elements.workspace.textContent = state.info.workspace;
  elements.workspace.title = state.info.workspace;
  populateModelSelect(state.info);
  renderEmptyHint(state.info);
}

function resizeInput() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 240)}px`;
}

function submitInput() {
  const text = elements.input.value.trim();
  if (!text || state.running) {
    return;
  }
  if (sendToServer({ type: "user_message", text })) {
    elements.input.value = "";
    resizeInput();
  }
}

function isMobileLayout() {
  return window.matchMedia("(max-width: 800px)").matches;
}

function toggleSidebar() {
  if (isMobileLayout()) {
    elements.app.classList.toggle("sidebar-open");
  } else {
    elements.app.classList.toggle("sidebar-hidden");
  }
}

function closeMobileSidebar() {
  elements.app.classList.remove("sidebar-open");
}

// ===================== 连接 =====================

function socketUrl(sessionId) {
  const url = new URL(`ws/${encodeURIComponent(sessionId)}`, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.hash = "";
  return url.href;
}

function sendToServer(message) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    addNotice("还没连上服务，稍等再试", "warning");
    return false;
  }
  state.socket.send(JSON.stringify(message));
  return true;
}

function connect(sessionId) {
  if (state.socket) {
    const previous = state.socket;
    state.socket = null;
    previous.close();
  }
  state.sessionId = sessionId;
  history.replaceState(null, "", `#${encodeURIComponent(sessionId)}`);
  highlightActiveSession();

  const socket = new WebSocket(socketUrl(sessionId));
  state.socket = socket;
  socket.addEventListener("open", () => {
    elements.connectionBanner.classList.add("hidden");
    state.reconnectDelay = 1000;
  });
  socket.addEventListener("message", (event) => handleServerMessage(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    if (state.socket !== socket) {
      // 是我们自己切换会话关掉的
      return;
    }
    elements.connectionBanner.classList.remove("hidden");
    setTimeout(() => {
      if (state.socket === socket) {
        connect(sessionId);
      }
    }, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 10000);
  });
}

// ===================== REST =====================

async function requestJson(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      message = JSON.parse(text).error || text;
    } catch (notJson) {
      // 不是 JSON 就原样显示
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function sendJson(path, method, body) {
  return requestJson(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function formatTime(isoText) {
  return isoText ? isoText.replace("T", " ").slice(5, 16) : "";
}

function highlightActiveSession() {
  for (const item of elements.sessionList.querySelectorAll(".session-item")) {
    item.classList.toggle("active", item.dataset.sessionId === state.sessionId);
  }
}

async function loadSessions() {
  const { sessions } = await requestJson("api/sessions");
  elements.sessionList.replaceChildren();
  if (sessions.length === 0) {
    elements.sessionList.appendChild(createElement("div", "session-empty", "还没有会话"));
    return;
  }
  for (const session of sessions) {
    const item = createElement("button", "session-item");
    item.type = "button";
    item.dataset.sessionId = session.id;
    item.append(
      createElement("span", "session-title", session.title),
      createElement("span", "session-time", formatTime(session.updated_at)),
    );
    item.addEventListener("click", () => {
      closeMobileSidebar();
      if (session.id !== state.sessionId) {
        connect(session.id);
      }
    });
    elements.sessionList.appendChild(item);
  }
  highlightActiveSession();
}

async function startNewSession() {
  const { id } = await requestJson("api/sessions", { method: "POST" });
  clearTranscript();
  setRunning(false);
  connect(id);
  closeMobileSidebar();
  elements.input.focus();
}

async function openSessionFromHashOrNew() {
  const sessionId = decodeURIComponent(window.location.hash.slice(1));
  if (sessionId) {
    try {
      await requestJson(`api/sessions/${encodeURIComponent(sessionId)}`);
      connect(sessionId);
      return;
    } catch (error) {
      if (error.status !== 404) {
        throw error;
      }
    }
  }
  await startNewSession();
}

function renderEmptyHint(info) {
  if (info.models.length === 0) {
    elements.emptyHint.replaceChildren("还没配置模型：点右上角 ⚙ 设置，添加模型名、接口地址和 API Key。");
    return;
  }
  elements.emptyHint.replaceChildren(
    "在 ",
    createElement("strong", "", info.workspace),
    " 里工作。试试：「看看这个仓库是做什么的」",
  );
}

// ===================== 设置弹窗 =====================

// 设置面板里正在编辑、还没保存的配置
const settingsDraft = {
  models: [],
  defaultModel: null,
  selectedId: null,
};

const MODEL_FORM_FIELDS = [
  "name",
  "model",
  "base_url",
  "api_key",
  "reasoning_effort",
  "context_window_tokens",
  "temperature",
  "extra_body",
];

const DEFAULT_CONTEXT_WINDOW = 128000;

function isSettingsOpen() {
  return !elements.settingsModal.classList.contains("hidden");
}

function formatJsonObject(value) {
  return value && Object.keys(value).length > 0 ? JSON.stringify(value) : "";
}

function textOrEmpty(value) {
  return value === null || value === undefined ? "" : String(value);
}

function toDraftModel(model) {
  return {
    id: model.id,
    name: textOrEmpty(model.name),
    model: textOrEmpty(model.model),
    base_url: textOrEmpty(model.base_url),
    api_key: "",
    api_key_hint: model.api_key_hint || "",
    clear_api_key: false,
    reasoning_effort: textOrEmpty(model.reasoning_effort),
    context_window_tokens: textOrEmpty(model.context_window_tokens),
    temperature: textOrEmpty(model.temperature),
    extra_body: formatJsonObject(model.extra_body),
  };
}

function toPayloadModel(model) {
  const payload = { id: model.id, clear_api_key: model.clear_api_key };
  for (const field of MODEL_FORM_FIELDS) {
    payload[field] = model[field];
  }
  return payload;
}

function selectedDraftModel() {
  return settingsDraft.models.find((model) => model.id === settingsDraft.selectedId) || null;
}

function setSettingsMessage(text, isOk) {
  elements.settingsMessage.textContent = text;
  elements.settingsMessage.classList.toggle("ok", Boolean(isOk));
}

function setTestResult(text, kind) {
  elements.testResult.textContent = text;
  elements.testResult.className = `test-result ${kind || ""}`;
}

async function openSettings() {
  const config = await requestJson("api/config");
  settingsDraft.models = config.models.map(toDraftModel);
  settingsDraft.defaultModel = config.default_model;
  settingsDraft.selectedId = elements.modelSelect.value || config.default_model || null;
  if (!selectedDraftModel() && settingsDraft.models.length > 0) {
    settingsDraft.selectedId = settingsDraft.models[0].id;
  }
  fillModeOptions(elements.defaultModeSelect, state.info.modes);
  elements.defaultModeSelect.value = config.approval_mode;
  elements.maxStepsInput.value = config.max_steps;
  elements.configPath.textContent = `保存在 ${config.config_path}（文件权限 600，只有你自己能读）`;
  setSettingsMessage("");
  renderModelList();
  renderModelForm();
  elements.settingsModal.classList.remove("hidden");
  if (settingsDraft.models.length === 0) {
    addModel();
  }
}

function closeSettings() {
  elements.settingsModal.classList.add("hidden");
}

function renderModelList() {
  elements.modelList.replaceChildren();
  for (const model of settingsDraft.models) {
    const item = createElement("button", "model-item");
    item.type = "button";
    item.classList.toggle("active", model.id === settingsDraft.selectedId);
    const nameLine = createElement("span", "model-item-name", model.name || model.model || "（未命名）");
    if (model.id === settingsDraft.defaultModel) {
      nameLine.appendChild(createElement("span", "default-badge", "默认"));
    }
    item.append(nameLine, createElement("span", "model-item-model", model.model || "未填模型名"));
    item.addEventListener("click", () => {
      settingsDraft.selectedId = model.id;
      renderModelList();
      renderModelForm();
    });
    elements.modelList.appendChild(item);
  }
}

function renderApiKeyHint(model) {
  const hint = elements.apiKeyHint;
  hint.replaceChildren();
  if (model.api_key) {
    hint.append("保存后使用新填的密钥");
    return;
  }
  if (model.clear_api_key) {
    hint.append("保存后会清除已保存的密钥");
    return;
  }
  if (!model.api_key_hint) {
    hint.append("未设置");
    return;
  }
  hint.append(`已保存 ${model.api_key_hint}，留空表示不改`);
  const clearButton = createElement("button", "link-button", "清除");
  clearButton.type = "button";
  clearButton.addEventListener("click", () => {
    model.clear_api_key = true;
    model.api_key = "";
    renderModelForm();
  });
  hint.appendChild(clearButton);
}

function renderModelForm() {
  const model = selectedDraftModel();
  elements.modelFormEmpty.classList.toggle("hidden", model !== null);
  elements.modelFormFields.classList.toggle("hidden", model === null);
  setTestResult("");
  if (!model) {
    return;
  }
  for (const field of MODEL_FORM_FIELDS) {
    elements.modelForm.elements.namedItem(field).value = model[field];
  }
  renderApiKeyHint(model);
  elements.makeDefaultButton.disabled = model.id === settingsDraft.defaultModel;
}

function onModelFieldInput(event) {
  const model = selectedDraftModel();
  if (!model || !MODEL_FORM_FIELDS.includes(event.target.name)) {
    return;
  }
  model[event.target.name] = event.target.value;
  if (event.target.name === "api_key") {
    if (event.target.value) {
      model.clear_api_key = false;
    }
    renderApiKeyHint(model);
  }
  if (event.target.name === "name" || event.target.name === "model") {
    renderModelList();
  }
}

function randomModelId() {
  return Math.random().toString(16).slice(2, 10).padEnd(8, "0");
}

function addModel() {
  const model = toDraftModel({ id: randomModelId(), context_window_tokens: DEFAULT_CONTEXT_WINDOW });
  settingsDraft.models.push(model);
  settingsDraft.selectedId = model.id;
  if (!settingsDraft.defaultModel) {
    settingsDraft.defaultModel = model.id;
  }
  renderModelList();
  renderModelForm();
  elements.modelForm.elements.namedItem("name").focus();
}

function deleteSelectedModel() {
  const model = selectedDraftModel();
  if (!model || !window.confirm(`删除模型「${model.name || model.model}」？保存后生效。`)) {
    return;
  }
  settingsDraft.models = settingsDraft.models.filter((each) => each.id !== model.id);
  const firstId = settingsDraft.models.length > 0 ? settingsDraft.models[0].id : null;
  if (settingsDraft.defaultModel === model.id) {
    settingsDraft.defaultModel = firstId;
  }
  settingsDraft.selectedId = firstId;
  renderModelList();
  renderModelForm();
}

function makeSelectedDefault() {
  settingsDraft.defaultModel = settingsDraft.selectedId;
  renderModelList();
  renderModelForm();
}

async function testSelectedModel() {
  const model = selectedDraftModel();
  if (!model) {
    return;
  }
  setTestResult("测试中…");
  elements.testModelButton.disabled = true;
  try {
    const result = await sendJson("api/config/test", "POST", toPayloadModel(model));
    if (result.ok) {
      setTestResult(`✓ ${result.seconds} 秒，回复：${result.reply || "（空）"}`, "ok");
    } else {
      setTestResult(`✗ ${result.error}`, "error");
    }
  } catch (error) {
    setTestResult(`✗ ${error.message}`, "error");
  } finally {
    elements.testModelButton.disabled = false;
  }
}

async function saveSettings() {
  const payload = {
    models: settingsDraft.models.map(toPayloadModel),
    default_model: settingsDraft.defaultModel,
    approval_mode: elements.defaultModeSelect.value,
    max_steps: elements.maxStepsInput.value,
  };
  elements.settingsSave.disabled = true;
  setSettingsMessage("保存中…", true);
  try {
    await sendJson("api/config", "PUT", payload);
  } catch (error) {
    setSettingsMessage(error.message);
    return;
  } finally {
    elements.settingsSave.disabled = false;
  }
  closeSettings();
  await reloadInfo();
  // 当前会话的模型可能被删了、换成了默认模型，按服务端的结果对齐下拉框
  if (state.sessionId) {
    const snapshot = await requestJson(`api/sessions/${encodeURIComponent(state.sessionId)}`);
    setModel(snapshot.model_id);
  }
}

function bindSettingsControls() {
  elements.settingsButton.addEventListener("click", () => {
    openSettings().catch((error) => addNotice(`打开设置失败：${error.message}`, "error"));
  });
  elements.settingsClose.addEventListener("click", closeSettings);
  elements.settingsCancel.addEventListener("click", closeSettings);
  elements.settingsModal.addEventListener("click", (event) => {
    if (event.target === elements.settingsModal) {
      closeSettings();
    }
  });
  elements.settingsSave.addEventListener("click", saveSettings);
  elements.modelForm.addEventListener("input", onModelFieldInput);
  elements.modelForm.addEventListener("submit", (event) => event.preventDefault());
  elements.addModelButton.addEventListener("click", addModel);
  elements.deleteModelButton.addEventListener("click", deleteSelectedModel);
  elements.makeDefaultButton.addEventListener("click", makeSelectedDefault);
  elements.testModelButton.addEventListener("click", testSelectedModel);
}

// ===================== 初始化 =====================

function bindControls() {
  elements.sendButton.addEventListener("click", () => {
    if (state.running) {
      sendToServer({ type: "interrupt" });
    } else {
      submitInput();
    }
  });
  elements.input.addEventListener("input", resizeInput);
  elements.input.addEventListener("keydown", (event) => {
    // 中文输入法选词时按的 Enter 不算发送
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      submitInput();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    if (isSettingsOpen()) {
      closeSettings();
    } else if (state.running) {
      sendToServer({ type: "interrupt" });
    }
  });
  elements.modeSelect.addEventListener("change", () => {
    sendToServer({ type: "set_mode", mode: elements.modeSelect.value });
  });
  elements.modelSelect.addEventListener("change", () => {
    sendToServer({ type: "set_model", model_id: elements.modelSelect.value });
  });
  elements.compactButton.addEventListener("click", () => sendToServer({ type: "compact" }));
  elements.newSessionButton.addEventListener("click", startNewSession);
  elements.toggleSidebarButton.addEventListener("click", toggleSidebar);
  elements.sidebarBackdrop.addEventListener("click", closeMobileSidebar);
  bindSettingsControls();
}

async function init() {
  bindControls();
  await reloadInfo();
  fillModeOptions(elements.modeSelect, state.info.modes);
  setMode(state.info.default_mode);
  // 先定下这个标签页用哪个会话，再画侧栏，免得用户点了历史会话又被新建的会话盖掉
  await openSessionFromHashOrNew();
  await loadSessions();
  elements.input.focus();
}

init().catch((error) => addNotice(`初始化失败：${error.message}`, "error"));
