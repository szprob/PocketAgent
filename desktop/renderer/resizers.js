// 左右两栏的宽度拖动：悬停高亮、双击复位，宽度记在本地偏好里。

import { loadPreference, savePreference } from "./state.js";

const SIDEBAR = { key: "sidebarWidth", elementId: "sidebar", resizerId: "sidebar-resizer", min: 220, max: 420, fallback: 288 };
const PANEL = { key: "rightPanelWidth", elementId: "right-panel", resizerId: "panel-resizer", min: 240, max: 460, fallback: 300, invert: true };

export function initResizers() {
  bindResizer(SIDEBAR);
  bindResizer(PANEL);
}

function bindResizer(spec) {
  const element = document.getElementById(spec.elementId);
  const resizer = document.getElementById(spec.resizerId);
  if (!element || !resizer) {
    return;
  }
  const saved = Number(loadPreference(spec.key, spec.fallback));
  const width = clamp(saved, spec.min, spec.max);
  element.style.width = `${width}px`;
  element.style.flexBasis = `${width}px`;

  resizer.addEventListener("mousedown", (event) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = element.getBoundingClientRect().width;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (moveEvent) => {
      const delta = spec.invert ? startX - moveEvent.clientX : moveEvent.clientX - startX;
      const next = clamp(startWidth + delta, spec.min, spec.max);
      element.style.width = `${next}px`;
      element.style.flexBasis = `${next}px`;
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      savePreference(spec.key, Math.round(element.getBoundingClientRect().width));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  resizer.addEventListener("dblclick", () => {
    element.style.width = `${spec.fallback}px`;
    element.style.flexBasis = `${spec.fallback}px`;
    savePreference(spec.key, spec.fallback);
  });
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
