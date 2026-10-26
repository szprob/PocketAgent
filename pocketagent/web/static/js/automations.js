// 自动化页：定时在某个工作区里开任务跑一段提示词。列表 + 编辑弹窗。

import { requestJson, sendJson } from "./api.js";
import { button, el, formatClock, icon, show, workspaceName } from "./dom.js";
import { MODE_LABELS, findModel, state } from "./state.js";

const WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"];
const STATUS_LABELS = {
  running: "运行中",
  done: "完成",
  error: "出错",
  denied: "有操作被拒绝",
  step_limit: "达到步数上限",
  interrupted: "被中断",
};

const elements = {};
let callbacks = {};
// 正在编辑的自动化（新建时为 null）
let editing = null;

export function initAutomations(handlers) {
  callbacks = handlers;
  const byId = (id) => document.getElementById(id);
  Object.assign(elements, {
    list: byId("automation-list"),
    modal: byId("automation-modal"),
    title: byId("automation-modal-title"),
    form: byId("automation-form"),
    message: byId("automation-message"),
    weekdays: byId("weekday-picker"),
    dailyFields: byId("daily-fields"),
    intervalFields: byId("interval-fields"),
  });
  byId("new-automation").addEventListener("click", () => openEditor(null));
  byId("automation-cancel").addEventListener("click", closeEditor);
  byId("automation-save").addEventListener("click", saveAutomation);
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeEditor();
    }
  });
  for (const radio of elements.form.querySelectorAll('input[name="schedule_kind"]')) {
    radio.addEventListener("change", syncScheduleFields);
  }
  WEEKDAY_NAMES.forEach((name, index) => {
    const toggle = button("weekday-toggle", name);
    toggle.dataset.day = String(index);
    toggle.addEventListener("click", () => toggle.classList.toggle("active"));
    elements.weekdays.appendChild(toggle);
  });
}

export function isAutomationEditorOpen() {
  return !elements.modal.classList.contains("hidden");
}

export function closeEditor() {
  show(elements.modal, false);
}

export async function showAutomations() {
  elements.list.replaceChildren(el("div", "muted", "加载中…"));
  try {
    const { automations } = await requestJson("api/automations");
    renderList(automations);
  } catch (error) {
    elements.list.replaceChildren(el("div", "error-text", error.message));
  }
}

// ---- 列表 ----

function renderList(automations) {
  elements.list.replaceChildren();
  if (automations.length === 0) {
    const empty = el("div", "automation-empty");
    empty.append(icon("clock"), el("div", "", "还没有自动化。比如每天早上让它看看仓库里有什么需要处理的，或者每隔一小时跑一遍测试。"));
    elements.list.appendChild(empty);
    return;
  }
  for (const automation of automations) {
    elements.list.appendChild(renderCard(automation));
  }
}

function metaItem(label, value) {
  const item = el("span", "automation-meta-item");
  item.append(el("span", "meta-label", label), el("span", "", value));
  return item;
}

function renderCard(automation) {
  const card = el("div", "automation-card");
  card.classList.toggle("disabled", !automation.enabled);

  const header = el("div", "automation-card-header");
  const enabled = el("input");
  enabled.type = "checkbox";
  enabled.checked = automation.enabled;
  enabled.title = automation.enabled ? "已启用，点一下停用" : "已停用，点一下启用";
  enabled.addEventListener("change", () => saveForm({ ...automation, enabled: enabled.checked }, automation.id));
  const switchLabel = el("label", "switch");
  switchLabel.append(enabled, el("span", "switch-track"));
  header.append(el("span", "automation-name", automation.name), el("span", "schedule-pill", automation.schedule_text), el("span", "toolbar-spacer"), switchLabel);

  const prompt = el("div", "automation-prompt", automation.prompt);

  const meta = el("div", "automation-meta");
  const model = findModel(automation.model_id);
  meta.append(
    metaItem("工作区", workspaceName(automation.workspace)),
    metaItem("模型", model ? model.name : "默认模型"),
    metaItem("权限", MODE_LABELS[automation.approval_mode] || automation.approval_mode),
    metaItem("下次", automation.next_run_at ? formatClock(automation.next_run_at) : "已停用")
  );
  if (automation.last_run_at) {
    const status = STATUS_LABELS[automation.last_status] || automation.last_status || "";
    const last = metaItem("上次", `${formatClock(automation.last_run_at)} ${status}`);
    if (automation.last_session_id) {
      const open = button("link-button", "查看任务");
      open.addEventListener("click", () => callbacks.onOpenTask(automation.last_session_id));
      last.appendChild(open);
    }
    meta.appendChild(last);
  }

  const actions = el("div", "automation-actions");
  const run = button("ghost-button small", "立即运行", "play");
  run.addEventListener("click", () => runNow(automation, run));
  const edit = button("ghost-button small", "编辑", "pencil");
  edit.addEventListener("click", () => openEditor(automation));
  const remove = button("ghost-button small danger-text", "删除", "trash");
  remove.addEventListener("click", () => deleteAutomation(automation));
  actions.append(run, edit, remove);

  card.append(header, prompt, meta, actions);
  return card;
}

