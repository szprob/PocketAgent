// 自动化页：一条自动化 = 什么时候跑（触发器）+ 跑什么。列表 + 编辑弹窗。

import { requestJson, sendJson } from "../core/api.js";
import { button, el, formatClock, icon, show, workspaceName } from "../ui/dom.js";
import { MODE_LABELS, findModel, state } from "../core/state.js";

const WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"];
const TRIGGER_KINDS = ["clock", "task_end", "webhook", "poll"];
const STATUS_LABELS = {
  running: "运行中",
  done: "完成",
  error: "出错",
  denied: "有操作被拒绝",
  step_limit: "达到步数上限",
  interrupted: "被中断",
  truncated: "回复超长截断",
  filtered: "被内容审核中断",
  incomplete: "输出中断",
};

const elements = {};
let callbacks = {};
// 正在编辑的自动化（新建时为 null）
let editing = null;
// 上一次拉到的列表：编辑弹窗里的「等哪个自动化」要从这里出选项
let loadedAutomations = [];

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
  byId("new-automation").addEventListener("click", () => openEditor(null));
  byId("automation-cancel").addEventListener("click", closeEditor);
  byId("automation-save").addEventListener("click", saveAutomation);
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeEditor();
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
    loadedAutomations = automations;
    renderList(automations);
  } catch (error) {
    elements.list.replaceChildren(el("div", "error-text", error.message));
  }
}

// ---- 列表 ----

