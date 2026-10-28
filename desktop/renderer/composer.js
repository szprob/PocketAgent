// 输入框：「新任务」页和任务页各用一个。
// 文本框 + 附件（选择 / 粘贴 / 拖进来）+ 权限 + 模型 + 发送 / 停止，底下一行是工作区和上下文占用。
// 输入以 / 开头时浮出命令补全（命令表来自 /api/info，技能名来自 /api/skills）。

import { button, el, formatBytes, show, workspaceName } from "./dom.js";
import { icon } from "./icons.js";
import { ModelPicker } from "./modelpicker.js";

const MAX_TEXTAREA_HEIGHT = 240;

// 工作区下拉里「不指定工作区」那一项的值。真实工作区都是绝对路径，不会撞上。
export const SCRATCH_WORKSPACE = "__scratch__";
const SCRATCH_LABEL = "临时任务（不指定工作区）";
const SCRATCH_TITLE = "在 ~/.pocketagent/scratch 下新建一个临时目录，不碰任何项目";

const MODE_LABELS = {
  ask: "每次询问",
  ai_auto: "AI 自动审批",
  full_auto: "完全访问",
  read_only: "只读",
  auto_edit: "自动改文件",
};

export class Composer {
  constructor(container, options) {
    this.kind = options.kind;
    this.onSubmit = options.onSubmit;
    this.onStop = options.onStop;
    this.onModeChange = options.onModeChange;
    this.onModelChange = options.onModelChange;
    this.onWorkspaceChange = options.onWorkspaceChange;

    this.pendingFiles = [];
    // 从对话里「恢复到这里」带回来的附件：服务端已经有了，重发时只传 id
    this.pendingAttachments = [];
    this.running = false;
    this.commands = [];
    this.skillNames = [];
    this.commandSelection = 0;

    this.root = this.build(options.placeholder);
    container.replaceChildren(this.root);
  }

  build(placeholder) {
    const wrapper = el("div", "composer-wrapper");
    this.card = el("div", "composer");
    this.commandMenu = el("div", "command-menu hidden");
    this.chips = el("div", "attachment-chips hidden");
    this.textarea = el("textarea", "composer-input");
    this.textarea.rows = 1;
    this.textarea.placeholder = placeholder;
    this.card.append(this.commandMenu, this.chips, this.textarea, this.buildToolbar());
    wrapper.append(this.card, this.buildFooter());
    this.bindInput();
    return wrapper;
  }

  buildToolbar() {
    const toolbar = el("div", "composer-toolbar");

    this.fileInput = document.createElement("input");
    this.fileInput.type = "file";
    this.fileInput.multiple = true;
    this.fileInput.hidden = true;
    const attachButton = iconButton("paperclip", "添加图片或文件（也可以直接粘贴、拖进来）", "tool-button");
    attachButton.addEventListener("click", () => this.fileInput.click());
    this.fileInput.addEventListener("change", () => {
      this.addFiles(this.fileInput.files);
      this.fileInput.value = "";
    });

    this.permissionPill = el("label", "pill");
    this.permissionIcon = el("span", "pill-icon");
    this.permissionSelect = el("select");
    this.permissionSelect.title = "权限";
    this.permissionPill.append(this.permissionIcon, this.permissionSelect, icon("chevronDown", "pill-chevron"));
    this.permissionSelect.addEventListener("change", () => {
      this.paintPermission();
      this.onModeChange?.(this.permissionSelect.value);
    });

    this.modelPicker = new ModelPicker({ onSelectModel: (modelId) => this.onModelChange?.(modelId) });

    this.stopButton = button("stop-button hidden", "停止", "stop", "停止（Esc）");
    this.stopButton.addEventListener("click", () => this.onStop?.());
    this.sendButton = button("send-button", "发送", "arrowUp", "发送（Enter）");
    this.sendButtonLabel = this.sendButton.querySelector("span");
    this.sendButton.addEventListener("click", () => this.submit());

    toolbar.append(
      attachButton,
      this.fileInput,
      this.permissionPill,
      this.modelPicker.root,
      el("div", "toolbar-spacer"),
      this.stopButton,
      this.sendButton,
    );
    return toolbar;
  }

