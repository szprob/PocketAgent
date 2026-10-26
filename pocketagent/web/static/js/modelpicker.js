// 选模型的弹出面板：选模型，顺手设思考档位和上下文长度（存进配置，用这个模型的任务马上换上）。
// 思考档位只列这个模型实测能用的。

import { sendJson } from "./api.js";
import { button, el, formatTokens, icon } from "./dom.js";
import { reloadInfo, state } from "./state.js";

export const REASONING_LABELS = {
  none: "关",
  minimal: "极低",
  low: "低",
  medium: "中",
  high: "高",
  max: "最高",
  xhigh: "超高",
};

const CONTEXT_PRESETS = [32_000, 64_000, 128_000, 256_000, 512_000, 1_000_000];

export function reasoningLabel(effort) {
  return effort ? REASONING_LABELS[effort] || effort : "默认";
}

export class ModelPicker {
  constructor({ onSelectModel }) {
    this.onSelectModel = onSelectModel;
    this.models = [];
    this.fallbackModel = "";
    this.selectedId = null;
    this.root = el("div", "model-picker");
    this.trigger = button("pill model-trigger", "", "model", "模型、思考档位、上下文长度");
    this.triggerLabel = el("span", "pill-label");
    this.trigger.append(this.triggerLabel, icon("chevronDown", "pill-chevron"));
    this.popover = el("div", "picker-popover hidden");
    this.root.append(this.trigger, this.popover);
    this.trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      this.toggle();
    });
    this.popover.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", () => this.close());
    window.addEventListener("resize", () => {
      if (this.isOpen()) {
        this._place();
      }
    });
  }

  // ---- 对外 ----

  setModels(models, fallbackModel) {
    this.models = models;
    this.fallbackModel = fallbackModel;
    if (!models.some((model) => model.id === this.selectedId)) {
      this.selectedId = (models.find((model) => model.visible !== false) || models[0])?.id || null;
    }
    this._renderTrigger();
    if (this.isOpen()) {
      this._renderPopover();
      this._place();
    }
  }

  setModel(modelId) {
    if (modelId && this.models.some((model) => model.id === modelId)) {
      this.selectedId = modelId;
      this._renderTrigger();
    }
  }

  getModel() {
    return this.selectedId;
  }

  isOpen() {
    return !this.popover.classList.contains("hidden");
  }

  close() {
    this.popover.classList.add("hidden");
  }

  toggle() {
    if (this.isOpen()) {
      this.close();
      return;
    }
    this._renderPopover();
    this.popover.classList.remove("hidden");
    this._place();
  }

  // 往上还是往下弹、最多多高：看触发按钮上下哪边空间大，整个面板都留在窗口里
  _place() {
    if (window.matchMedia("(max-width: 860px)").matches) {
      // 窄屏是固定在底部的样式，由 CSS 管
      this.popover.style.maxHeight = "";
      return;
    }
    const margin = 12;
    const gap = 10;
    const rect = this.trigger.getBoundingClientRect();
    const spaceAbove = rect.top - gap - margin;
    const spaceBelow = window.innerHeight - rect.bottom - gap - margin;
    const natural = Math.min(560, this.popover.scrollHeight);
    const opensDown = spaceAbove < natural && spaceBelow > spaceAbove;
    this.popover.classList.toggle("opens-down", opensDown);
    this.popover.style.maxHeight = `${Math.max(160, Math.min(560, opensDown ? spaceBelow : spaceAbove))}px`;
  }

  // ---- 渲染 ----

  _selected() {
    return this.models.find((model) => model.id === this.selectedId) || null;
  }

  _renderTrigger() {
    const model = this._selected();
    if (!model) {
      this.triggerLabel.textContent = `${this.fallbackModel}（环境变量）`;
      this.trigger.disabled = true;
      return;
    }
    this.trigger.disabled = false;
    this.triggerLabel.textContent = `${model.name} · ${reasoningLabel(model.reasoning_effort)}`;
    this.trigger.title = `${model.model}\n思考：${reasoningLabel(model.reasoning_effort)}\n上下文：${formatTokens(model.context_window_tokens)}`;
  }

  _renderPopover() {
    this.popover.replaceChildren(this._modelSection());
    const model = this._selected();
    if (model) {
      this.popover.append(this._reasoningSection(model), this._contextSection(model), this._compactHint());
    }
  }

  _modelSection() {
    const section = el("div", "picker-section picker-models");
    section.appendChild(el("div", "picker-title", "模型"));
    // 设置里勾掉「在会话里显示」的不列；正在用的那个照样列出来
    const listed = this.models.filter((model) => model.visible !== false || model.id === this.selectedId);
    for (const model of listed) {
      const item = el("button", "picker-model");
      item.type = "button";
      item.classList.toggle("active", model.id === this.selectedId);
      const name = el("span", "picker-model-name", model.name);
      const meta = el("span", "picker-model-meta", model.model);
      const text = el("span", "picker-model-text");
      text.append(name, meta);
      item.append(icon(model.id === this.selectedId ? "check" : "model", "picker-check"), text);
      if (model.supports_images) {
        item.appendChild(el("span", "vision-badge", "图"));
      }
      item.addEventListener("click", () => {
        this.selectedId = model.id;
        this._renderTrigger();
        this._renderPopover();
        this.onSelectModel?.(model.id);
      });
      section.appendChild(item);
    }
    return section;
  }

  _segment(options, currentValue, onChoose) {
    const group = el("div", "segmented");
    for (const option of options) {
      const choice = button("segment", option.label);
      choice.classList.toggle("active", option.value === currentValue);
      choice.addEventListener("click", () => onChoose(option.value));
      group.appendChild(choice);
    }
    return group;
  }

  _reasoningSection(model) {
    const section = el("div", "picker-section");
    section.appendChild(el("div", "picker-title", "思考档位"));
    const options = [{ value: null, label: "默认" }].concat(
      model.reasoning_choices.map((choice) => ({ value: choice, label: reasoningLabel(choice) }))
    );
    section.appendChild(
      this._segment(options, model.reasoning_effort || null, (value) => this._save(model, { reasoning_effort: value }))
    );
    const note = model.reasoning_choices.includes("none") ? "「关」不思考，最快；越高想得越久" : "这个模型不能关闭思考";
    section.appendChild(el("div", "picker-note", `默认 = 不传，用接口自己的默认。${note}`));
    return section;
  }

  _contextSection(model) {
    const section = el("div", "picker-section");
    section.appendChild(el("div", "picker-title", "上下文长度"));
    const values = CONTEXT_PRESETS.includes(model.context_window_tokens)
      ? CONTEXT_PRESETS
      : [...CONTEXT_PRESETS, model.context_window_tokens].sort((first, second) => first - second);
    const options = values.map((value) => ({ value, label: formatTokens(value) }));
    section.appendChild(
      this._segment(options, model.context_window_tokens, (value) => this._save(model, { context_window_tokens: value }))
    );
    return section;
  }

  _compactHint() {
    const info = state.info;
    const text = info.auto_compact
      ? `上下文超过 ${info.compact_threshold_percent}% 时在后台自动压缩，不打断对话`
      : "自动压缩已关闭（设置 → 通用）";
    return el("div", "picker-footer", text);
  }

  async _save(model, options) {
    try {
      await sendJson(`api/models/${encodeURIComponent(model.id)}`, "PATCH", options);
    } catch (error) {
      window.alert(`保存失败：${error.message}`);
      return;
    }
    // 重新拉模型信息，所有输入框的选择器一起更新
    await reloadInfo();
  }
}