function renderList(automations) {
  loadedAutomations = automations;
  elements.list.replaceChildren();
  if (automations.length === 0) {
    const empty = el("div", "automation-empty");
    empty.append(
      icon("clock"),
      el("div", "", "还没有自动化。可以每天早上跑一次，也可以让它在某个任务跑完之后接着做，或者等外面的系统 POST 过来。")
    );
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
  enabled.addEventListener("change", () => saveForm({ ...readFormOf(automation), enabled: enabled.checked }, automation.id));
  const switchLabel = el("label", "switch");
  switchLabel.append(enabled, el("span", "switch-track"));
  header.append(
    el("span", "automation-name", automation.name),
    el("span", "schedule-pill", automation.schedule_text),
    el("span", "toolbar-spacer"),
    switchLabel
  );

  const prompt = el("div", "automation-prompt", automation.prompt);

  const meta = el("div", "automation-meta");
  const model = findModel(automation.model_id);
  meta.append(
    metaItem("工作区", workspaceName(automation.workspace)),
    metaItem("模型", model ? model.name : "默认模型"),
    metaItem("权限", MODE_LABELS[automation.approval_mode] || automation.approval_mode)
  );
  if (automation.next_run_at) {
    meta.appendChild(metaItem("下次", formatClock(automation.next_run_at)));
  }
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
  if (automation.problem) {
    meta.appendChild(metaItem("问题", automation.problem));
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
  const trigger = automation?.trigger || { source: { kind: "clock", mode: "daily" }, condition: { kind: "always" } };
  const source = trigger.source || { kind: "clock", mode: "daily" };
  const condition = trigger.condition || { kind: "always" };

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

  form.elements.trigger_kind.value = TRIGGER_KINDS.includes(source.kind) ? source.kind : "clock";
  const clockMode = source.mode === "interval" ? "interval" : "daily";
  form.querySelector(`input[name="clock_mode"][value="${clockMode}"]`).checked = true;
  form.elements.daily_time.value = source.daily_time || "09:00";
  form.elements.interval_minutes.value = source.interval_minutes || 60;
  const weekdays = source.weekdays || [0, 1, 2, 3, 4, 5, 6];
  for (const toggle of elements.weekdays.children) {
    toggle.classList.toggle("active", weekdays.includes(Number(toggle.dataset.day)));
  }
  fillWatchOptions(automation?.id, source.automation_id || "");
  form.elements.poll_command.value = source.command || "";
  form.elements.poll_interval_minutes.value = source.interval_minutes || 5;

  form.elements.condition_kind.value = condition.kind || "always";
  form.elements.condition_pattern.value = condition.pattern || "";
  form.elements.condition_command.value = condition.command || "";
  elements.webhookFields.textContent = webhookHint(automation, source);

  form.elements.enabled.checked = automation ? automation.enabled : true;
  syncTriggerFields();
  show(elements.modal, true);
  form.elements.name.focus();
}

function webhookHint(automation, source) {
  if (!automation?.id || source.kind !== "webhook" || !source.secret) {
    return "保存之后这里会显示地址和签名密钥：外面用 POST 打过来，正文随便写，签名对了才会开任务。";
  }
  const url = `${window.location.origin}/api/triggers/${automation.id}/hook`;
  return [
    `地址：POST ${url}`,
    "签名：X-PocketAgent-Signature: sha256=<HMAC-SHA256(正文, 密钥)>",
    "去重：X-PocketAgent-Delivery: <每次调用一个唯一 id>，同一个 id 只会跑一次",
    `密钥：${source.secret}`,
  ].join("\n");
}

function fillWatchOptions(currentAutomationId, selectedValue) {
  const select = elements.form.elements.watch_automation;
  const options = [{ value: "", label: "任何自动化" }].concat(
    loadedAutomations
      .filter((automation) => automation.id !== currentAutomationId)
      .map((automation) => ({ value: automation.id, label: automation.name }))
  );
  fillSelect(select, options, selectedValue);
}

function syncTriggerFields() {
  const form = elements.form;
  const kind = form.elements.trigger_kind.value;
  const clockMode = form.querySelector('input[name="clock_mode"]:checked')?.value || "daily";
  show(elements.clockFields, kind === "clock");
  show(elements.dailyFields, kind === "clock" && clockMode === "daily");
  show(elements.intervalFields, kind === "clock" && clockMode === "interval");
  show(elements.watchField, kind === "task_end");
  show(elements.pollFields, kind === "poll");
  show(elements.pollIntervalField, kind === "poll");
  show(elements.webhookFields, kind === "webhook");

  const conditionKind = form.elements.condition_kind.value;
  show(elements.conditionPatternField, conditionKind === "match");
  show(elements.conditionShellField, conditionKind === "shell");
}

function activeWeekdays() {
  return [...elements.weekdays.children]
    .filter((toggle) => toggle.classList.contains("active"))
    .map((toggle) => Number(toggle.dataset.day));
}

function readSource(kind) {
  const form = elements.form;
  if (kind === "clock") {
    const mode = form.querySelector('input[name="clock_mode"]:checked').value;
    if (mode === "interval") {
      return { kind, mode, interval_minutes: Number(form.elements.interval_minutes.value) || 60 };
    }
    return { kind, mode, daily_time: form.elements.daily_time.value, weekdays: activeWeekdays() };
  }
  if (kind === "task_end") {
    return { kind, automation_id: form.elements.watch_automation.value };
  }
  if (kind === "poll") {
    return {
      kind,
      interval_minutes: Number(form.elements.poll_interval_minutes.value) || 5,
      command: form.elements.poll_command.value,
    };
  }
  // webhook 的密钥在服务端生成：编辑时这里不带 secret，服务端会沿用原来的
  return { kind };
}

function readCondition() {
  const form = elements.form;
  const kind = form.elements.condition_kind.value;
  if (kind === "match") {
    return { kind, pattern: form.elements.condition_pattern.value };
  }
  if (kind === "shell") {
    return { kind, command: form.elements.condition_command.value };
  }
  return { kind };
}

function readForm() {
  const form = elements.form;
  return {
    name: form.elements.name.value,
    prompt: form.elements.prompt.value,
    workspace: form.elements.workspace.value,
    model_id: form.elements.model_id.value,
    approval_mode: form.elements.approval_mode.value,
    trigger: { source: readSource(form.elements.trigger_kind.value), condition: readCondition() },
    enabled: form.elements.enabled.checked,
  };
}

// 列表上的启用开关只改这一项，别把别的字段冲掉
function readFormOf(automation) {
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
