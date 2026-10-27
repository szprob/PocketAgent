// 添加工作区：一级一级浏览本机目录挑一个，也可以直接粘贴路径。

import { request, sendJson } from "./api.js";
import { el, show } from "./dom.js";
import { icon } from "./icons.js";

const elements = {};
let currentPath = null;
let onAdded = null;

export function initFolderPicker(handler) {
  onAdded = handler;
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
  homeButton.append(icon("folder"));
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
  return elements.modal && !elements.modal.classList.contains("hidden");
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
    result = await request(`/api/browse${query}`);
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
  const path = elements.pathInput.value.trim() || currentPath;
  if (!path) {
    return;
  }
  try {
    await sendJson("/api/workspaces", "POST", { path });
  } catch (error) {
    elements.error.textContent = error.message;
    return;
  }
  closeFolderPicker();
  onAdded?.(path);
}
