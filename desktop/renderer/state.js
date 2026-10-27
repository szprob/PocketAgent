// 各模块共享的状态和一点点事件总线；偏好放 localStorage（记在应用自己的数据目录里）。

export const state = {
  info: null,
  tasks: [],
  currentTaskId: null,
  view: "new",
  expandedWorkspaces: new Set(),
};

const listeners = new Map();

export function on(event, handler) {
  if (!listeners.has(event)) {
    listeners.set(event, []);
  }
  listeners.get(event).push(handler);
}

export function emit(event, payload) {
  for (const handler of listeners.get(event) || []) {
    handler(payload);
  }
}

export function loadPreference(key, fallback) {
  try {
    const raw = localStorage.getItem(`pocketagent.${key}`);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (notAvailable) {
    return fallback;
  }
}

export function savePreference(key, value) {
  try {
    localStorage.setItem(`pocketagent.${key}`, JSON.stringify(value));
  } catch (notAvailable) {
    // 存不了就算了，下次打开用默认值
  }
}
