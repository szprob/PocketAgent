// 自动化：按计划在工作区里开任务。列表页 + 新建/编辑弹窗。

import { request, sendJson } from "./api.js";
import { button, el, formatClock, formatRelativeTime, show, workspaceName } from "./dom.js";
import { state } from "./state.js";

const WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"];
const STATUS_LABELS = {
  running: "运行中",
  done: "完成",
  error: "出错",
  denied: "被拒绝",
  step_limit: "到上限",
  interrupted: "中断",
  truncated: "超长截断",
  filtered: "被审核中断",
  incomplete: "输出中断",
};

const elements = {};
let callbacks = {};
let automations = [];
let editingId = null;
let selectedWeekdays = new Set([0, 1, 2, 3, 4, 5, 6]);

export function initAutomations(handlers) {
  callbacks = handlers;
  elements.modal = document.getElementById("automation-modal");
  elements.title = document.getElementById("automation-modal-title");
  elements.list = document.getElementById("automation-list");
  elements.form = document.getElementById("automation-form");
  elements.message = document.getElementById("automation-message");
  elements.weekdayPicker = document.getElementById("weekday-picker");
  elements.dailyFields = document.getElementById("daily-fields");
  elements.intervalFields = document.getElementById("interval-fields");

  document.getElementById("new-automation").addEventListener("click", () => openEditor(null));
  document.getElementById("automation-cancel").addEventListener("click", closeEditor);
  document.getElementById("automation-save").addEventListener("click", saveAutomation);
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeEditor();
    }
  });
  for (const radio of elements.form.querySelectorAll('input[name="schedule_kind"]')) {
    radio.addEventListener("change", paintScheduleFields);
  }
  buildWeekdayChips();
}

export function isAutomationEditorOpen() {
  return elements.modal && !elements.modal.classList.contains("hidden");
}

export function closeEditor() {
  show(elements.modal, false);
}

export async function showAutomations() {
  elements.list.replaceChildren(el("div", "muted", "加载中…"));
  try {
    const result = await request("/api/automations");
    automations = result.automations || [];
  } catch (error) {
    elements.list.replaceChildren(el("div", "error-text", `读取自动化失败：${error.message}`));
    return;
  }
  render();
}

function render() {
  elements.list.replaceChildren();
  if (automations.length === 0) {
    elements.list.append(el("div", "panel-empty", "还没有自动化，点右上角新建一个"));
    return;
  }
  for (const automation of automations) {
    elements.list.append(renderAutomation(automation));
  }
}

function renderAutomation(automation) {
  const card = el("div", "automation-card");
  const head = el("div", "automation-head");
  head.append(el("span", "automation-name", automation.name));
  if (automation.last_status) {
    const status = el("span", `automation-status ${automation.last_status}`, STATUS_LABELS[automation.last_status] || automation.last_status);
    head.append(status);
  }
  if (!automation.enabled) {
    head.append(el("span", "automation-status", "已停用"));
  }
  head.append(el("div", "header-spacer"));
  card.append(head);

  card.append(el("div", "automation-prompt", automation.prompt));

  const meta = el("div", "automation-meta");
  meta.append(el("span", "", automation.schedule_text || ""));
  if (automation.enabled && automation.next_run_at) {
    meta.append(el("span", "", `下次：${formatClock(automation.next_run_at)}`));
  }
  if (automation.last_run_at) {
    meta.append(el("span", "", `上次：${formatRelativeTime(automation.last_run_at)}`));
  }
  meta.append(el("span", "", `工作区：${workspaceName(automation.workspace)}`));
  card.append(meta);

  const actions = el("div", "automation-actions");
  const run = button("ghost-button small", "立即运行");
  run.addEventListener("click", () => runNow(automation));
  const edit = button("ghost-button small", "编辑");
  edit.addEventListener("click", () => openEditor(automation));
  const toggle = button("ghost-button small", automation.enabled ? "停用" : "启用");
  toggle.addEventListener("click", () => toggleEnabled(automation));
  const remove = button("ghost-button small danger-text", "删除");
  remove.addEventListener("click", () => deleteAutomation(automation));
  actions.append(run, edit, toggle, remove);
  if (automation.last_session_id) {
    const open = button("ghost-button small", "打开上次任务");
    open.addEventListener("click", () => callbacks.onOpenTask?.(automation.last_session_id));
    actions.append(open);
  }
  card.append(actions);
  return card;
}

async function runNow(automation) {
  try {
    const result = await sendJson(`/api/automations/${encodeURIComponent(automation.id)}/run`, "POST", {});
    await showAutomations();
    callbacks.onOpenTask?.(result.session_id);
  } catch (error) {
    window.alert(`运行失败：${error.message}`);
  }
}

