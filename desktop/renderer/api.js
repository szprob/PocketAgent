// 后端 API 客户端：REST 走主进程代理（绕开跨域），WebSocket 直连后端。
// 这里只认后端协议，不依赖 web/static 里的任何代码。

const bridge = window.pocketagentDesktop;

export function getBackendState() {
  return bridge.getBackendState();
}

export function restartBackend() {
  return bridge.restartBackend();
}

export function onBackendState(callback) {
  return bridge.onBackendState(callback);
}

export function onBackendLog(callback) {
  return bridge.onBackendLog(callback);
}

export function openExternal(url) {
  return bridge.openExternal(url);
}

export function openInBrowser() {
  return bridge.openInBrowser();
}

export async function request(path, options = {}) {
  const result = await bridge.request(path, options);
  if (!result.ok) {
    const error = new Error(result.error || `请求失败（${result.status}）`);
    error.status = result.status;
    throw error;
  }
  return result.data;
}

export function sendJson(path, method, body) {
  return request(path, { method, body });
}

export async function uploadFiles(sessionId, files) {
  if (files.length === 0) {
    return [];
  }
  const payload = [];
  for (const file of files) {
    payload.push({
      name: file.name || "粘贴的图片.png",
      type: file.type || "application/octet-stream",
      bytes: new Uint8Array(await file.arrayBuffer()),
    });
  }
  const result = await bridge.upload(sessionId, payload);
  if (!result.ok) {
    throw new Error(result.error || `上传失败（${result.status}）`);
  }
  return result.data?.attachments || [];
}

// 附件和图片预览要带 token，渲染进程不能直接 fetch，交给主进程取回来转成 data URL。
// 同一个地址只取一次（切换任务时会重新挂载 DOM，但图片缓存可以复用）。
const binaryCache = new Map();

export async function binaryDataUrl(path) {
  if (binaryCache.has(path)) {
    return binaryCache.get(path);
  }
  const promise = bridge.readBinary(path).then((result) => {
    if (!result.ok) {
      throw new Error(result.error || "读取失败");
    }
    return `data:${result.data.mime};base64,${result.data.base64}`;
  });
  binaryCache.set(path, promise);
  try {
    return await promise;
  } catch (error) {
    binaryCache.delete(path);
    throw error;
  }
}

export function attachmentUrl(sessionId, attachmentId) {
  return `/api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}`;
}

// 会话工作区里的文件（聊天图片、产出缩略图用）；内容交给 binaryDataUrl 走主进程取
export function sessionFilePath(sessionId, path) {
  return `/api/sessions/${encodeURIComponent(sessionId)}/file/raw?path=${encodeURIComponent(path)}`;
}

export function runCommand(text, { sessionId, workspace } = {}) {
  return sendJson("/api/command", "POST", {
    text,
    session_id: sessionId || null,
    workspace: workspace || null,
  });
}

export function looksLikeCommand(text) {
  return /^\/[^\s/]+(\s.*)?$/.test(text.trim());
}

const skillNameCache = new Map();

export function loadSkillNames(workspace) {
  const key = workspace || "";
  if (!skillNameCache.has(key)) {
    const promise = request(`/api/skills?workspace=${encodeURIComponent(key)}`)
      .then((data) => (data.skills || []).filter((skill) => skill.enabled).map((skill) => skill.name))
      .catch(() => []);
    skillNameCache.set(key, promise);
  }
  return skillNameCache.get(key);
}

export function invalidateSkillNames() {
  skillNameCache.clear();
}

export function socketUrl(backendState, sessionId) {
  const url = new URL(backendState.baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/ws/${encodeURIComponent(sessionId)}`;
  url.search = `?token=${encodeURIComponent(backendState.token)}`;
  url.hash = "";
  return url.toString();
}

export class SessionSocket {
  // 每个任务一条连接；断了自动重连，重连后后端会先发一份 snapshot，界面直接重放。
  constructor(sessionId, handlers) {
    this.sessionId = sessionId;
    this.handlers = handlers;
    this.backendState = null;
    this.socket = null;
    this.retryTimer = null;
    this.retryCount = 0;
    this.pendingMessages = [];
    this.closedByUs = false;
  }

  connect(backendState) {
    this.backendState = backendState;
    this.closedByUs = false;
    const socket = new WebSocket(socketUrl(backendState, this.sessionId));
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.retryCount = 0;
      const queued = this.pendingMessages;
      this.pendingMessages = [];
      for (const message of queued) {
        socket.send(JSON.stringify(message));
      }
      this.handlers.onOpen?.();
    });

    socket.addEventListener("message", (event) => {
      let message = null;
      try {
        message = JSON.parse(event.data);
      } catch (notJson) {
        return;
      }
      this.handlers.onMessage?.(message);
    });

    socket.addEventListener("close", () => {
      if (this.socket !== socket) {
        return;
      }
      this.socket = null;
      this.handlers.onClose?.();
      this.scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      // close 事件里统一处理重连
    });
  }

  scheduleReconnect() {
    if (this.closedByUs || this.retryCount >= 8) {
      return;
    }
    const delay = Math.min(800 * (this.retryCount + 1), 5000);
    this.retryCount += 1;
    this.retryTimer = setTimeout(() => {
      if (!this.closedByUs) {
        this.connect(this.backendState);
      }
    }, delay);
  }

  send(message) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
      return true;
    }
    this.pendingMessages.push(message);
    return false;
  }

  close() {
    this.closedByUs = true;
    clearTimeout(this.retryTimer);
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }
}
