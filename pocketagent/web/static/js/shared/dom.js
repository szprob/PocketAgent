// 建 DOM 节点、复制、diff 上色这些小工具。
// 模型输出一律用 textContent 或 markdown.js 的节点拼装，不往 innerHTML 里拼用户内容。

import { icon } from "./platform.js";

export function el(tagName, className = "", text = undefined) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  // 0 也要显示出来，只有没传才不设
  if (text !== undefined && text !== null) {
    element.textContent = String(text);
  }
  return element;
}

export function button(className, label, iconName = "", title = "") {
  const element = el("button", className);
  element.type = "button";
  if (iconName) {
    element.append(icon(iconName));
  }
  if (label) {
    element.append(el("span", "", label));
  }
  if (title) {
    element.title = title;
    element.setAttribute("aria-label", title);
  }
  return element;
}

export function iconButton(iconName, title, className = "icon-button") {
  return button(className, "", iconName, title);
}

export function clear(element) {
  element.replaceChildren();
}

export function show(element, visible) {
  element.classList.toggle("hidden", !visible);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (notAllowed) {
    // 非安全上下文（比如经网关的 http）没有剪贴板权限，退回老办法
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

// 外观和字号：设置里选的存在 config.json（web 和桌面端共用），/api/info 里带着；启动和每次改完设置都套一次
export function applyAppearance(info) {
  const root = document.documentElement;
  if (info?.appearance === "light" || info?.appearance === "dark") {
    root.dataset.theme = info.appearance;
  } else {
    // 跟随系统：交给样式表里的 prefers-color-scheme
    delete root.dataset.theme;
  }
  root.style.setProperty("--font-scale", String(info?.font_scale || 1));
}
