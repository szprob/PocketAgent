// 左右两栏的宽度拖动：悬停高亮、双击复位，宽度记在本地偏好里。

import { loadPreference, savePreference } from "./state.js";

const SIDEBAR = { key: "sidebarWidth", elementId: "sidebar", resizerId: "sidebar-resizer", min: 220, max: 420, fallback: 288 };
const PANEL = { key: "rightPanelWidth", elementId: "right-panel", resizerId: "panel-resizer", min: 240, max: 460, fallback: 300, invert: true };
// 右侧栏切到「轨迹」时一行要放下角色、内容和耗时，默认更宽、也能拉得更宽；两个页签的宽度各记各的
const TRACE_PANEL = { ...PANEL, key: "tracePanelWidth", max: 900, fallback: 520 };
let panelSpec = PANEL;

export function initResizers() {
  bindResizer(SIDEBAR);
  bindResizer(PANEL, () => panelSpec);
}

// 右侧栏换页签时调：换成那个页签记住的宽度（拉宽也不超过窗口的一半多一点，免得把对话挤没了）
export function setPanelWidthMode(mode) {
  panelSpec = mode === "trace" ? TRACE_PANEL : PANEL;
  const element = document.getElementById(panelSpec.elementId);
  if (!element) {
    return;
  }
  const width = clamp(Number(loadPreference(panelSpec.key, panelSpec.fallback)), panelSpec.min, maxWidth(panelSpec));
  element.style.width = `${width}px`;
  element.style.flexBasis = `${width}px`;
}

function maxWidth(spec) {
  return spec === TRACE_PANEL ? Math.min(spec.max, Math.floor(window.innerWidth * 0.55)) : spec.max;
}

// currentSpec：右侧栏的上下限和偏好键跟着页签变，拖的时候按当前页签取
function bindResizer(initialSpec, currentSpec = () => initialSpec) {
  const element = document.getElementById(initialSpec.elementId);
  const resizer = document.getElementById(initialSpec.resizerId);
  if (!element || !resizer) {
    return;
  }
  const startSpec = currentSpec();
  const saved = Number(loadPreference(startSpec.key, startSpec.fallback));
  const width = clamp(saved, startSpec.min, maxWidth(startSpec));
  element.style.width = `${width}px`;
  element.style.flexBasis = `${width}px`;

  resizer.addEventListener("mousedown", (event) => {
    event.preventDefault();
    const spec = currentSpec();
    const startX = event.clientX;
    const startWidth = element.getBoundingClientRect().width;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (moveEvent) => {
      const delta = spec.invert ? startX - moveEvent.clientX : moveEvent.clientX - startX;
      const next = clamp(startWidth + delta, spec.min, maxWidth(spec));
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
    const spec = currentSpec();
    element.style.width = `${spec.fallback}px`;
    element.style.flexBasis = `${spec.fallback}px`;
    savePreference(spec.key, spec.fallback);
  });
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
