// 建 DOM 节点、格式化、复制、diff 上色这些小工具。
// 模型输出一律走 textContent / renderMarkdown 的 DOM 拼装，不拼 innerHTML。

import { icon } from "./icons.js";

export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text) {
    node.textContent = text;
  }
  return node;
}

export function button(className, label, iconName = "", title = "") {
  const node = el("button", className);
  node.type = "button";
  if (iconName) {
    node.append(icon(iconName));
  }
  if (label) {
    node.append(el("span", "", label));
  }
  if (title) {
    node.title = title;
  }
  return node;
}

export function iconButton(iconName, title, className = "icon-button") {
  const node = el("button", className);
  node.type = "button";
  node.append(icon(iconName));
  node.title = title;
  node.setAttribute("aria-label", title);
  return node;
}

export function clear(node) {
  node.replaceChildren();
}

export function show(node, visible) {
  node.classList.toggle("hidden", !visible);
}

export function formatNumber(count) {
  return (Number(count) || 0).toLocaleString("zh-CN");
}

export function formatTokens(count) {
  if (!Number.isFinite(count)) {
    return "?";
  }
  if (count >= 10000) {
    return `${Math.round(count / 1000)}k`;
  }
  if (count >= 1000) {
    return `${(count / 1000).toFixed(1)}k`;
  }
  return String(count);
}

export function formatBytes(size) {
  const value = Number(size) || 0;
  if (value >= 1024 * 1024) {
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }
  if (value >= 1024) {
    return `${Math.round(value / 1024)} KB`;
  }
  return `${value} B`;
}

function isToday(date) {
  return date.toDateString() === new Date().toDateString();
}

export function formatClock(isoText) {
  if (!isoText) {
    return "";
  }
  const date = new Date(isoText);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const time = date.toTimeString().slice(0, 5);
  if (isToday(date)) {
    return time;
  }
  return `${date.getMonth() + 1}-${date.getDate()} ${time}`;
}

export function formatRelativeTime(isoText) {
  if (!isoText) {
    return "";
  }
  const seconds = Math.max(0, (Date.now() - new Date(isoText).getTime()) / 1000);
  if (Number.isNaN(seconds)) {
    return "";
  }
  if (seconds < 60) {
    return "刚刚";
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} 分钟前`;
  }
  if (seconds < 86400) {
    return `${Math.floor(seconds / 3600)} 小时前`;
  }
  return `${Math.floor(seconds / 86400)} 天前`;
}

export function workspaceName(path) {
  if (!path) {
    return "";
  }
  const parts = path.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "avif", "ico"]);

export function isImagePath(path) {
  const name = (path || "").split("/").pop() || "";
  const dot = name.lastIndexOf(".");
  return dot >= 0 && IMAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (notAllowed) {
    const textarea = el("textarea");
    textarea.value = text;
    document.body.append(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    return copied;
  }
}

// diff 文本按行上色：+ 新增、- 删除、@@ 段落
export function renderDiff(diffText, className = "") {
  const pre = el("pre", `diff ${className}`.trim());
  for (const line of String(diffText ?? "").split("\n")) {
    let lineClass = "diff-line";
    if (line.startsWith("+") && !line.startsWith("+++")) {
      lineClass += " diff-add";
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      lineClass += " diff-remove";
    } else if (line.startsWith("@@")) {
      lineClass += " diff-hunk";
    }
    pre.append(el("span", lineClass, line || " "));
  }
  return pre;
}