async function runNow(automation, runButton) {
  runButton.disabled = true;
  try {
    const { session_id: sessionId } = await sendJson(`api/automations/${automation.id}/run`, "POST", {});
    callbacks.onOpenTask(sessionId);
  } catch (error) {
    window.alert(`运行失败：${error.message}`);
    runButton.disabled = false;
  }
}

async function deleteAutomation(automation) {
  if (!window.confirm(`删除自动化「${automation.name}」？已经跑出来的任务不删。`)) {
    return;
  }
  const { automations } = await requestJson(`api/automations/${automation.id}`, { method: "DELETE" });
  renderList(automations);
}

// ---- 编辑弹窗 ----

function fillSelect(select, options, selectedValue) {
  select.replaceChildren();
  for (const option of options) {
    const element = el("option", "", option.label);
    element.value = option.value;
    select.appendChild(element);
  }
  select.value = selectedValue;
}

function openEditor(automation) {
  editing = automation;
  const form = elements.form;
  elements.title.textContent = automation ? "编辑自动化" : "新建自动化";
  elements.message.textContent = "";
  form.elements.name.value = automation?.name || "";
  form.elements.prompt.value = automation?.prompt || "";
  const workspaceOptions = state.info.workspaces.map((workspace) => ({ value: workspace.path, label: workspace.name }));
  fillSelect(form.elements.workspace, workspaceOptions, automation?.workspace || state.info.default_workspace);
  const modelOptions = [{ value: "", label: "默认模型" }].concat(
    state.info.models
      .filter((model) => model.visible !== false || model.id === automation?.model_id)
      .map((model) => ({ value: model.id, label: model.name }))
  );
  fillSelect(form.elements.model_id, modelOptions, automation?.model_id || "");
  const modeOptions = ["ai_auto", "full_auto"].map((mode) => ({ value: mode, label: MODE_LABELS[mode] }));
  fillSelect(form.elements.approval_mode, modeOptions, automation?.approval_mode || "ai_auto");
  const kind = automation?.schedule_kind || "daily";
  form.querySelector(`input[name="schedule_kind"][value="${kind}"]`).checked = true;
  form.elements.daily_time.value = automation?.daily_time || "09:00";
  form.elements.interval_minutes.value = automation?.interval_minutes || 60;
  const weekdays = automation?.weekdays || [0, 1, 2, 3, 4, 5, 6];
  for (const toggle of elements.weekdays.children) {
    toggle.classList.toggle("active", weekdays.includes(Number(toggle.dataset.day)));
  }
  form.elements.enabled.checked = automation ? automation.enabled : true;
  syncScheduleFields();
  show(elements.modal, true);
  form.elements.name.focus();
}

function syncScheduleFields() {
  const kind = elements.form.querySelector('input[name="schedule_kind"]:checked').value;
  show(elements.dailyFields, kind === "daily");
  show(elements.intervalFields, kind === "interval");
}

function readForm() {
  const form = elements.form;
  return {
    name: form.elements.name.value,
    prompt: form.elements.prompt.value,
    workspace: form.elements.workspace.value,
    model_id: form.elements.model_id.value,
    approval_mode: form.elements.approval_mode.value,
    schedule_kind: form.querySelector('input[name="schedule_kind"]:checked').value,
    daily_time: form.elements.daily_time.value,
    interval_minutes: form.elements.interval_minutes.value,
    weekdays: [...elements.weekdays.children].filter((toggle) => toggle.classList.contains("active")).map((toggle) => Number(toggle.dataset.day)),
    enabled: form.elements.enabled.checked,
  };
}

async function saveAutomation() {
  const saved = await saveForm(readForm(), editing?.id || null);
  if (saved) {
    closeEditor();
  }
}

async function saveForm(form, automationId) {
  try {
    const path = automationId ? `api/automations/${automationId}` : "api/automations";
    const { automations } = await sendJson(path, automationId ? "PUT" : "POST", form);
    renderList(automations);
    return true;
  } catch (error) {
    if (isAutomationEditorOpen()) {
      elements.message.textContent = error.message;
    } else {
      window.alert(error.message);
      showAutomations();
    }
    return false;
  }
}
