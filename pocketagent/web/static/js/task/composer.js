// 输入框：「新任务」页和任务页各用一个。
// 文本框 + 附件（回形针、粘贴图片、拖进来）+ 权限 + 模型 + 发送 / 停止，底下一行是工作区和上下文占用。
// 以 / 开头输入时浮出命令补全（命令表来自 /api/info）。

import { button, el, formatBytes, icon, show, workspaceName } from "../ui/dom.js";
import { ModelPicker } from "./modelpicker.js";
import { MODE_ICONS, MODE_LABELS } from "../core/state.js";

const MAX_TEXTAREA_HEIGHT = 240;

// 工作区下拉里「不指定工作区」那一项的值。真实工作区都是绝对路径，不会撞上。
export const SCRATCH_WORKSPACE = "__scratch__";
const SCRATCH_LABEL = "临时任务（不指定工作区）";
const SCRATCH_TITLE = "在 ~/.pocketagent/scratch 下新建一个临时目录，不碰任何项目";

export class Composer {
  /**
   * kind: "new"（新任务页，底下能选工作区）或 "chat"（任务页，底下显示任务所在的工作区）
   * onSubmit({text, files}) 返回 true 表示已经发出去了，输入框可以清空
   */
  constructor(container, { kind, placeholder, onSubmit, onStop, onModeChange, onModelChange, onWorkspaceChange }) {
    this.kind = kind;
    this.onSubmit = onSubmit;
    this.onStop = onStop;
    this.onModeChange = onModeChange;
    this.onModelChange = onModelChange;
    this.onWorkspaceChange = onWorkspaceChange;
    this.pendingFiles = [];
    // 从对话里「恢复到这里」带回来的附件：服务端已经有了，重发时只传 id
    this.pendingAttachments = [];
    this.running = false;
    // /api/info 里的命令表 + 技能名，输入 / 时用来补全
    this.commands = [];
    this.skillNames = [];
    this.root = this._build(placeholder);
    container.replaceChildren(this.root);
  }

  // ---- 搭界面 ----

  _build(placeholder) {
    const root = el("div", "composer-wrapper");
    this.card = el("div", "composer");
    this.chips = el("div", "attachment-chips hidden");
    this.textarea = el("textarea", "composer-input");
    this.textarea.rows = 1;
    this.textarea.placeholder = placeholder;
    // 命令补全菜单：输入 / 时浮出来，点一条或按 Tab 补全
    this.commandMenu = el("div", "command-menu hidden");
    this.card.append(this.commandMenu, this.chips, this.textarea, this._buildToolbar());
    root.append(this.card, this._buildFooter());
    this._bindInput();
    return root;
  }

