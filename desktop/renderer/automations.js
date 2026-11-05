// 自动化：按触发器（到点、某个任务跑完、外部 POST、轮询）在工作区里开任务。
// 这里只画列表；新建 / 编辑弹窗是和 web 共用的那份（static/js/shared/automation-editor.js）。

import { request, sendJson } from "./api.js";
import { button, el, formatClock, formatRelativeTime, workspaceName } from "./dom.js";
import { state } from "./state.js";
import { RUN_STATUS_LABELS } from "./static/js/shared/labels.js";
import {
  automationFields,
  closeAutomationEditor,
  initAutomationEditor,
  isAutomationEditorOpen,
  openAutomationEditor,
  saveAutomation,
} from "./static/js/shared/automation-editor.js";

const elements = {};
let callbacks = {};
let automations = [];

// handlers.onOpenTask(sessionId)；handlers.getBackend() 返回 { baseUrl }，外部 POST 的地址要用后端的真实地址
export function initAutomations(handlers) {
  callbacks = handlers;
  elements.list = document.getElementById("automation-list");
  document.getElementById("new-automation").addEventListener("click", () => openAutomationEditor(null, automations));
  initAutomationEditor({
    getInfo: () => state.info,
    hookUrl: (automationId) => {
      const baseUrl = callbacks.getBackend?.()?.baseUrl || "http://127.0.0.1:<端口>";
      return `${baseUrl}/api/triggers/${encodeURIComponent(automationId)}/hook`;
    },
    onSaved: (result) => {
      automations = result.automations || [];
      render(result.problem);
    },
  });
}

export { isAutomationEditorOpen };

export function closeEditor() {
  closeAutomationEditor();
}

export async function showAutomations() {
  elements.list.replaceChildren(el("div", "muted", "加载中…"));
  let result;
  try {
    result = await request("/api/automations");
  } catch (error) {
    elements.list.replaceChildren(el("div", "error-text", `读取自动化失败：${error.message}`));
    return;
  }
  automations = result.automations || [];
  render(result.problem);
}

function render(problem = "") {
  elements.list.replaceChildren();
  if (problem) {
    // 调度整体的问题（配置文件坏了、某一轮调度抛错）：卡片上挂不出来，单独显示
    elements.list.append(el("div", "error-text", problem));
  }
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
    const label = RUN_STATUS_LABELS[automation.last_status] || automation.last_status;
    head.append(el("span", `automation-status ${automation.last_status}`, label));
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
  if (automation.problem) {
    card.append(el("div", "error-text", automation.problem));
  }

  const actions = el("div", "automation-actions");
  const run = button("ghost-button small", "立即运行");
  run.addEventListener("click", () => runNow(automation, run));
  const edit = button("ghost-button small", "编辑");
  edit.addEventListener("click", () => openAutomationEditor(automation, automations));
  const toggle = button("ghost-button small", automation.enabled ? "停用" : "启用");
  toggle.addEventListener("click", () => setEnabled(automation, !automation.enabled));
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

async function runNow(automation, runButton) {
  runButton.disabled = true;
  try {
    const result = await sendJson(`/api/automations/${encodeURIComponent(automation.id)}/run`, "POST", {});
    await showAutomations();
    callbacks.onOpenTask?.(result.session_id);
  } catch (error) {
    window.alert(`运行失败：${error.message}`);
    runButton.disabled = false;
  }
}

async function setEnabled(automation, enabled) {
  try {
    await saveAutomation({ ...automationFields(automation), enabled }, automation.id);
  } catch (error) {
    window.alert(`保存失败：${error.message}`);
    showAutomations();
  }
}

async function deleteAutomation(automation) {
  if (!window.confirm(`删除自动化「${automation.name}」？已经跑出来的任务不删。`)) {
    return;
  }
  try {
    const result = await sendJson(`/api/automations/${encodeURIComponent(automation.id)}`, "DELETE", {});
    automations = result.automations || [];
    render(result.problem);
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
  }
}
