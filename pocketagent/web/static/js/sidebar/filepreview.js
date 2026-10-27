// 文件预览弹窗：从左侧文件树点开。文本带行号，图片直接显示；可以把 @路径 插进输入框让 agent 去读。

import { requestJson, withToken } from "../core/api.js";
import { copyText, el, formatBytes, show } from "../ui/dom.js";
import { emit } from "../core/state.js";

const elements = {};
let current = null;

export function initFilePreview() {
  elements.modal = document.getElementById("file-modal");
  elements.title = document.getElementById("file-modal-title");
  elements.meta = document.getElementById("file-modal-meta");
  elements.body = document.getElementById("file-modal-body");
  document.getElementById("file-close").addEventListener("click", closeFilePreview);
  document.getElementById("file-cite").addEventListener("click", citeCurrentFile);
  const copyButton = document.getElementById("file-copy");
  copyButton.addEventListener("click", async () => {
    if (current) {
      copyButton.textContent = (await copyText(current.absolutePath)) ? "已复制" : "复制失败";
    }
  });
  elements.copyButton = copyButton;
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeFilePreview();
    }
  });
}

export function isFilePreviewOpen() {
  return !elements.modal.classList.contains("hidden");
}

export function closeFilePreview() {
  show(elements.modal, false);
  current = null;
}

function fileQuery(workspace, path) {
  return `workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`;
}

export async function openFilePreview(workspace, path) {
  current = { workspace, path, absolutePath: path };
  elements.title.textContent = path;
  elements.meta.textContent = "";
  elements.copyButton.textContent = "复制路径";
  elements.body.replaceChildren(el("div", "muted", "加载中…"));
  show(elements.modal, true);
  let preview;
  try {
    preview = await requestJson(`api/file?${fileQuery(workspace, path)}`);
  } catch (error) {
    elements.body.replaceChildren(el("div", "error-text", error.message));
    return;
  }
  current.absolutePath = preview.absolute_path;
  elements.meta.textContent = `${formatBytes(preview.size)} · ${preview.mime}`;
  if (preview.kind === "image") {
    const image = el("img", "file-image");
    image.src = withToken(`api/file/raw?${fileQuery(workspace, path)}`);
    image.alt = path;
    elements.body.replaceChildren(image);
  } else if (preview.kind === "binary") {
    elements.body.replaceChildren(el("div", "muted", "二进制文件，不显示内容"));
  } else {
    elements.body.replaceChildren(renderNumberedText(preview.content));
    if (preview.truncated) {
      elements.body.appendChild(el("div", "muted", "文件太大，只显示了开头"));
    }
  }
}

function renderNumberedText(content) {
  const lines = content.split("\n");
  const gutter = el("pre", "file-gutter", lines.map((_, index) => index + 1).join("\n"));
  const code = el("pre", "file-code", content);
  const wrapper = el("div", "file-text");
  wrapper.append(gutter, code);
  return wrapper;
}

function citeCurrentFile() {
  if (!current) {
    return;
  }
  // 交给 main.js 放进当前的输入框（任务页或新任务页）
  emit("cite-file", { workspace: current.workspace, path: current.path });
  closeFilePreview();
}

export function citationText(path) {
  return `@${path} `;
}
