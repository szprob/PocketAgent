// 三栏之间的拖动条：悬停出现竖线，按住左右拖改栏宽，双击回到默认宽度，宽度记在浏览器本地。

import { loadPreference, savePreference } from "./state.js";

const SIDEBAR_DEFAULT_PIXELS = 268;
const PANEL_DEFAULT_PIXELS = 320;
const SIDEBAR_MIN_PIXELS = 200;
const SIDEBAR_MAX_PIXELS = 460;
const PANEL_MIN_PIXELS = 260;

function rememberedWidth(preferenceKey, defaultPixels, minPixels) {
  return Math.max(loadPreference(preferenceKey, defaultPixels), minPixels);
}

function applyWidth(variableName, widthPixels) {
  document.documentElement.style.setProperty(variableName, `${Math.round(widthPixels)}px`);
}

function maxPanelPixels() {
  // 右侧栏最多拉到窗口的一半，免得把对话挤没了
  return Math.max(PANEL_MIN_PIXELS, Math.floor(window.innerWidth * 0.5));
}

function bindResizer(resizer, { side, variableName, preferenceKey, defaultPixels, minPixels, maxPixels }) {
  let dragging = false;
  let startPointerX = 0;
  let startWidth = 0;
  let lastWidth = 0;

  resizer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) {
      return;
    }
    dragging = true;
    startPointerX = event.clientX;
    startWidth = rememberedWidth(preferenceKey, defaultPixels, minPixels);
    // 没动过也先把起点宽度落到变量上，endDrag 里直接读 lastWidth 就是最终宽度
    lastWidth = startWidth;
    applyWidth(variableName, lastWidth);
    resizer.setPointerCapture(event.pointerId);
    resizer.classList.add("dragging");
    document.body.classList.add("resizing");
    event.preventDefault();
  });

  resizer.addEventListener("pointermove", (event) => {
    if (!dragging) {
      return;
    }
    // 宽度 = 起点宽度 + 相对起点的总位移，指针跑出窗口再回来也不会跳
    const draggedWidth = side === "left"
      ? startWidth + (event.clientX - startPointerX)
      : startWidth - (event.clientX - startPointerX);
    lastWidth = Math.min(Math.max(draggedWidth, minPixels), maxPixels());
    applyWidth(variableName, lastWidth);
  });

  function endDrag() {
    if (!dragging) {
      return;
    }
    dragging = false;
    resizer.classList.remove("dragging");
    document.body.classList.remove("resizing");
    savePreference(preferenceKey, Math.round(lastWidth));
  }

  resizer.addEventListener("pointerup", endDrag);
  resizer.addEventListener("pointercancel", endDrag);

  resizer.addEventListener("dblclick", () => {
    applyWidth(variableName, defaultPixels);
    savePreference(preferenceKey, defaultPixels);
  });
}

export function initResizers() {
  applyWidth("--sidebar-width", rememberedWidth("sidebarWidth", SIDEBAR_DEFAULT_PIXELS, SIDEBAR_MIN_PIXELS));
  applyWidth("--panel-width", rememberedWidth("panelWidth", PANEL_DEFAULT_PIXELS, PANEL_MIN_PIXELS));
  bindResizer(document.getElementById("sidebar-resizer"), {
    side: "left",
    variableName: "--sidebar-width",
    preferenceKey: "sidebarWidth",
    defaultPixels: SIDEBAR_DEFAULT_PIXELS,
    minPixels: SIDEBAR_MIN_PIXELS,
    maxPixels: () => SIDEBAR_MAX_PIXELS,
  });
  bindResizer(document.getElementById("panel-resizer"), {
    side: "right",
    variableName: "--panel-width",
    preferenceKey: "panelWidth",
    defaultPixels: PANEL_DEFAULT_PIXELS,
    minPixels: PANEL_MIN_PIXELS,
    maxPixels: maxPanelPixels,
  });
}
