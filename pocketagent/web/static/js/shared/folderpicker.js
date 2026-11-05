// 添加工作区：一级一级浏览本机目录挑一个，也可以直接粘贴路径。
// 弹窗的元素 id（workspace-modal、folder-list……）两个 index.html 里要一致。

import { el, show } from "./dom.js";
import { icon, requestJson, sendJson } from "./platform.js";

const elements = {};
let currentPath = null;
let onAdded = null;

// handleAdded(path)：加好以后回调，path 是后端登记的写法（可能和输入框里的不完全一样）
export function initFolderPicker(handleAdded) {
  onAdded = handleAdded;
  elements.modal = document.getElementById("workspace-modal");
  elements.pathInput = document.getElementById("workspace-path-input");
  elements.list = document.getElementById("folder-list");
  elements.error = document.getElementById("workspace-error");
  document.getElementById("workspace-cancel").addEventListener("click", closeFolderPicker);
  document.getElementById("workspace-save").addEventListener("click", addCurrentFolder);
  document.getElementById("folder-up").addEventListener("click", () => {
    if (elements.list.dataset.parent) {
      browse(elements.list.dataset.parent);
    }
  });
  const homeButton = document.getElementById("folder-home");
  // web 的 index.html 里按 data-icon 统一填图标，桌面端没有这一步，空着就自己补上
  if (!homeButton.firstChild) {
    homeButton.append(icon("folder"));
  }
  homeButton.addEventListener("click", () => browse(null));
  elements.pathInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      browse(elements.pathInput.value.trim());
    }
  });
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeFolderPicker();
    }
  });
}

export function isFolderPickerOpen() {
  return Boolean(elements.modal) && !elements.modal.classList.contains("hidden");
}

export function closeFolderPicker() {
  show(elements.modal, false);
}

export function openFolderPicker() {
  elements.error.textContent = "";
  show(elements.modal, true);
  browse(null);
}

async function browse(path) {
  elements.error.textContent = "";
  let result;
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    result = await requestJson(`/api/browse${query}`);
  } catch (error) {
    elements.error.textContent = error.message;
    return;
  }
  currentPath = result.path;
  elements.pathInput.value = result.path;
  elements.list.dataset.parent = result.parent || "";
  elements.list.replaceChildren();
  if (result.directories.length === 0) {
    elements.list.append(el("div", "muted", "没有子目录"));
  }
  for (const directory of result.directories) {
    const row = el("button", "folder-row");
    row.type = "button";
    row.append(icon("folder"), el("span", "", directory.name));
    row.addEventListener("click", () => browse(directory.path));
    elements.list.append(row);
  }
}

async function addCurrentFolder() {
  // 输入框里手动改了路径就用输入框的
  const path = elements.pathInput.value.trim() || currentPath;
  if (!path) {
    return;
  }
  let result;
  try {
    result = await sendJson("/api/workspaces", "POST", { path });
  } catch (error) {
    elements.error.textContent = error.message;
    return;
  }
  closeFolderPicker();
  // 后端会把路径规整一遍（展开 ~、去掉末尾的 /），以它返回的列表为准
  const workspaces = result?.workspaces || [];
  const added = workspaces.find((workspace) => workspace.path === path) || workspaces.at(-1);
  onAdded?.(added?.path || path);
}