  buildFooter() {
    const footer = el("div", "composer-footer");
    const workspace = el("span", "footer-workspace");
    workspace.append(icon("monitor"));
    if (this.kind === "new") {
      this.workspaceSelect = el("select", "workspace-select");
      this.workspaceSelect.title = "在哪个工作区里做";
      this.workspaceSelect.addEventListener("change", () => {
        fitSelectToSelection(this.workspaceSelect);
        this.onWorkspaceChange?.();
      });
      workspace.append(this.workspaceSelect, icon("chevronDown", "footer-chevron"));
    } else {
      this.workspaceLabel = el("span", "workspace-label");
      workspace.append(this.workspaceLabel);
    }
    this.busyHint = el("span", "busy-hint");
    this.compactingHint = el("span", "compacting-hint hidden", "后台压缩上下文中…");

    this.contextMeter = el("span", "context-meter hidden");
    this.contextBar = el("span", "context-bar");
    this.contextFill = el("span", "context-fill");
    this.contextMarker = el("span", "context-marker hidden");
    this.contextBar.append(this.contextFill, this.contextMarker);
    this.contextText = el("span", "context-text");
    this.contextMeter.append(this.contextBar, this.contextText);

    footer.append(workspace, this.busyHint, el("div", "toolbar-spacer"), this.compactingHint, this.contextMeter);
    return footer;
  }

