// DOM 小工具和格式化函数。

import { iconSvg } from "./icons.js";

export function el(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  if (text !== undefined && text !== null) {
    element.textContent = text;
  }
  return element;
}

export function icon(name, className) {
  const wrapper = el("span", `icon ${className || ""}`);
  // 图标是 icons.js 里写死的常量，不含用户输入
  wrapper.innerHTML = iconSvg(name);
  return wrapper;
}

export function button(className, label, iconName, title) {
  const element = el("button", className);
  element.type = "button";
  if (iconName) {
    element.appendChild(icon(iconName));
  }
  if (label) {
    element.appendChild(el("span", "", label));
  }
  if (title) {
    element.title = title;
    element.setAttribute("aria-label", title);
  }
  return element;
}

export function show(element, visible) {
  element.classList.toggle("hidden", !visible);
}

export function workspaceName(path) {
  if (!path) {
    return "";
  }
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "avif", "ico"]);

export function isImagePath(path) {
  const name = (path || "").split("/").pop() || "";
  const dot = name.lastIndexOf(".");
  return dot >= 0 && IMAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

// 大数字缩成 1.2k / 35k / 1.5m / 2.1b；桌面端 dom.js 里是同一套写法
const COMPACT_UNITS = [
  { size: 1_000_000_000, suffix: "b" },
  { size: 1_000_000, suffix: "m" },
  { size: 1000, suffix: "k" },
];

export function formatTokens(count) {
  const number = Number(count) || 0;
  for (const unit of COMPACT_UNITS) {
    if (number >= unit.size) {
      const value = number / unit.size;
      // 十以内留一位小数（1.2k），再大就取整（35k），不然数字太长
      return `${value.toFixed(value >= 10 ? 0 : 1)}${unit.suffix}`;
    }
  }
  return String(Math.round(number));
}

export function formatNumber(count) {
  return (Number(count) || 0).toLocaleString("zh-CN");
}

function isToday(date) {
  const now = new Date();
  return date.toDateString() === now.toDateString();
}

export function formatClock(isoText) {
  if (!isoText) {
    return "";
  }
  const date = new Date(isoText);
  const time = date.toTimeString().slice(0, 5);
  if (isToday(date)) {
    return time;
  }
  return `${date.getMonth() + 1}-${date.getDate()} ${time}`;
}

export function relativeTime(isoText) {
  if (!isoText) {
    return "";
  }
  const seconds = (Date.now() - new Date(isoText).getTime()) / 1000;
  if (seconds < 60) {
    return "刚刚";
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} 分钟前`;
  }
  if (seconds < 86400) {
    return `${Math.floor(seconds / 3600)} 小时前`;
  }
  if (seconds < 86400 * 7) {
    return `${Math.floor(seconds / 86400)} 天前`;
  }
  return formatClock(isoText);
}

export function formatBytes(size) {
  if (size >= 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
  if (size >= 1024) {
    return `${Math.round(size / 1024)} KB`;
  }
  return `${size} B`;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (notAllowed) {
    // 非安全上下文（比如经网关的 http）没有剪贴板权限，退回老办法
    const textarea = el("textarea");
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    return copied;
  }
}

// diff 文本按行上色
export function renderDiff(diffText, className) {
  const pre = el("pre", `diff ${className || ""}`);
  for (const line of diffText.split("\n")) {
    let lineClass = "diff-line";
    if (line.startsWith("+") && !line.startsWith("+++")) {
      lineClass += " diff-add";
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      lineClass += " diff-remove";
    } else if (line.startsWith("@@")) {
      lineClass += " diff-hunk";
    }
    pre.appendChild(el("span", lineClass, line || " "));
  }
  return pre;
}
