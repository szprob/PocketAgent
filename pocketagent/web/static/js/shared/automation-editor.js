// 自动化的新建 / 编辑弹窗：名称、要做的事、工作区、模型、权限，加上触发器（什么时候跑、满足什么条件才跑）。
// web 和桌面端共用；列表长什么样各端自己画。表单值和后端 JSON 之间怎么转在 trigger-form.js。
//
// 弹窗的元素 id（automation-modal、automation-form、clock-fields……）和表单控件的 name
// 两个 index.html 里要一致。

import { button, el, show } from "./dom.js";
import { MODE_LABELS } from "./labels.js";
import { sendJson } from "./platform.js";
import { emptyTrigger, formValuesOf, triggerFromForm, visibleFields } from "./trigger-form.js";

const WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"];
// 没人在场时只给这两种权限可选：每次询问会一直卡在审批上
const UNATTENDED_MODES = ["ai_auto", "full_auto"];

const elements = {};
let options = {};
// 正在编辑的自动化（新建时为 null）
let editing = null;
// 「等哪个自动化跑完」的候选，打开弹窗时由列表页传进来
let knownAutomations = [];

// options.getInfo()：当前的 /api/info（工作区、模型、默认工作区）
// options.hookUrl(automationId)：外部 POST 进来的完整地址（web 和桌面端的后端地址拿法不同）
// options.onSaved({ automations, problem })：保存成功后后端返回的新列表，交给列表页重画
export function initAutomationEditor(editorOptions) {
  options = editorOptions;
  const byId = (id) => document.getElementById(id);
  Object.assign(elements, {
    modal: byId("automation-modal"),
    title: byId("automation-modal-title"),
    form: byId("automation-form"),
    message: byId("automation-message"),
    weekdays: byId("weekday-picker"),
    clockFields: byId("clock-fields"),
    dailyFields: byId("daily-fields"),
    intervalFields: byId("interval-fields"),
    watchField: byId("watch-field"),
    pollFields: byId("poll-fields"),
    pollIntervalField: byId("poll-interval-field"),
    webhookFields: byId("webhook-fields"),
    conditionPatternField: byId("condition-pattern-field"),
    conditionShellField: byId("condition-shell-field"),
  });
  elements.webhookFields.style.whiteSpace = "pre-wrap";
  byId("automation-cancel").addEventListener("click", closeAutomationEditor);
  byId("automation-save").addEventListener("click", saveEditor);
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeAutomationEditor();
    }
  });
  elements.form.elements.trigger_kind.addEventListener("change", syncTriggerFields);
  elements.form.elements.condition_kind.addEventListener("change", syncTriggerFields);
  for (const radio of elements.form.querySelectorAll('input[name="clock_mode"]')) {
    radio.addEventListener("change", syncTriggerFields);
  }
  WEEKDAY_NAMES.forEach((name, index) => {
    const toggle = button("weekday-toggle", name);
    toggle.dataset.day = String(index);
    toggle.addEventListener("click", () => toggle.classList.toggle("active"));
    elements.weekdays.append(toggle);
  });
}

export function isAutomationEditorOpen() {
  return Boolean(elements.modal) && !elements.modal.classList.contains("hidden");
}

export function closeAutomationEditor() {
  show(elements.modal, false);
}

// automation 为 null 是新建；automations 是当前列表，用来出「等哪个自动化」的选项
export function openAutomationEditor(automation, automations = []) {
  editing = automation;
  knownAutomations = automations;
  const form = elements.form;
  const info = options.getInfo();
  const trigger = automation?.trigger || emptyTrigger();
  const values = formValuesOf(trigger);

  elements.title.textContent = automation ? "编辑自动化" : "新建自动化";
  elements.message.textContent = "";
  form.elements.name.value = automation?.name || "";
  form.elements.prompt.value = automation?.prompt || "";
  const workspaceOptions = (info?.workspaces || []).map((workspace) => ({ value: workspace.path, label: workspace.name }));
  fillSelect(form.elements.workspace, workspaceOptions, automation?.workspace || info?.default_workspace || "");
  const modelOptions = [{ value: "", label: "默认模型" }].concat(
    (info?.models || [])
      .filter((model) => model.visible !== false || model.id === automation?.model_id)
      .map((model) => ({ value: model.id, label: model.name })),
  );
  fillSelect(form.elements.model_id, modelOptions, automation?.model_id || "");
  const modeOptions = UNATTENDED_MODES.map((mode) => ({ value: mode, label: MODE_LABELS[mode] }));
  fillSelect(form.elements.approval_mode, modeOptions, automation?.approval_mode || "ai_auto");

  form.elements.trigger_kind.value = values.triggerKind;
  form.querySelector(`input[name="clock_mode"][value="${values.clockMode}"]`).checked = true;
  form.elements.daily_time.value = values.dailyTime;
  form.elements.interval_minutes.value = values.clockIntervalMinutes;
  form.elements.poll_interval_minutes.value = values.pollIntervalMinutes;
  form.elements.poll_command.value = values.pollCommand;
  for (const toggle of elements.weekdays.children) {
    toggle.classList.toggle("active", values.weekdays.includes(Number(toggle.dataset.day)));
  }
  fillWatchOptions(automation?.id, values.watchAutomation);

  form.elements.condition_kind.value = values.conditionKind;
  form.elements.condition_pattern.value = values.conditionPattern;
  form.elements.condition_command.value = values.conditionCommand;
  elements.webhookFields.textContent = webhookHint(automation, trigger.source || {});

  form.elements.enabled.checked = automation ? automation.enabled : true;
  syncTriggerFields();
  show(elements.modal, true);
  form.elements.name.focus();
}

