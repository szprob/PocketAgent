// 设置 → 记忆：看某个工作区的项目记忆（MEMORY.md 索引 + 每条记忆），可以查看、删除。开关跟着设置一起保存。

import { requestJson, sendJson } from "../core/api.js";
import { button, el, relativeTime } from "../ui/dom.js";
import { renderMarkdown } from "../ui/markdown.js";
import { state } from "../core/state.js";

const TYPE_LABELS = { user: "用户", feedback: "反馈", project: "项目", reference: "参考" };

const view = {
  container: null,
  workspace: "",
  // 展开查看的那一条
  openFile: null,
};

function currentTaskWorkspace() {
  const task = state.tasks.find((each) => each.id === state.currentTaskId);
  return task?.workspace || state.info?.default_workspace || "";
}

export function renderMemoryPage(container) {
  view.container = container;
  const workspaces = state.info?.workspaces || [];
  if (workspaces.length === 0) {
    container.replaceChildren(el("div", "muted", "还没有工作区。"));
    return;
  }
  const preferred = view.workspace || currentTaskWorkspace() || workspaces[0].path;
  view.workspace = workspaces.some((each) => each.path === preferred) ? preferred : workspaces[0].path;
  view.openFile = null;
  loadMemory();
}

function workspaceSelect() {
  const select = document.createElement("select");
  for (const workspace of state.info.workspaces) {
    const option = el("option", "", `${workspace.name}  ·  ${workspace.path}`);
    option.value = workspace.path;
    select.appendChild(option);
  }
  select.value = view.workspace;
  select.addEventListener("change", () => {
    view.workspace = select.value;
    view.openFile = null;
    loadMemory();
  });
  const field = el("label", "field");
  field.append(el("span", "", "工作区"), select);
  return field;
}

async function loadMemory() {
  const container = view.container;
  container.replaceChildren(workspaceSelect(), el("div", "muted", "加载中…"));
  let overview;
  try {
    overview = await requestJson(`api/memory?workspace=${encodeURIComponent(view.workspace)}`);
  } catch (error) {
    container.replaceChildren(workspaceSelect(), el("div", "error-text", error.message));
    return;
  }
  renderOverview(overview);
}

function renderOverview(overview) {
  const container = view.container;
  const hint = el(
    "div",
    "field-hint",
    `存在 ${overview.directory}，不进仓库。开任务时把 MEMORY.md 放进系统提示，具体某条用到时 agent 再去读；记和改都是 agent 自己做的。`
  );
  const list = el("div", "skill-list");
  list.appendChild(memoryRow({ file: "MEMORY.md", name: "MEMORY.md", description: "索引：一行一条记忆", type: "", updated_at: "" }, true));
  for (const memory of overview.memories) {
    list.appendChild(memoryRow(memory, false));
  }
  if (overview.memories.length === 0) {
    list.appendChild(el("div", "muted memory-empty", "这个工作区还没有记忆。跟 agent 说「记住……」，或者它觉得以后用得上时会自己记。"));
  }
  container.replaceChildren(workspaceSelect(), hint, list);
}

function memoryRow(memory, isIndex) {
  const wrapper = el("div", "memory-item");
  const row = el("div", "skill-row memory-row");
  const text = el("div", "skill-text");
  const title = el("div", "skill-title");
  title.appendChild(el("strong", "", memory.name));
  if (memory.type) {
    title.appendChild(el("span", "skill-source", TYPE_LABELS[memory.type] || memory.type));
  }
  if (memory.updated_at) {
    title.appendChild(el("span", "memory-time", relativeTime(memory.updated_at)));
  }
  text.append(title, el("div", "skill-description", memory.description || "（没写描述）"));
  text.title = memory.file;
  const actions = el("div", "memory-actions");
  const toggle = button("ghost-button small", view.openFile === memory.file ? "收起" : "查看");
  toggle.addEventListener("click", () => {
    view.openFile = view.openFile === memory.file ? null : memory.file;
    loadMemory();
  });
  actions.appendChild(toggle);
  if (!isIndex) {
    const remove = button("ghost-button small danger-text", "删除");
    remove.addEventListener("click", () => removeMemory(memory));
    actions.appendChild(remove);
  }
  row.append(text, actions);
  wrapper.appendChild(row);
  if (view.openFile === memory.file) {
    wrapper.appendChild(renderContent(memory.file));
  }
  return wrapper;
}

function renderContent(fileName) {
  const box = el("div", "memory-body markdown-view");
  box.appendChild(el("div", "muted", "加载中…"));
  const query = `workspace=${encodeURIComponent(view.workspace)}&file=${encodeURIComponent(fileName)}`;
  requestJson(`api/memory/file?${query}`)
    .then(({ content }) => {
      box.replaceChildren();
      box.innerHTML = renderMarkdown(content || "（空）");
    })
    .catch((error) => box.replaceChildren(el("div", "error-text", error.message)));
  return box;
}

async function removeMemory(memory) {
  if (!window.confirm(`删掉这条记忆？\n${memory.name}（${memory.file}）\n\nMEMORY.md 里指向它的那一行也会一起去掉。`)) {
    return;
  }
  const query = `workspace=${encodeURIComponent(view.workspace)}&file=${encodeURIComponent(memory.file)}`;
  try {
    const overview = await sendJson(`api/memory/file?${query}`, "DELETE");
    view.openFile = null;
    renderOverview(overview);
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
  }
}
