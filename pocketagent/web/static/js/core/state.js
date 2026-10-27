// 各模块共享的状态，加一个很小的事件总线，模块之间不直接互相调用。

import { requestJson } from "./api.js";

export const MODE_LABELS = {
  ask: "每次询问",
  ai_auto: "AI 自动审批",
  full_auto: "完全访问",
  read_only: "只读",
  auto_edit: "自动改文件",
};

export const MODE_ICONS = {
  ask: "shield",
  ai_auto: "sparkle",
  full_auto: "alert",
};

export const state = {
  // /api/info：用户名、工作区、模型、审批模式
  info: null,
  // /api/sessions：所有任务
  tasks: [],
  // 当前打开的任务 id（在任务页时）
  currentTaskId: null,
  // 侧栏里展开了的工作区
  expandedWorkspaces: new Set(),
};

const handlers = new Map();

export function on(eventName, handler) {
  if (!handlers.has(eventName)) {
    handlers.set(eventName, []);
  }
  handlers.get(eventName).push(handler);
}

export function emit(eventName, payload) {
  for (const handler of handlers.get(eventName) || []) {
    handler(payload);
  }
}

export async function reloadInfo() {
  state.info = await requestJson("api/info");
  emit("info-changed", state.info);
  return state.info;
}

export async function reloadTasks() {
  const result = await requestJson("api/sessions");
  state.tasks = result.sessions;
  emit("tasks-changed", state.tasks);
  return state.tasks;
}

export function findModel(modelId) {
  return (state.info?.models || []).find((model) => model.id === modelId) || null;
}

// 浏览器本地记住的界面偏好（侧栏开关等）；读写失败（隐私模式）就用默认值
export function loadPreference(key, fallback) {
  try {
    const stored = window.localStorage.getItem(`pocketagent.${key}`);
    return stored === null ? fallback : JSON.parse(stored);
  } catch (unavailable) {
    return fallback;
  }
}

export function savePreference(key, value) {
  try {
    window.localStorage.setItem(`pocketagent.${key}`, JSON.stringify(value));
  } catch (unavailable) {
    // 存不了就算了，只是下次打开回到默认
  }
}