// 列表上的启用开关只改这一项：用自动化现有的字段原样存回去，别把别的冲掉
export function automationFields(automation) {
  return {
    name: automation.name,
    prompt: automation.prompt,
    workspace: automation.workspace,
    model_id: automation.model_id || "",
    approval_mode: automation.approval_mode,
    trigger: automation.trigger,
    enabled: automation.enabled,
  };
}

// 新建（automationId 为 null）或更新；成功后把新列表交给 onSaved。出错时弹窗开着就写在弹窗里，否则抛出去
export async function saveAutomation(fields, automationId) {
  const path = automationId ? `/api/automations/${encodeURIComponent(automationId)}` : "/api/automations";
  try {
    const result = await sendJson(path, automationId ? "PUT" : "POST", fields);
    options.onSaved?.(result);
    return true;
  } catch (error) {
    if (!isAutomationEditorOpen()) {
      throw error;
    }
    elements.message.textContent = error.message;
    return false;
  }
}

function fillSelect(select, choices, selectedValue) {
  select.replaceChildren();
  for (const choice of choices) {
    const option = el("option", "", choice.label);
    option.value = choice.value;
    select.append(option);
  }
  select.value = selectedValue;
}

function webhookHint(automation, source) {
  if (!automation?.id || source.kind !== "webhook" || !source.secret) {
    return "保存之后这里会显示地址和签名密钥：外面用 POST 打过来，正文随便写，签名对了才会开任务。";
  }
  return [
    `地址：POST ${options.hookUrl(automation.id)}`,
    "签名：X-PocketAgent-Signature: sha256=<HMAC-SHA256(正文, 密钥)>",
    "去重：X-PocketAgent-Delivery: <每次调用一个唯一 id>，同一个 id 只会跑一次",
    `密钥：${source.secret}`,
  ].join("\n");
}

function fillWatchOptions(currentAutomationId, selectedValue) {
  const choices = [{ value: "", label: "任何自动化" }].concat(
    knownAutomations
      .filter((automation) => automation.id !== currentAutomationId)
      .map((automation) => ({ value: automation.id, label: automation.name })),
  );
  fillSelect(elements.form.elements.watch_automation, choices, selectedValue);
}

function syncTriggerFields() {
  const form = elements.form;
  const visible = visibleFields(form.elements.trigger_kind.value);
  const clockMode = form.querySelector('input[name="clock_mode"]:checked')?.value || "daily";
  show(elements.clockFields, visible.clock);
  show(elements.dailyFields, visible.clock && clockMode === "daily");
  show(elements.intervalFields, visible.clock && clockMode === "interval");
  show(elements.watchField, visible.watch);
  show(elements.pollFields, visible.poll);
  show(elements.pollIntervalField, visible.poll);
  show(elements.webhookFields, visible.webhook);

  const conditionKind = form.elements.condition_kind.value;
  show(elements.conditionPatternField, conditionKind === "match");
  show(elements.conditionShellField, conditionKind === "shell");
}

function activeWeekdays() {
  return [...elements.weekdays.children]
    .filter((toggle) => toggle.classList.contains("active"))
    .map((toggle) => Number(toggle.dataset.day));
}

// 控件里的值先收成一个扁平对象，怎么变成后端 JSON 由 trigger-form.js 决定
function currentFormValues() {
  const form = elements.form;
  return {
    triggerKind: form.elements.trigger_kind.value,
    clockMode: form.querySelector('input[name="clock_mode"]:checked').value,
    dailyTime: form.elements.daily_time.value,
    clockIntervalMinutes: form.elements.interval_minutes.value,
    weekdays: activeWeekdays(),
    watchAutomation: form.elements.watch_automation.value,
    pollIntervalMinutes: form.elements.poll_interval_minutes.value,
    pollCommand: form.elements.poll_command.value,
    conditionKind: form.elements.condition_kind.value,
    conditionPattern: form.elements.condition_pattern.value,
    conditionCommand: form.elements.condition_command.value,
  };
}

function readEditor() {
  const form = elements.form;
  return {
    name: form.elements.name.value,
    prompt: form.elements.prompt.value,
    workspace: form.elements.workspace.value,
    model_id: form.elements.model_id.value,
    approval_mode: form.elements.approval_mode.value,
    trigger: triggerFromForm(currentFormValues()),
    enabled: form.elements.enabled.checked,
  };
}

async function saveEditor() {
  if (await saveAutomation(readEditor(), editing?.id || null)) {
    closeAutomationEditor();
  }
}
