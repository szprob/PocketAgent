// 输入框：「新任务」页和任务页各用一个。
// 文本框 + 附件（回形针、粘贴图片、拖进来）+ 权限 + 模型 + 发送 / 停止，底下一行是工作区和上下文占用。

import { button, el, formatBytes, icon, show, workspaceName } from "./dom.js";
import { ModelPicker } from "./modelpicker.js";
import { MODE_ICONS, MODE_LABELS } from "./state.js";

const MAX_TEXTAREA_HEIGHT = 240;

export class Composer {
  /**
   * kind: "new"（新任务页，底下能选工作区）或 "chat"（任务页，底下显示任务所在的工作区）
   * onSubmit({text, files}) 返回 true 表示已经发出去了，输入框可以清空
   */
  constructor(container, { kind, placeholder, onSubmit, onStop, onModeChange, onModelChange }) {
    this.kind = kind;
    this.onSubmit = onSubmit;
    this.onStop = onStop;
    this.onModeChange = onModeChange;
    this.onModelChange = onModelChange;
    this.pendingFiles = [];
    this.running = false;
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
    this.card.append(this.chips, this.textarea, this._buildToolbar());
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
      workspace.appendChild(this.workspaceSelect);
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
    this.textarea.addEventListener("input", () => this._resize());
    this.textarea.addEventListener("keydown", (event) => {
      // 中文输入法选词时按的 Enter 不算发送
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        this.submit();
      }
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
    show(this.chips, this.pendingFiles.length > 0);
    this.pendingFiles.forEach((pending, index) => {
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
      label.append(el("span", "chip-name", pending.file.name || "粘贴的图片"), el("span", "chip-size", formatBytes(pending.file.size)));
      const remove = button("chip-remove", "", "close", "移除");
      remove.addEventListener("click", () => this._removeFile(index));
      chip.append(label, remove);
      this.chips.appendChild(chip);
    });
  }

  // ---- 发送 ----

  async submit() {
    const text = this.textarea.value.trim();
    if (!text && this.pendingFiles.length === 0) {
      return;
    }
    const files = this.pendingFiles.map((pending) => pending.file);
    const sent = await this.onSubmit({ text, files });
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
    if (selectedPath) {
      this.workspaceSelect.value = selectedPath;
    }
  }

  getWorkspace() {
    return this.workspaceSelect?.value || null;
  }

  setWorkspaceLabel(path) {
    this.workspaceLabel.textContent = workspaceName(path);
    this.workspaceLabel.title = path;
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