  _buildToolbar() {
    const toolbar = el("div", "composer-toolbar");
    this.fileInput = el("input");
    this.fileInput.type = "file";
    this.fileInput.multiple = true;
    this.fileInput.hidden = true;
    const attachButton = button("tool-button", "", "paperclip", "添加图片或文件（也可以直接粘贴、拖进来）");
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
      this._paintPermission();
      this.onModeChange?.(this.permissionSelect.value);
    });

    this.modelPicker = new ModelPicker({ onSelectModel: (modelId) => this.onModelChange?.(modelId) });

    this.stopButton = button("stop-button hidden", "", "stop", "停止（Esc）");
    this.stopButton.addEventListener("click", () => this.onStop?.());
    this.sendButton = button("send-button", "", "arrowUp", "发送（Enter）");
    this.sendButton.addEventListener("click", () => this.submit());

    toolbar.append(
      attachButton,
      this.fileInput,
      this.permissionPill,
      this.modelPicker.root,
      el("div", "toolbar-spacer"),
      this.stopButton,
      this.sendButton
    );
    return toolbar;
  }

  _buildFooter() {
    const footer = el("div", "composer-footer");
    const workspace = el("span", "footer-workspace");
    workspace.appendChild(icon("monitor"));
    if (this.kind === "new") {
      this.workspaceSelect = el("select", "workspace-select");
      this.workspaceSelect.title = "在哪个工作区里做";
      this.workspaceSelect.addEventListener("change", () => {
        this.onWorkspaceChange?.();
      });
      workspace.append(this.workspaceSelect);
    } else {
      this.workspaceLabel = el("span", "workspace-label");
      workspace.appendChild(this.workspaceLabel);
    }
    this.busyHint = el("span", "busy-hint");
    this.compactingHint = el("span", "compacting-hint hidden", "后台压缩上下文中…");
    this.contextMeter = el("span", "context-meter hidden");
    this.contextFill = el("span", "context-fill");
    // 自动压缩阈值的位置画一条竖线
    this.contextMarker = el("span", "context-marker hidden");
    const bar = el("span", "context-bar");
    bar.append(this.contextFill, this.contextMarker);
    this.contextText = el("span", "context-text");
    this.contextMeter.append(bar, this.contextText);
    footer.append(workspace, this.busyHint, el("span", "toolbar-spacer"), this.compactingHint, this.contextMeter);
    return footer;
  }

  _bindInput() {
    this.textarea.addEventListener("input", () => {
      this._resize();
      this._updateCommandMenu();
    });
    this.textarea.addEventListener("keydown", (event) => {
      // 命令菜单开着时：上下键选、Tab 补全、Esc 关掉；Enter 在还没打完命令名时补全，打完了就发送
      if (this._commandMenuOpen()) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          this._moveCommandSelection(event.key === "ArrowDown" ? 1 : -1);
          return;
        }
        if (event.key === "Tab") {
          event.preventDefault();
          this._acceptCommandSuggestion();
          return;
        }
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
          const selected = this.commandMenu.querySelectorAll(".command-item")[this.commandSelection];
          const name = selected?.querySelector(".command-name")?.textContent || "";
          if (this.textarea.value.trim() !== name) {
            event.preventDefault();
            this._fillCommand(name);
            return;
          }
          // 打的就是完整命令名：落下继续走发送
        }
        if (event.key === "Escape") {
          event.preventDefault();
          this._hideCommandMenu();
          return;
        }
      }
      // 中文输入法选词时按的 Enter 不算发送
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        this.submit();
      }
    });
    this.textarea.addEventListener("blur", () => {
      // 点菜单里的条目时 blur 会先到，留一拍让 click 生效
      setTimeout(() => this._hideCommandMenu(), 150);
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

  _resize() {
    this.textarea.style.height = "auto";
    this.textarea.style.height = `${Math.min(this.textarea.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }

  // ---- 命令补全 ----

  // 整行以 / 开头、后面还没打空格时，才弹命令菜单（打完参数就不挡视线了）
  _commandToken() {
    const value = this.textarea.value;
    if (!value.startsWith("/") || /\s/.test(value)) {
      return null;
    }
    return value.trim().toLowerCase();
  }

  _commandMenuOpen() {
    return !this.commandMenu.classList.contains("hidden");
  }

  _updateCommandMenu() {
    const token = this._commandToken();
    if (token === null) {
      this._hideCommandMenu();
      return;
    }
    const matches = [...this.commands, ...this.skillNames.map((name) => ({ name, summary: "技能" }))].filter(
      (item) => `/${item.name}`.startsWith(token)
    );
    if (matches.length === 0) {
      this._hideCommandMenu();
      return;
    }
    this.commandMenu.replaceChildren();
    matches.slice(0, 8).forEach((item, index) => {
      const row = el("button", "command-item" + (index === 0 ? " selected" : ""));
      row.type = "button";
      const name = el("span", "command-name", `/${item.name}`);
      const summary = el("span", "command-summary", item.summary || "");
      row.append(name, summary);
      row.addEventListener("mousedown", (event) => {
        // mousedown 抢在 textarea blur 之前，点了就补全
        event.preventDefault();
        this._fillCommand(`/${item.name}`);
      });
      this.commandMenu.appendChild(row);
    });
    this.commandSelection = 0;
    show(this.commandMenu, true);
  }

  _hideCommandMenu() {
    show(this.commandMenu, false);
    this.commandSelection = 0;
  }

  _moveCommandSelection(delta) {
    const items = [...this.commandMenu.querySelectorAll(".command-item")];
    if (items.length === 0) {
      return;
    }
    this.commandSelection = (this.commandSelection + delta + items.length) % items.length;
    items.forEach((item, index) => item.classList.toggle("selected", index === this.commandSelection));
  }

  _acceptCommandSuggestion() {
    const items = [...this.commandMenu.querySelectorAll(".command-item")];
    const selected = items[this.commandSelection];
    if (selected) {
      this._fillCommand(selected.querySelector(".command-name").textContent);
    }
  }

  _fillCommand(text) {
    this.textarea.value = `${text} `;
    this._hideCommandMenu();
    this._resize();
    this.focus();
  }

  setCommands(commands) {
    this.commands = commands || [];
    this._updateCommandMenu();
  }

  setSkillNames(names) {
    this.skillNames = names || [];
    this._updateCommandMenu();
  }

  // ---- 附件 ----

  addFiles(fileList) {
    for (const file of fileList) {
      const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
      this.pendingFiles.push({ file, previewUrl });
    }
    this._renderChips();
  }

  _removeFile(index) {
    const [removed] = this.pendingFiles.splice(index, 1);
    if (removed?.previewUrl) {
      URL.revokeObjectURL(removed.previewUrl);
    }
    this._renderChips();
  }

  _renderChips() {
    this.chips.replaceChildren();
    const total = this.pendingFiles.length + this.pendingAttachments.length;
    show(this.chips, total > 0);
    this.pendingFiles.forEach((pending, index) => {
      this.chips.appendChild(this._fileChip(pending, index));
    });
    this.pendingAttachments.forEach((attachment, index) => {
      this.chips.appendChild(this._attachmentChip(attachment, index));
    });
  }

  _fileChip(pending, index) {
    const chip = el("div", "attachment-chip");
    if (pending.previewUrl) {
      const image = el("img", "chip-thumbnail");
      image.src = pending.previewUrl;
      image.alt = pending.file.name;
      chip.appendChild(image);
    } else {
      chip.appendChild(icon("file", "chip-icon"));
    }
    const label = el("span", "chip-label");
    label.append(
      el("span", "chip-name", pending.file.name || "粘贴的图片"),
      el("span", "chip-size", formatBytes(pending.file.size))
    );
    const remove = button("chip-remove", "", "close", "移除");
    remove.addEventListener("click", () => this._removeFile(index));
    chip.append(label, remove);
    return chip;
  }

  _attachmentChip(attachment, index) {
    const chip = el("div", "attachment-chip");
    chip.appendChild(icon(attachment.kind === "image" ? "image" : "file", "chip-icon"));
    const label = el("span", "chip-label");
    label.append(
      el("span", "chip-name", attachment.name || "附件"),
      el("span", "chip-size", formatBytes(attachment.size || 0))
    );
    const remove = button("chip-remove", "", "close", "移除");
    remove.addEventListener("click", () => this._removeAttachment(index));
    chip.append(label, remove);
    return chip;
  }

  _removeAttachment(index) {
    this.pendingAttachments.splice(index, 1);
    this._renderChips();
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
    this._renderChips();
    this._resize();
  }

  // 中断时服务端退回的插话 / 排队消息，放回输入框
  restoreTexts(texts) {
    const restored = texts.filter((text) => text).join("\n");
    if (!restored) {
      return;
    }
    this.textarea.value = this.textarea.value ? `${this.textarea.value}\n${restored}` : restored;
    this._resize();
    this.focus();
  }

  // 「恢复到这里」：这句话的原话和附件放回输入框，改完直接重发
  restoreMessage(message) {
    if (!message) {
      return;
    }
    this.textarea.value = message.text || "";
    this.pendingAttachments = (message.attachments || []).map((attachment) => ({ ...attachment }));
    this._renderChips();
    this._resize();
    this.focus();
    const end = this.textarea.value.length;
    this.textarea.setSelectionRange(end, end);
  }

  focus() {
    this.textarea.focus();
  }

  // 在光标处插入一段文字（比如左侧文件树里「在对话中引用」的 @路径）
  insertText(text) {
    const start = this.textarea.selectionStart ?? this.textarea.value.length;
    const end = this.textarea.selectionEnd ?? start;
    const before = this.textarea.value.slice(0, start);
    const spacer = before && !/\s$/.test(before) ? " " : "";
    this.textarea.value = `${before}${spacer}${text}${this.textarea.value.slice(end)}`;
    const cursor = before.length + spacer.length + text.length;
    this.textarea.setSelectionRange(cursor, cursor);
    this._resize();
    this.focus();
  }

  // ---- 外部设置 ----

  setModes(modes) {
    this.permissionSelect.replaceChildren();
    for (const mode of modes) {
      const option = el("option", "", MODE_LABELS[mode] || mode);
      option.value = mode;
      this.permissionSelect.appendChild(option);
    }
  }

  setMode(mode) {
    if (mode && [...this.permissionSelect.options].some((option) => option.value === mode)) {
      this.permissionSelect.value = mode;
    }
    this._paintPermission();
  }

  getMode() {
    return this.permissionSelect.value;
  }

  _paintPermission() {
    const mode = this.permissionSelect.value;
    this.permissionIcon.replaceChildren(icon(MODE_ICONS[mode] || "shield"));
    this.permissionPill.classList.toggle("danger", mode === "full_auto");
    this.permissionPill.classList.toggle("accent", mode === "ai_auto");
  }

  setModels(models, fallbackModel) {
    this.modelPicker.setModels(models, fallbackModel);
  }

  setModel(modelId) {
    this.modelPicker.setModel(modelId);
  }

  // 新任务页：跟着配置里的默认模型走；用户自己挑过模型就不动
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
      this.workspaceSelect.appendChild(option);
    }
    const scratch = el("option", "", SCRATCH_LABEL);
    scratch.value = SCRATCH_WORKSPACE;
    scratch.title = SCRATCH_TITLE;
    this.workspaceSelect.appendChild(scratch);
    // 没指定就保持原来选的（比如用户刚选了「临时任务」，配置刷新不该把它弹回去）
    const wanted = selectedPath || this.workspaceSelect.value;
    const available = [...this.workspaceSelect.options].filter((option) => !option.disabled).map((option) => option.value);
    this.workspaceSelect.value = available.includes(wanted) ? wanted : available[0];
  }

  // 新任务页要的：选了哪个工作区，还是「临时任务」
  getWorkspaceChoice() {
    const value = this.workspaceSelect?.value || "";
    if (value === SCRATCH_WORKSPACE) {
      return { workspace: null, ephemeral: true };
    }
    return { workspace: value || null, ephemeral: false };
  }

  setWorkspaceLabel(path, ephemeral, worktree = null) {
    this.workspaceLabel.textContent = ephemeral ? "临时任务" : worktree ? "独立 worktree" : workspaceName(path);
    this.workspaceLabel.title = ephemeral
      ? `临时目录 ${path}`
      : worktree
        ? `${worktree.branch}\n来源 ${worktree.origin}\n目录 ${path}`
        : path;
  }

  setRunning(running, busyInputMode) {
    this.running = running;
    show(this.stopButton, running);
    if (running) {
      this.busyHint.textContent =
        busyInputMode === "queue" ? "运行中 · 发的消息会排队，这一轮结束后执行" : "运行中 · 发的消息会插话，模型下一步就能看到";
    } else {
      this.busyHint.textContent = "";
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