  bindInput() {
    this.textarea.addEventListener("input", () => {
      this.resize();
      this.updateCommandMenu();
    });
    this.textarea.addEventListener("keydown", (event) => {
      if (this.commandMenuOpen()) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          this.moveCommandSelection(event.key === "ArrowDown" ? 1 : -1);
          return;
        }
        if (event.key === "Tab") {
          event.preventDefault();
          this.acceptCommandSuggestion();
          return;
        }
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
          const selected = this.commandMenu.querySelectorAll(".command-item")[this.commandSelection];
          const name = selected?.querySelector(".command-name")?.textContent || "";
          if (this.textarea.value.trim() !== name) {
            event.preventDefault();
            this.fillCommand(name);
            return;
          }
        }
        if (event.key === "Escape") {
          event.preventDefault();
          this.hideCommandMenu();
          return;
        }
      }
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        this.submit();
      }
    });
    this.textarea.addEventListener("blur", () => {
      setTimeout(() => this.hideCommandMenu(), 150);
    });
    this.textarea.addEventListener("paste", (event) => {
      const files = event.clipboardData?.files;
      if (files && files.length > 0) {
        event.preventDefault();
        this.addFiles(files);
      }
    });
    this.card.addEventListener("dragover", (event) => {
      event.preventDefault();
      this.card.classList.add("dragging");
    });
    this.card.addEventListener("dragleave", () => this.card.classList.remove("dragging"));
    this.card.addEventListener("drop", (event) => {
      event.preventDefault();
      this.card.classList.remove("dragging");
      this.addFiles(event.dataTransfer.files);
    });
  }

  resize() {
    this.textarea.style.height = "auto";
    this.textarea.style.height = `${Math.min(this.textarea.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }

  // ---- 命令补全 ----

  commandToken() {
    const value = this.textarea.value;
    if (!value.startsWith("/") || /\s/.test(value)) {
      return null;
    }
    return value.trim().toLowerCase();
  }

  commandMenuOpen() {
    return !this.commandMenu.classList.contains("hidden");
  }

  updateCommandMenu() {
    const token = this.commandToken();
    if (token === null) {
      this.hideCommandMenu();
      return;
    }
    const matches = [...this.commands, ...this.skillNames.map((name) => ({ name, summary: "技能" }))].filter((item) =>
      `/${item.name}`.startsWith(token),
    );
    if (matches.length === 0) {
      this.hideCommandMenu();
      return;
    }
    this.commandMenu.replaceChildren();
    matches.slice(0, 8).forEach((item, index) => {
      const row = el("button", `command-item${index === 0 ? " selected" : ""}`);
      row.type = "button";
      row.append(el("span", "command-name", `/${item.name}`), el("span", "command-summary", item.summary || ""));
      row.addEventListener("mousedown", (event) => {
        event.preventDefault();
        this.fillCommand(`/${item.name}`);
      });
      this.commandMenu.append(row);
    });
    this.commandSelection = 0;
    show(this.commandMenu, true);
  }

  hideCommandMenu() {
    show(this.commandMenu, false);
    this.commandSelection = 0;
  }

  moveCommandSelection(delta) {
    const items = [...this.commandMenu.querySelectorAll(".command-item")];
    if (items.length === 0) {
      return;
    }
    this.commandSelection = (this.commandSelection + delta + items.length) % items.length;
    items.forEach((item, index) => item.classList.toggle("selected", index === this.commandSelection));
  }

  acceptCommandSuggestion() {
    const items = [...this.commandMenu.querySelectorAll(".command-item")];
    const selected = items[this.commandSelection];
    if (selected) {
      this.fillCommand(selected.querySelector(".command-name").textContent);
    }
  }

  fillCommand(text) {
    this.textarea.value = `${text} `;
    this.hideCommandMenu();
    this.resize();
    this.focus();
  }

  setCommands(commands) {
    this.commands = commands || [];
    this.updateCommandMenu();
  }

  setSkillNames(names) {
    this.skillNames = names || [];
    this.updateCommandMenu();
  }

  // ---- 附件 ----

  addFiles(fileList) {
    for (const file of fileList) {
      const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
      this.pendingFiles.push({ file, previewUrl });
    }
    this.renderChips();
  }

  removeFile(index) {
    const [removed] = this.pendingFiles.splice(index, 1);
    if (removed?.previewUrl) {
      URL.revokeObjectURL(removed.previewUrl);
    }
    this.renderChips();
  }

  renderChips() {
    this.chips.replaceChildren();
    const total = this.pendingFiles.length + this.pendingAttachments.length;
    show(this.chips, total > 0);
    this.pendingFiles.forEach((pending, index) => {
      this.chips.append(this.fileChip(pending, index));
    });
    this.pendingAttachments.forEach((attachment, index) => {
      this.chips.append(this.attachmentChip(attachment, index));
    });
  }

  fileChip(pending, index) {
    const chip = el("div", "chip");
    if (pending.previewUrl) {
      const image = el("img", "chip-thumbnail");
      image.src = pending.previewUrl;
      image.alt = pending.file.name;
      chip.append(image);
    } else {
      chip.append(icon("file", "chip-icon"));
    }
    const label = el("span", "chip-label");
    label.append(el("span", "chip-name", pending.file.name || "粘贴的图片"), el("span", "chip-size", formatBytes(pending.file.size)));
    const remove = iconButton("close", "移除", "chip-remove");
    remove.addEventListener("click", () => this.removeFile(index));
    chip.append(label, remove);
    return chip;
  }

  attachmentChip(attachment, index) {
    const chip = el("div", "chip");
    chip.append(icon(attachment?.kind === "image" ? "image" : "file", "chip-icon"));
    const label = el("span", "chip-label");
    label.append(
      el("span", "chip-name", attachment?.name || "附件"),
      el("span", "chip-size", formatBytes(attachment?.size || 0))
    );
    const remove = iconButton("close", "移除", "chip-remove");
    remove.addEventListener("click", () => this.removeAttachment(index));
    chip.append(label, remove);
    return chip;
  }

  removeAttachment(index) {
    this.pendingAttachments.splice(index, 1);
    this.renderChips();
  }

  // ---- 发送 ----

  async submit() {
    const text = this.textarea.value.trim();
    if (!text && this.pendingFiles.length === 0 && this.pendingAttachments.length === 0) {
      return;
    }
    const files = this.pendingFiles.map((pending) => pending.file);
    const attachmentIds = this.pendingAttachments.map((attachment) => attachment.id);
    const sent = await this.onSubmit({ text, files, attachmentIds });
    if (sent) {
      this.clear();
    }
  }

  clear() {
    this.textarea.value = "";
    for (const pending of this.pendingFiles) {
      if (pending.previewUrl) {
        URL.revokeObjectURL(pending.previewUrl);
      }
    }
    this.pendingFiles = [];
    this.pendingAttachments = [];
    this.renderChips();
    this.resize();
  }

  // 切换任务时清掉上一个任务的状态，但保留用户正在打的字
  clearSessionState() {
    this.setRunning(false, "interject");
    show(this.contextMeter, false);
    this.setCompacting(false);
    for (const pending of this.pendingFiles) {
      if (pending.previewUrl) {
        URL.revokeObjectURL(pending.previewUrl);
      }
    }
    this.pendingFiles = [];
    this.pendingAttachments = [];
    this.renderChips();
  }

  restoreTexts(texts) {
    const restored = texts.filter(Boolean).join("\n");
    if (!restored) {
      return;
    }
    this.textarea.value = this.textarea.value ? `${this.textarea.value}\n${restored}` : restored;
    this.resize();
    this.focus();
  }

  // 「恢复到这里」：这句话的原话和附件放回输入框，改完直接重发
  restoreMessage(message) {
    if (!message) {
      return;
    }
    this.textarea.value = message.text || "";
    this.pendingAttachments = (message.attachments || []).map((attachment) => ({ ...attachment }));
    this.renderChips();
    this.resize();
    this.focus();
    const end = this.textarea.value.length;
    this.textarea.setSelectionRange(end, end);
  }

  focus() {
    this.textarea.focus();
  }

  insertText(text) {
    const start = this.textarea.selectionStart ?? this.textarea.value.length;
    const end = this.textarea.selectionEnd ?? start;
    const before = this.textarea.value.slice(0, start);
    const spacer = before && !/\s$/.test(before) ? " " : "";
    this.textarea.value = `${before}${spacer}${text}${this.textarea.value.slice(end)}`;
    const cursor = before.length + spacer.length + text.length;
    this.textarea.setSelectionRange(cursor, cursor);
    this.resize();
    this.focus();
  }

  // ---- 外部设置 ----

  setModes(modes) {
    this.permissionSelect.replaceChildren();
    for (const mode of modes) {
      const option = el("option", "", MODE_LABELS[mode] || mode);
      option.value = mode;
      this.permissionSelect.append(option);
    }
    this.paintPermission();
  }

  setMode(mode) {
    if (mode && [...this.permissionSelect.options].some((option) => option.value === mode)) {
      this.permissionSelect.value = mode;
    }
    this.paintPermission();
  }

  getMode() {
    return this.permissionSelect.value;
  }

  paintPermission() {
    const mode = this.permissionSelect.value;
    this.permissionIcon.replaceChildren(icon("shield"));
    this.permissionPill.classList.toggle("danger", mode === "full_auto");
    this.permissionPill.classList.toggle("accent", mode === "ai_auto");
    fitSelectToSelection(this.permissionSelect);
  }

  setModels(models, fallbackModel) {
    this.modelPicker.setModels(models, fallbackModel);
  }

  setModel(modelId) {
    this.modelPicker.setModel(modelId);
  }

  setDefaultModel(modelId) {
    this.modelPicker.setDefaultModel(modelId);
  }

  getModel() {
    return this.modelPicker.getModel();
  }

  setCompacting(compacting) {
    show(this.compactingHint, Boolean(compacting));
  }

  setWorkspaces(workspaces, selectedPath) {
    this.workspaceSelect.replaceChildren();
    for (const workspace of workspaces) {
      const option = el("option", "", workspace.name);
      option.value = workspace.path;
      option.title = workspace.path;
      option.disabled = !workspace.exists;
      this.workspaceSelect.append(option);
    }
    const scratch = el("option", "", SCRATCH_LABEL);
    scratch.value = SCRATCH_WORKSPACE;
    scratch.title = SCRATCH_TITLE;
    this.workspaceSelect.append(scratch);
    const wanted = selectedPath || this.workspaceSelect.value;
    const available = [...this.workspaceSelect.options].filter((option) => !option.disabled).map((option) => option.value);
    this.workspaceSelect.value = available.includes(wanted) ? wanted : available[0];
    fitSelectToSelection(this.workspaceSelect);
  }

  getWorkspaceChoice() {
    const value = this.workspaceSelect?.value || "";
    if (value === SCRATCH_WORKSPACE) {
      return { workspace: null, ephemeral: true };
    }
    return { workspace: value || null, ephemeral: false };
  }

  setWorkspaceLabel(path, ephemeral) {
    this.workspaceLabel.textContent = ephemeral ? "临时任务" : workspaceName(path);
    this.workspaceLabel.title = ephemeral ? `临时目录 ${path}` : path;
  }

  setRunning(running, busyInputMode) {
    this.running = running;
    show(this.stopButton, running);
    if (running) {
      this.busyHint.textContent =
        busyInputMode === "queue" ? "运行中 · 发的消息会排队" : "运行中 · 发的消息会插话";
      this.sendButtonLabel.textContent = busyInputMode === "queue" ? "排队" : "插话";
    } else {
      this.busyHint.textContent = "";
      this.sendButtonLabel.textContent = "发送";
    }
  }

  setContext(tokens, windowSize, compactThreshold) {
    if (!windowSize) {
      show(this.contextMeter, false);
      return;
    }
    const percent = Math.min(100, Math.round((tokens / windowSize) * 100));
    show(this.contextMeter, true);
    this.contextFill.style.width = `${percent}%`;
    const thresholdPercent = compactThreshold ? Math.round(compactThreshold * 100) : null;
    this.contextMeter.classList.toggle("warning", thresholdPercent !== null && percent >= thresholdPercent - 10);
    show(this.contextMarker, thresholdPercent !== null);
    if (thresholdPercent !== null) {
      this.contextMarker.style.left = `${thresholdPercent}%`;
    }
    this.contextText.textContent = `${percent}%`;
    const rule = thresholdPercent !== null ? `超过 ${thresholdPercent}% 在后台自动压缩` : "自动压缩已关闭";
    this.contextMeter.title = `上下文约 ${tokens} / ${windowSize} tokens，${rule}`;
  }
}

// 原生 select 的宽度永远等于最宽的 option，选中项短时右边会悬空一大段；
// 外观收成无边框文本后，按当前选中项的实际文本宽把 select 收窄。
function fitSelectToSelection(select) {
  const option = select.selectedOptions[0];
  if (!option) {
    return;
  }
  const style = getComputedStyle(select);
  const probe = document.createElement("span");
  probe.style.position = "absolute";
  probe.style.visibility = "hidden";
  probe.style.whiteSpace = "pre";
  probe.style.fontSize = style.fontSize;
  probe.style.fontFamily = style.fontFamily;
  probe.textContent = option.textContent;
  document.body.append(probe);
  const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  select.style.width = `${Math.ceil(probe.getBoundingClientRect().width) + padding + 4}px`;
  probe.remove();
}

function iconButton(iconName, title, className) {
  const node = el("button", className);
  node.type = "button";
  node.append(icon(iconName));
  node.title = title;
  node.setAttribute("aria-label", title);
  return node;
}
