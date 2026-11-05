// 自动化页：一条自动化 = 什么时候跑（触发器）+ 跑什么。这里只画列表，编辑弹窗是和桌面端共用的那份。

import { requestJson, sendJson } from "../core/api.js";
import { button, el, formatClock, icon, workspaceName } from "../ui/dom.js";
import { findModel, state } from "../core/state.js";
import { MODE_LABELS, RUN_STATUS_LABELS } from "../shared/labels.js";
import {
  automationFields,
  closeAutomationEditor,
  initAutomationEditor,
  isAutomationEditorOpen,
  openAutomationEditor,
  saveAutomation,
} from "../shared/automation-editor.js";

const elements = {};
let callbacks = {};
// 上一次拉到的列表：编辑弹窗里的「等哪个自动化」要从这里出选项
let loadedAutomations = [];

export function initAutomations(handlers) {
  callbacks = handlers;
  elements.list = document.getElementById("automation-list");
  document.getElementById("new-automation").addEventListener("click", () => openAutomationEditor(null, loadedAutomations));
  initAutomationEditor({
    getInfo: () => state.info,
    // 挂在网关子路径下时地址要带上子路径，所以相对页面算，不用 location.origin
    hookUrl: (automationId) => new URL(`api/triggers/${encodeURIComponent(automationId)}/hook`, window.location.href).href,
    onSaved: ({ automations, problem }) => renderList(automations, problem),
  });
}

export { isAutomationEditorOpen };

export function closeEditor() {
  closeAutomationEditor();
}

export async function showAutomations() {
  elements.list.replaceChildren(el("div", "muted", "加载中…"));
  try {
    const { automations, problem } = await requestJson("api/automations");
    renderList(automations, problem);
  } catch (error) {
    elements.list.replaceChildren(el("div", "error-text", error.message));
  }
}

function renderList(automations, problem = "") {
  loadedAutomations = automations;
  elements.list.replaceChildren();
  if (problem) {
    // 调度整体的问题（配置文件坏了、某一轮调度抛错）：列表可能是空的，卡片上挂不出来
    elements.list.appendChild(el("div", "error-text", problem));
  }
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
  enabled.addEventListener("change", () => setEnabled(automation, enabled.checked));
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
    const status = RUN_STATUS_LABELS[automation.last_status] || automation.last_status || "";
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
  edit.addEventListener("click", () => openAutomationEditor(automation, loadedAutomations));
  const remove = button("ghost-button small danger-text", "删除", "trash");
  remove.addEventListener("click", () => deleteAutomation(automation));
  actions.append(run, edit, remove);

  card.append(header, prompt, meta, actions);
  return card;
}

async function setEnabled(automation, enabled) {
  try {
    await saveAutomation({ ...automationFields(automation), enabled }, automation.id);
  } catch (error) {
    window.alert(error.message);
    showAutomations();
  }
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
  const { automations, problem } = await requestJson(`api/automations/${automation.id}`, { method: "DELETE" });
  renderList(automations, problem);
}
