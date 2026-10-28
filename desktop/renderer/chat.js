// 对话视图：把后端的 snapshot / state / event / 审批 / 提问渲染成界面。
// 事件协议见 pocketagent/server/__init__.py 的说明；这里不依赖 web/static 的实现。

import { attachmentUrl, binaryDataUrl, openExternal, request, sessionFilePath } from "./api.js";
import { button, clear, el, isImagePath, renderDiff } from "./dom.js";
import { icon } from "./icons.js";
import { openFilePreview } from "./filepreview.js";
import { renderMarkdown } from "./markdown.js";

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

const SUBAGENT_STEPS_SHOWN = 4;
const TYPEWRITER_FRAMES_TO_CATCH_UP = 8;
const TYPEWRITER_MIN_CHARS_PER_FRAME = 2;
// 工具输出里的本地服务地址：带端口的 localhost / 127.0.0.1 / 0.0.0.0 / ::1，或带协议的 localhost（和 web 端同一条）
const LOCAL_LINK_RE =
  /(?:https?:\/\/)?((?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1)):(\d{2,5})(?:\/[^\s"'`<>)\],;]*)?|(?:https?:\/\/)(localhost)(?:\/[^\s"'`<>)\],;]*)?/gi;

function extractLocalLinks(text) {
  // 都归一成 http://localhost:端口/路径，去掉句尾的标点
  const links = [];
  for (const match of text.matchAll(LOCAL_LINK_RE)) {
    const host = match[1] || match[3];
    const port = match[2] || "";
    const afterHost = match[0].indexOf(host) + host.length + (port ? port.length + 1 : 0);
    const path = (match[0].slice(afterHost) || "").replace(/[./]+$/, "");
    links.push(`http://localhost${port ? `:${port}` : ""}${path}`);
  }
  return [...new Set(links)];
}
const FILE_TOOLS = new Set(["edit_file", "write_file", "bash"]);
const TODO_STATUS_ICONS = { pending: "○", in_progress: "●", completed: "✓" };

export class ChatView {
  constructor({ transcript, inner, callbacks }) {
    this.transcript = transcript;
    this.inner = inner;
    this.callbacks = callbacks;
    this.sessionId = null;

    this.currentAssistant = null;
    this.currentReasoning = null;
    this.activity = null;
    // 本轮最后一条收尾的回复块，turn_end 判断要不要挂「被掐断」角标用
    this.lastReply = null;
    this.toolCards = new Map();
    this.approvalCards = new Map();
    this.questionCards = new Map();
    // 「任务计划」面板：todo_write 维护，一张卡片原地更新
    this.todoPanel = null;
    this.running = false;
    this.stickToBottom = true;
    this.workspace = null;
    this.ephemeral = false;
    // 本轮从哪个子元素开始画的、回复里已经内联引用过哪些图，用来决定要不要自动贴图
    this.turnStartChildCount = null;
    this.turnImagePaths = new Set();

    this.transcript.addEventListener("scroll", () => {
      this.stickToBottom = this.isNearBottom();
    });
  }

  reset(sessionId = null) {
    this.finishStreamingBlocks();
    this.hideActivity();
    clear(this.inner);
    this.toolCards.clear();
    this.approvalCards.clear();
    this.questionCards.clear();
    this.todoPanel = null;
    this.currentAssistant = null;
    this.currentReasoning = null;
    this.running = false;
    this.stickToBottom = true;
    this.workspace = null;
    this.ephemeral = false;
    this.turnStartChildCount = null;
    this.turnImagePaths = new Set();
    this.sessionId = sessionId;
    this.transcript.scrollTop = 0;
  }

  // ---- 服务端消息 ----

  handleMessage(message) {
    switch (message.type) {
      case "snapshot":
        this.renderSnapshot(message);
        break;
      case "state":
        this.running = Boolean(message.running);
        this.workspace = message.workspace || this.workspace;
        this.ephemeral = Boolean(message.ephemeral);
        this.callbacks.onStatus?.(message);
        break;
      case "event":
        this.handleAgentEvent(message.event);
        break;
      case "approval_request":
        this.showApproval(message);
        break;
      case "approval_resolved":
        this.removeApproval(message.request_id);
        break;
      case "question_request":
        this.showQuestions(message);
        break;
      case "question_resolved":
        this.removeQuestions(message.request_id);
        break;
      case "returned_inputs":
        this.callbacks.onReturnedInputs?.(message.inputs || []);
        break;
      case "error":
        this.addNotice(message.message, "error");
        break;
      default:
        break;
    }
  }

  renderSnapshot(snapshot) {
    this.reset(snapshot.session_id);
    this.running = Boolean(snapshot.running);
    this.workspace = snapshot.workspace || null;
    this.ephemeral = Boolean(snapshot.ephemeral);
    for (const item of snapshot.items || []) {
      this.renderHistoryItem(item);
    }
    if ((snapshot.items || []).length === 0) {
      this.inner.append(el("div", "empty-chat", "说说要做什么，agent 会在这个工作区里读代码、改文件、跑命令。"));
    }
    for (const approval of snapshot.pending_approvals || []) {
      this.showApproval(approval);
    }
    for (const question of snapshot.pending_questions || []) {
      this.showQuestions(question);
    }
    // 刷新 / 切回来时从会话上恢复计划面板（面板出现在历史后面，反映当前进度）
    this.renderTodoPanel(snapshot.todos);
    this.callbacks.onSnapshot?.(snapshot);
    if (snapshot.running && (snapshot.pending_approvals || []).length === 0 && (snapshot.pending_questions || []).length === 0) {
      this.showActivity("运行中");
    }
    this.scrollToBottom();
  }

  renderHistoryItem(item) {
    switch (item.kind) {
      case "user":
        this.addUserMessage(item.text, item.attachments);
        break;
      case "assistant":
        this.appendStaticAssistant(item.text, item.finish);
        break;
      case "summary":
        this.addSummary(item.text);
        break;
      case "notice":
        this.addNotice(item.text, "warning");
        break;
      case "tool": {
        this.addToolCard(item.call_id, item.name, item.summary);
        if (item.review) {
          this.setToolReview(item.call_id, item.review);
        }
        if (item.result !== null && item.result !== undefined) {
          this.setToolResult(item.call_id, item.result, item.is_error, item.denied, item.name);
        }
        break;
      }
      default:
        break;
    }
  }

  // ---- agent 事件 ----

  handleAgentEvent(event) {
    switch (event.type) {
      case "turn_start":
        this.finishStreamingBlocks();
        this.lastReply = null;
        this.turnStartChildCount = this.inner.children.length;
        this.turnImagePaths = new Set();
        this.addUserMessage(event.user_text, event.attachments);
        this.showActivity("等待模型响应");
        break;
      case "interjection_applied":
        this.finishStreamingBlocks();
        this.addUserMessage(event.user_text, event.attachments, "插话");
        this.showActivity("等待模型响应");
        break;
      case "text_delta":
        this.hideActivity();
        this.appendAssistantText(event.text);
        break;
      case "reasoning_delta":
        this.hideActivity();
        this.appendReasoning(event.text);
        break;
      case "llm_response":
        this.finishStreamingBlocks();
        this.callbacks.onModelResponded?.();
        break;
      case "llm_retry":
        this.discardPartialAssistant();
        this.addNotice(`模型调用失败（${event.error}），${event.wait_seconds} 秒后第 ${event.attempt} 次尝试`, "warning");
        this.showActivity("重试中");
        break;
      case "llm_truncated":
        this.finishAssistantBlock();
        this.addNotice(truncatedNotice(event), "warning");
        this.showActivity("等待模型响应");
        break;
      case "tool_call":
        this.finishStreamingBlocks();
        this.hideActivity();
        this.addToolCard(event.call_id, event.name, event.summary);
        break;
      case "subagent_progress":
        this.addSubagentStep(event);
        break;
      case "tool_review":
        this.setToolReview(event.call_id, { allow: event.allow, reason: event.reason });
        break;
      case "tool_output_delta":
        this.hideActivity();
        this.appendToolOutput(event.call_id, event.text);
        break;
      case "tool_result":
        this.setToolResult(event.call_id, event.content, event.is_error, event.denied, event.name);
        if (FILE_TOOLS.has(event.name) && !event.denied) {
          this.callbacks.onFilesChanged?.();
        }
        if (!event.denied && event.content) {
          // 输出里有本地服务地址（dev server 之类）就交给右栏的「预览」当候选
          this.callbacks.onLocalLinks?.(extractLocalLinks(event.content));
        }
        if (this.running && !event.denied) {
          this.showActivity("等待模型响应");
        }
        break;
      case "todos_updated":
        this.renderTodoPanel(event.todos);
        break;
      case "checkpoint":
        this.callbacks.onFilesChanged?.();
        break;
      case "compaction_waiting":
        this.showActivity("上下文快满了，等后台压缩做完");
        break;
      case "compaction_failed":
        this.addNotice(`后台压缩失败：${event.message}（下次超过阈值会再试）`, "warning");
        break;
      case "compaction":
        this.addNotice(compactionNotice(event), "info");
        break;
      case "step_limit":
        this.addNotice(`这一轮已调用模型 ${event.max_steps} 次，达到上限，先停下`, "warning");
        break;
      case "error":
        this.addNotice(event.message, "error");
        break;
      case "turn_end": {
        this.finishStreamingBlocks();
        this.hideActivity();
        this.markCutOffReply(event.stop_reason);
        const notice = TURN_END_NOTICES[event.stop_reason];
        if (notice) {
          this.addNotice(notice[0], notice[1]);
        }
        this.callbacks.onTurnEnd?.(event);
        this.showTurnImages();
        break;
      }
      default:
        break;
    }
  }

  // ---- 对话条目 ----

  addUserMessage(text, attachments = [], badge = "") {
    const wrapper = el("div", "message user");
    if (attachments.length > 0) {
      wrapper.append(this.buildAttachments(attachments));
    }
    if (text || attachments.length === 0) {
      const bubble = el("div", "bubble");
      if (badge) {
        bubble.append(el("span", "bubble-badge", badge));
      }
      bubble.append(document.createTextNode(text || "（附件）"));
      wrapper.append(bubble);
    }
    this.appendNode(wrapper);
  }

  buildAttachments(attachments) {
    const list = el("div", "message-attachments");
    for (const attachment of attachments) {
      const link = el("a", `message-attachment ${attachment.kind || "file"}`);
      link.href = "#";
      link.title = attachment.name;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        this.callbacks.onOpenAttachment?.(attachment);
      });
      if (attachment.kind === "image") {
        const image = el("img");
        image.alt = attachment.name;
        image.src = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";
        if (this.sessionId) {
          binaryDataUrl(attachmentUrl(this.sessionId, attachment.id))
            .then((url) => {
              image.src = url;
            })
            .catch(() => {
              image.remove();
            });
        }
        link.append(image);
      } else {
        link.append(icon("file"), el("span", "", attachment.name || "附件"));
      }
      list.append(link);
    }
    return list;
  }

  appendStaticAssistant(text, finish = "") {
    const element = el("div", "message assistant");
    const body = el("div", "assistant-body");
    body.append(renderMarkdown(text, this.markdownOptions()));
    element.append(body);
    const badge = buildCutOffBadge(finish);
    if (badge) {
      // 历史里重放时也把「这条是被掐断的」标出来
      element.append(badge);
    }
    this.appendNode(element);
  }

  // 回复里的 ![说明](路径)：工作区内的相对路径换成会话取图接口，临时任务也能显示
  markdownOptions() {
    return {
      resolveImage: (source) => {
        const relative = this.relativeWorkspacePath(source);
        return relative && this.sessionId ? sessionFilePath(this.sessionId, relative) : null;
      },
      onImage: (apiPath) => this.turnImagePaths.add(apiPath),
      onOpenImage: (source) => this.openWorkspaceImage(source),
      onOpenExternalImage: (url) => openExternal(url),
    };
  }

  relativeWorkspacePath(source) {
    const workspace = this.workspace;
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
    return relative;
  }

  // 点图片打开文件预览；临时任务的工作区没登记，预览打不开就不响应
  openWorkspaceImage(source) {
    const relative = this.relativeWorkspacePath(source);
    if (!relative || this.ephemeral || !this.workspace) {
      return;
    }
    openFilePreview(this.workspace, relative);
  }

  // 一轮结束时，把本轮新增 / 改过的图片补在回复下面；回复里已经贴过的就不重复
  async showTurnImages() {
    const sessionId = this.sessionId;
    if (!sessionId) {
      return;
    }
    let result;
    try {
      result = await request(`/api/sessions/${encodeURIComponent(sessionId)}/changes/turn`);
    } catch (error) {
      return;
    }
    if (sessionId !== this.sessionId) {
      return;
    }
    const seen = new Set();
    const changed = [];
    for (const change of result.changes || []) {
      if (change.status === "deleted" || !isImagePath(change.path) || seen.has(change.path)) {
        continue;
      }
      seen.add(change.path);
      changed.push(change.path);
    }
    if (changed.length === 0) {
      return;
    }
    const missing = changed.filter((path) => !this.turnImagePaths.has(sessionFilePath(sessionId, path)));
    if (missing.length > 0) {
      this.appendNode(this.buildTurnImages(missing));
    }
  }

  buildTurnImages(paths) {
    const wrapper = el("div", "turn-images");
    wrapper.append(el("div", "turn-images-title", "本轮产出的图片"));
    const grid = el("div", "turn-images-grid");
    for (const path of paths) {
      const item = el("a", "turn-image");
      item.href = "#";
      item.title = path;
      const image = el("img");
      image.alt = path;
      item.append(image, el("span", "turn-image-path", path));
      item.addEventListener("click", (event) => {
        event.preventDefault();
        this.openWorkspaceImage(path);
      });
      binaryDataUrl(sessionFilePath(this.sessionId, path))
        .then((url) => {
          image.src = url;
        })
        .catch(() => {
          item.remove();
        });
      grid.append(item);
    }
    wrapper.append(grid);
    return wrapper;
  }

  // 这一轮是被上游掐断的：角标挂在最后一条回复末尾；一条回复都没有就退化成一行提示
  markCutOffReply(stopReason) {
    const labels = CUT_OFF_BADGES[stopReason];
    if (!labels) {
      return;
    }
    if (!this.lastReply) {
      this.addNotice(`${labels[0]}：${labels[1]}`, "warning");
      return;
    }
    this.lastReply.append(buildCutOffBadge(stopReason));
    this.scrollIfSticking();
  }

  ensureAssistantBlock() {
    if (this.currentAssistant) {
      return this.currentAssistant;
    }
    const element = el("div", "message assistant");
    const body = el("div", "assistant-body");
    element.append(body);
    this.insertBeforeActivity(element);
    // text：收到的全部；shown：已经画出来的；pending：还在缓冲里等着按打字机节奏放出来的
    this.currentAssistant = { element, body, text: "", shown: "", pending: "", frameScheduled: false };
    return this.currentAssistant;
  }

  appendAssistantText(text) {
    const block = this.ensureAssistantBlock();
    block.text += text;
    block.pending += text;
    this.scheduleTypewriter(block);
  }

  // ---- 流式输出：打字机节奏（和 web 的 task/chat.js 一样） ----
  // 网关常常一阵一阵地推（几十个片段同时到，然后停 100ms），直接画会一顿一顿的。
  // 收到的字先进缓冲，每帧按积压量放出一部分：积压少就慢慢出，积压多就加快追上。

  scheduleTypewriter(block) {
    if (block.frameScheduled) {
      return;
    }
    block.frameScheduled = true;
    requestAnimationFrame(() => this.typewriterFrame(block));
  }

  typewriterFrame(block) {
    block.frameScheduled = false;
    if (!block.pending || block !== this.currentAssistant) {
      return;
    }
    const take = Math.max(TYPEWRITER_MIN_CHARS_PER_FRAME, Math.ceil(block.pending.length / TYPEWRITER_FRAMES_TO_CATCH_UP));
    block.shown += block.pending.slice(0, take);
    block.pending = block.pending.slice(take);
    // 流式期间先放纯文本，结束时再整体按 Markdown 渲染（每帧重排 Markdown 会反复读图片）
    block.body.textContent = block.shown;
    this.scrollIfSticking();
    if (block.pending) {
      this.scheduleTypewriter(block);
    }
  }

  finishAssistantBlock() {
    if (!this.currentAssistant) {
      return;
    }
    this.currentAssistant.pending = "";
    this.currentAssistant.body.replaceChildren(renderMarkdown(this.currentAssistant.text, this.markdownOptions()));
    this.lastReply = this.currentAssistant.element;
    this.currentAssistant = null;
    this.scrollIfSticking();
  }

  appendReasoning(text) {
    if (!this.currentReasoning) {
      const details = el("details", "reasoning");
      details.open = true;
      const summary = el("summary");
      const label = el("span", "reasoning-label", "思考中");
      const preview = el("span", "reasoning-preview");
      summary.append(label, preview);
      const body = el("div", "reasoning-body");
      const bodyText = document.createTextNode("");
      body.append(bodyText);
      details.append(summary, body);
      this.insertBeforeActivity(details);

      const block = {
        element: details,
        label,
        preview,
        bodyText,
        length: 0,
        tail: "",
        startedAt: performance.now(),
        timer: null,
      };
      block.timer = setInterval(() => this.updateReasoningLabel(block), 100);
      this.currentReasoning = block;
    }
    const block = this.currentReasoning;
    block.bodyText.appendData(text);
    block.length += text.length;
    block.tail = (block.tail + text).slice(-96);
    this.scrollIfSticking();
  }

  updateReasoningLabel(block) {
    block.label.textContent = `思考中 ${elapsedSeconds(block.startedAt)}s`;
    block.preview.textContent = block.tail.replace(/\s+/g, " ").trim().slice(-48);
  }

  finishReasoning() {
    if (!this.currentReasoning) {
      return;
    }
    clearInterval(this.currentReasoning.timer);
    this.currentReasoning.label.textContent = `思考了 ${elapsedSeconds(this.currentReasoning.startedAt)}s（${this.currentReasoning.length} 字）`;
    this.currentReasoning.preview.textContent = "";
    this.currentReasoning.element.open = false;
    this.currentReasoning = null;
  }

  finishStreamingBlocks() {
    this.finishReasoning();
    this.finishAssistantBlock();
  }

  discardPartialAssistant() {
    if (this.currentReasoning) {
      clearInterval(this.currentReasoning.timer);
      this.currentReasoning.element.remove();
      this.currentReasoning = null;
    }
    this.currentAssistant?.element.remove();
    this.currentAssistant = null;
  }

  addToolCard(callId, name, summary) {
    if (callId && this.toolCards.has(callId)) {
      return this.toolCards.get(callId);
    }
    const details = el("details", "tool-card");
    const header = el("summary", "tool-card-header");
    header.append(el("span", "tool-name", name || "tool"), el("span", "tool-summary", summary || ""));
    const status = el("span", "tool-status", "执行中…");
    header.append(status);
    const body = el("div", "tool-body");
    details.append(header, body);
    this.insertBeforeActivity(details);

    // liveOutput：bash 边跑边出的输出；autoOpened：是不是为了看实时输出才自动展开的（跑完要收起）
    const card = { element: details, body, status, steps: null, liveOutput: null, autoOpened: false };
    details.addEventListener("toggle", () => {
      if (card.togglingByCode) {
        card.togglingByCode = false;
        return;
      }
      // 用户自己点过就听用户的：跑完不再自动收起
      card.autoOpened = false;
    });
    if (callId) {
      this.toolCards.set(callId, card);
    }
    this.scrollIfSticking();
    return card;
  }

  setCardOpen(card, open) {
    if (card.element.open === open) {
      return;
    }
    card.togglingByCode = true;
    card.element.open = open;
  }

  appendToolOutput(callId, text) {
    const card = this.toolCards.get(callId);
    if (!card || !text) {
      return;
    }
    if (!card.liveOutput) {
      card.liveOutput = el("pre");
      card.body.append(card.liveOutput);
      // 第一次收到输出：展开卡片，不然流式输出在折叠的卡片里看不见；跑完再收起（见 setToolResult）
      if (!card.element.open) {
        this.setCardOpen(card, true);
        card.autoOpened = true;
      }
    }
    const output = card.liveOutput;
    const stickToBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 24;
    output.append(document.createTextNode(text));
    if (stickToBottom) {
      output.scrollTop = output.scrollHeight;
    }
    this.scrollIfSticking();
  }

  setToolResult(callId, content, isError, denied, name = "") {
    const card = this.toolCards.get(callId) || this.addToolCard(callId, name, "");
    // 只为看实时输出才自动展开的卡片，成功跑完就收起；出错的下面会保持展开
    if (card.autoOpened && !isError) {
      this.setCardOpen(card, false);
    }
    card.autoOpened = false;
    card.liveOutput = null;
    if (denied) {
      card.status.textContent = "被拒绝";
      card.status.className = "tool-status denied";
    } else if (isError) {
      card.status.textContent = "出错";
      card.status.className = "tool-status error";
      card.element.classList.add("error");
      this.setCardOpen(card, true);
    } else {
      card.status.textContent = "完成";
      card.status.className = "tool-status";
    }
    const existing = card.body.querySelector("pre");
    const pre = existing || el("pre");
    pre.textContent = content || "（没有输出）";
    if (!existing) {
      card.body.append(pre);
    }
    this.scrollIfSticking();
  }

  setToolReview(callId, review) {
    if (!review) {
      return;
    }
    const card = this.toolCards.get(callId) || this.addToolCard(callId, "", "");
    const verdict = review.allow ? "AI 审批放行" : "AI 审批没有放行";
    const reason = review.reason ? `：${review.reason}` : "";
    card.body.append(el("div", "tool-review", verdict + reason));
    this.scrollIfSticking();
  }

  // ---- 任务计划面板 ----

  renderTodoPanel(todos) {
    if (!Array.isArray(todos) || todos.length === 0) {
      this.todoPanel?.remove();
      this.todoPanel = null;
      return;
    }
    if (!this.todoPanel || !this.todoPanel.isConnected) {
      const panel = el("div", "todo-panel");
      const header = el("div", "todo-header");
      header.append(icon("listCheck", "todo-icon"), el("span", "todo-title"), el("span", "todo-progress"));
      panel.append(header, el("div", "todo-items"));
      this.todoPanel = panel;
      this.appendNode(panel);
    }
    const done = todos.filter((item) => item.status === "completed").length;
    this.todoPanel.querySelector(".todo-title").textContent = "任务计划";
    this.todoPanel.querySelector(".todo-progress").textContent = `${done}/${todos.length}`;
    const items = this.todoPanel.querySelector(".todo-items");
    clear(items);
    for (const item of todos) {
      const row = el("div", `todo-item ${item.status}`);
      const label = item.status === "in_progress" ? item.active_form || item.content : item.content;
      row.append(el("span", "todo-mark", TODO_STATUS_ICONS[item.status] || "○"), el("span", "todo-text", label));
      items.append(row);
    }
    this.scrollIfSticking();
  }

  addSubagentStep(event) {
    const card = this.toolCards.get(event.call_id);
    if (!card) {
      return;
    }
    if (!card.steps) {
      card.steps = el("div", "subagent-steps");
      card.steps.dataset.count = "0";
      card.element.insertBefore(card.steps, card.body);
    }
    const count = Number(card.steps.dataset.count) + (event.step === "tool" ? 1 : 0);
    card.steps.dataset.count = String(count);

    const line = el("div", "subagent-step", `${event.step === "tool_error" ? "✗" : "↳"} ${event.name}  ${event.summary}`);
    if (event.step === "tool_error") {
      line.classList.add("error");
    }
    card.steps.querySelector(".subagent-count")?.remove();
    card.steps.append(line);
    const lines = [...card.steps.querySelectorAll(".subagent-step")];
    lines.slice(0, Math.max(0, lines.length - SUBAGENT_STEPS_SHOWN)).forEach((each) => each.remove());
    if (count > SUBAGENT_STEPS_SHOWN) {
      card.steps.prepend(el("div", "subagent-count", `子 agent 已经调了 ${count} 次工具，下面是最近几步`));
    }
    this.scrollIfSticking();
  }

  addNotice(text, kind = "info") {
    const node = el("div", `notice ${kind}`, text);
    this.appendNode(node);
    return node;
  }

  addCommandOutput(text, kind = "info") {
    if (!text) {
      return;
    }
    const node = el("div", `notice command-output ${kind}`, text);
    this.appendNode(node);
  }

  addSummary(text) {
    const details = el("details", "notice");
    const summary = el("summary", "", "之前的对话已压缩，点开看摘要");
    const pre = el("pre");
    pre.textContent = text;
    details.append(summary, pre);
    this.appendNode(details);
  }

  // ---- 审批 ----

  showApproval(request) {
    if (!request?.request_id || this.approvalCards.has(request.request_id)) {
      return;
    }
    const card = el("div", "card approval");
    const header = el("div", "card-header");
    header.append(el("span", "card-badge", "需要确认"), el("span", "card-title", request.tool_name || "工具调用"));
    card.append(header);

    const note = String(request.summary || "").match(/（AI 审批没有放行：(.*)）$/);
    if (note) {
      card.append(el("div", "approval-note", `AI 审批没有放行：${note[1]}`));
    }
    for (const warning of request.notes || []) {
      card.append(el("div", "approval-note approval-warning", warning));
    }
    card.append(renderDiff(request.preview || request.summary || "", "card-preview"));

    const actions = el("div", "card-actions");
    const allowOnce = button("primary-button", "允许一次");
    const allowAlways = request.always_label ? button("", request.always_label) : null;
    // 访问工作目录外时可以连父目录一起放行，之后兄弟目录也不再问
    const allowAlwaysParents = request.always_parents_label ? button("", request.always_parents_label) : null;
    const deny = button("danger-button", "拒绝");
    const answer = (value) => {
      this.callbacks.onApproval?.(request.request_id, value);
      for (const node of actions.querySelectorAll("button")) {
        node.disabled = true;
      }
    };
    allowOnce.addEventListener("click", () => answer("allow_once"));
    deny.addEventListener("click", () => answer("deny"));
    if (allowAlways) {
      allowAlways.addEventListener("click", () => answer("allow_always"));
    }
    if (allowAlwaysParents) {
      allowAlwaysParents.addEventListener("click", () => answer("allow_always_parents"));
    }
    actions.append(allowOnce);
    if (allowAlways) {
      actions.append(allowAlways);
    }
    if (allowAlwaysParents) {
      actions.append(allowAlwaysParents);
    }
    actions.append(deny);
    card.append(actions);

    this.appendNode(card);
    this.approvalCards.set(request.request_id, card);
    this.scrollToBottom();
  }

  removeApproval(requestId) {
    this.approvalCards.get(requestId)?.remove();
    this.approvalCards.delete(requestId);
  }

  // ---- 提问 ----

  showQuestions(request) {
    if (!request?.request_id || this.questionCards.has(request.request_id)) {
      return;
    }
    this.hideActivity();
    const card = el("div", "card question");
    const header = el("div", "card-header");
    header.append(el("span", "card-badge", "需要回答"), el("span", "card-title", "agent 想确认几件事"));
    card.append(header);

    const fields = [];
    request.questions.forEach((question, questionIndex) => {
      const field = el("div", "question-field");
      const label = el("div", "question-label");
      label.append(document.createTextNode(question.question));
      if (question.header) {
        label.prepend(el("span", "question-header", question.header));
      }
      field.append(label);

      const options = el("div", "question-options");
      const inputType = question.multi_select ? "checkbox" : "radio";
      const optionInputs = [];
      for (const option of question.options) {
        const optionLabel = el("label", "question-option");
        const input = document.createElement("input");
        input.type = inputType;
        input.name = `q-${request.request_id}-${questionIndex}`;
        input.value = option.label;
        optionInputs.push(input);

        const text = el("span", "question-option-text");
        text.append(el("span", "", option.label));
        if (option.description) {
          text.append(el("span", "question-option-description", option.description));
        }
        optionLabel.append(input, text);
        options.append(optionLabel);
      }
      field.append(options);

      const other = document.createElement("input");
      other.type = "text";
      other.className = "question-other";
      other.placeholder = "其他（自己写，可留空）";
      field.append(other);

      card.append(field);
      fields.push({ optionInputs, other });
    });

    const actions = el("div", "card-actions");
    const submit = button("primary-button", "提交");
    const skip = button("text-button", "跳过");
    const finish = (answers) => {
      this.callbacks.onQuestionAnswer?.(request.request_id, answers);
      for (const node of card.querySelectorAll("input, button")) {
        node.disabled = true;
      }
    };
    submit.addEventListener("click", () => {
      const answers = fields.map(({ optionInputs, other }) => ({
        selected: optionInputs.filter((input) => input.checked).map((input) => input.value),
        other: other.value.trim(),
      }));
      finish(answers);
    });
    skip.addEventListener("click", () => finish(null));
    actions.append(submit, skip);
    card.append(actions);

    this.appendNode(card);
    this.questionCards.set(request.request_id, card);
    this.scrollToBottom();
  }

  removeQuestions(requestId) {
    this.questionCards.get(requestId)?.remove();
    this.questionCards.delete(requestId);
    if (this.running) {
      this.showActivity("等待模型响应");
    }
  }

  // ---- 活动提示和滚动 ----

  showActivity(label) {
    if (!this.activity) {
      const element = el("div", "activity");
      const labelElement = el("span", "activity-label");
      const seconds = el("span", "activity-seconds");
      element.append(el("span", "activity-dot"), labelElement, seconds);
      this.inner.append(element);
      this.activity = { element, labelElement, seconds, startedAt: 0, timer: null };
    }
    const activity = this.activity;
    activity.labelElement.textContent = label;
    activity.startedAt = performance.now();
    activity.seconds.textContent = "";
    clearInterval(activity.timer);
    activity.timer = setInterval(() => {
      activity.seconds.textContent = `${elapsedSeconds(activity.startedAt)}s`;
    }, 200);
    this.inner.append(activity.element);
    this.scrollIfSticking();
  }

  hideActivity() {
    if (!this.activity) {
      return;
    }
    clearInterval(this.activity.timer);
    this.activity.element.remove();
    this.activity = null;
  }

  appendNode(node) {
    this.insertBeforeActivity(node);
    this.scrollIfSticking();
  }

  insertBeforeActivity(node) {
    // 对话里有了第一条内容，「说说要做什么」这句空白提示就该消失（web 端同样处理）
    this.inner.querySelector(".empty-chat")?.remove();
    if (this.activity) {
      this.inner.insertBefore(node, this.activity.element);
      return;
    }
    this.inner.append(node);
  }

  isNearBottom() {
    return this.transcript.scrollHeight - this.transcript.scrollTop - this.transcript.clientHeight < 120;
  }

  scrollIfSticking() {
    if (this.stickToBottom) {
      this.scrollToBottom();
    }
  }

  scrollToBottom() {
    this.transcript.scrollTop = this.transcript.scrollHeight;
  }
}

function elapsedSeconds(startedAt) {
  return ((performance.now() - startedAt) / 1000).toFixed(1);
}

function truncatedNotice(event) {
  if (event.gave_up) {
    return "回复多次超出输出长度上限，先停下了";
  }
  if (event.had_tool_calls) {
    return "回复超出输出长度上限被截断，工具调用不完整没有执行，已让模型拆小重来";
  }
  return "回复超出输出长度上限被截断，已让模型接着写";
}

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
