// 文件预览弹窗：从左侧文件树点开。文本带行号，图片直接显示；可以把 @路径 插进输入框让 agent 去读。
// 弹窗的元素 id（file-modal、file-modal-body……）两个 index.html 里要一致。

import { copyText, el, show } from "./dom.js";
import { formatBytes } from "./format.js";
import { imageSource, requestJson } from "./platform.js";

const elements = {};
let current = null;
let onCite = null;

function fileQuery(workspace, path) {
  return `workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`;
}

// handleCite({ workspace, path })：点「在对话中引用」，交给当前的输入框去插 @路径
export function initFilePreview(handleCite) {
  onCite = handleCite;
  elements.modal = document.getElementById("file-modal");
  elements.title = document.getElementById("file-modal-title");
  elements.meta = document.getElementById("file-modal-meta");
  elements.body = document.getElementById("file-modal-body");
  elements.copyButton = document.getElementById("file-copy");
  document.getElementById("file-close").addEventListener("click", closeFilePreview);
  document.getElementById("file-cite").addEventListener("click", citeCurrentFile);
  elements.copyButton.addEventListener("click", async () => {
    if (!current) {
      return;
    }
    elements.copyButton.textContent = (await copyText(current.absolutePath)) ? "已复制" : "复制失败";
  });
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeFilePreview();
    }
  });
}

export function isFilePreviewOpen() {
  return Boolean(elements.modal) && !elements.modal.classList.contains("hidden");
}

export function closeFilePreview() {
  show(elements.modal, false);
  current = null;
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
    preview = await requestJson(`/api/file?${fileQuery(workspace, path)}`);
  } catch (error) {
    elements.body.replaceChildren(el("div", "error-text", error.message));
    return;
  }
  current.absolutePath = preview.absolute_path;
  elements.meta.textContent = `${formatBytes(preview.size)} · ${preview.mime}`;

  if (preview.kind === "image") {
    const image = el("img", "file-image");
    image.alt = path;
    elements.body.replaceChildren(image);
    try {
      image.src = await imageSource(`/api/file/raw?${fileQuery(workspace, path)}`);
    } catch (error) {
      elements.body.replaceChildren(el("div", "error-text", `图片加载失败：${error.message}`));
    }
    return;
  }
  if (preview.kind === "binary") {
    elements.body.replaceChildren(el("div", "muted", "二进制文件，不显示内容"));
    return;
  }
  elements.body.replaceChildren(renderNumberedText(preview.content));
  if (preview.truncated) {
    elements.body.append(el("div", "muted", "文件太大，只显示了开头"));
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
  onCite?.({ workspace: current.workspace, path: current.path });
  closeFilePreview();
}

export function citationText(path) {
  return `@${path} `;
}