async function toggleEnabled(automation) {
  const payload = { ...automation, enabled: !automation.enabled };
  try {
    const result = await sendJson(`/api/automations/${encodeURIComponent(automation.id)}`, "PUT", payload);
    automations = result.automations || [];
    render();
  } catch (error) {
    window.alert(`保存失败：${error.message}`);
  }
}

async function deleteAutomation(automation) {
  if (!window.confirm(`删除自动化「${automation.name}」？`)) {
    return;
  }
  try {
    const result = await sendJson(`/api/automations/${encodeURIComponent(automation.id)}`, "DELETE");
    automations = result.automations || [];
    render();
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
  }
}

// ---- 编辑弹窗 ----

function buildWeekdayChips() {
  WEEKDAY_NAMES.forEach((name, day) => {
    const chip = el("button", "weekday-chip", `周${name}`);
    chip.type = "button";
    chip.dataset.day = String(day);
    chip.addEventListener("click", () => {
      if (selectedWeekdays.has(day)) {
        selectedWeekdays.delete(day);
      } else {
        selectedWeekdays.add(day);
      }
      paintWeekdays();
    });
    elements.weekdayPicker.append(chip);
  });
}

function paintWeekdays() {
  for (const chip of elements.weekdayPicker.querySelectorAll(".weekday-chip")) {
    chip.classList.toggle("active", selectedWeekdays.has(Number(chip.dataset.day)));
  }
}

function openEditor(automation) {
  editingId = automation?.id || null;
  elements.title.textContent = automation ? "编辑自动化" : "新建自动化";
  elements.message.textContent = "";
  elements.message.className = "settings-message";

  const info = state.info;
  const form = elements.form;
  form.elements.name.value = automation?.name || "";
  form.elements.prompt.value = automation?.prompt || "";

  form.elements.workspace.replaceChildren();
  for (const workspace of info?.workspaces || []) {
    const option = el("option", "", workspace.name);
    option.value = workspace.path;
    option.disabled = !workspace.exists;
    form.elements.workspace.append(option);
  }
  form.elements.workspace.value = automation?.workspace || info?.default_workspace || "";

  form.elements.model_id.replaceChildren();
  const defaultOption = el("option", "", "跟随默认");
  defaultOption.value = "";
  form.elements.model_id.append(defaultOption);
  for (const model of info?.models || []) {
    const option = el("option", "", model.name);
    option.value = model.id;
    form.elements.model_id.append(option);
  }
  form.elements.model_id.value = automation?.model_id || "";

  form.elements.approval_mode.replaceChildren();
  for (const mode of ["ai_auto", "full_auto"]) {
    const option = el("option", "", mode === "ai_auto" ? "AI 自动审批" : "完全访问");
    option.value = mode;
    form.elements.approval_mode.append(option);
  }
  form.elements.approval_mode.value = automation?.approval_mode || "ai_auto";

  const scheduleKind = automation?.schedule_kind || "daily";
  for (const radio of form.querySelectorAll('input[name="schedule_kind"]')) {
    radio.checked = radio.value === scheduleKind;
  }
  form.elements.daily_time.value = automation?.daily_time || "09:00";
  form.elements.interval_minutes.value = automation?.interval_minutes || 60;
  selectedWeekdays = new Set(automation?.weekdays || [0, 1, 2, 3, 4, 5, 6]);
  paintWeekdays();
  form.elements.enabled.checked = automation?.enabled ?? true;

  paintScheduleFields();
  show(elements.modal, true);
}

function paintScheduleFields() {
  const kind = elements.form.querySelector('input[name="schedule_kind"]:checked')?.value || "daily";
  show(elements.dailyFields, kind === "daily");
  show(elements.intervalFields, kind === "interval");
}

async function saveAutomation() {
  const form = elements.form;
  const kind = form.querySelector('input[name="schedule_kind"]:checked')?.value || "daily";
  const payload = {
    name: form.elements.name.value.trim(),
    prompt: form.elements.prompt.value.trim(),
    workspace: form.elements.workspace.value,
    model_id: form.elements.model_id.value || null,
    approval_mode: form.elements.approval_mode.value,
    schedule_kind: kind,
    daily_time: form.elements.daily_time.value || "09:00",
    weekdays: [...selectedWeekdays],
    interval_minutes: Number(form.elements.interval_minutes.value) || 60,
    enabled: form.elements.enabled.checked,
  };
  if (!payload.name || !payload.prompt) {
    elements.message.textContent = "名称和要做的事都不能为空";
    elements.message.className = "settings-message error";
    return;
  }
  elements.message.textContent = "保存中…";
  elements.message.className = "settings-message";
  try {
    const result = editingId
      ? await sendJson(`/api/automations/${encodeURIComponent(editingId)}`, "PUT", payload)
      : await sendJson("/api/automations", "POST", payload);
    automations = result.automations || [];
    closeEditor();
    render();
  } catch (error) {
    elements.message.textContent = `保存失败：${error.message}`;
    elements.message.className = "settings-message error";
  }
}
