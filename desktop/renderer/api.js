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

// 每条要保证送达的消息带一个 id：重连后补发同一条时，后端按它去重
export function newClientId() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// 应用层心跳：连上之后每 15 秒发一条 ping，后端回 pong。
// WebSocket 自带的 ping/pong 帧在 JS 里看不见，收不到东西就没法判断连接是不是还活着。
const PING_INTERVAL_MS = 15000;
// 超过这么久一条消息都没收到（三次心跳都没回）就当连接已经断了
const ALIVE_TIMEOUT_MS = 45000;
const WATCHDOG_INTERVAL_MS = 5000;
const MAX_RETRY_DELAY_MS = 5000;

export class SessionSocket {
  // 每个任务一条连接；断了自动重连，重连后后端会先发一份 snapshot，界面直接重放。
  //
  // 两件容易丢消息的事在这里兜住：
  // 1. 连接"看着还是 OPEN"其实对面早没了（合盖休眠、代理超时、后端重启）：send 不报错，
  //    消息就消失。所以有心跳和看门狗，判死之后主动重连，不等 close 事件（它可能永远不来）。
  // 2. 发出去到后端确认之间断线：消息进 outbox，重连后自动补发；后端按 client_id 去重，
  //    不会因为补发让这一轮跑两遍。
  constructor(sessionId, handlers) {
    this.sessionId = sessionId;
    this.handlers = handlers;
    this.backendState = null;
    this.socket = null;
    this.retryTimer = null;
    this.retryCount = 0;
    // client_id -> 还没被后端确认的消息
    this.outbox = new Map();
    this.pingTimer = null;
    this.watchdogTimer = null;
    this.lastInboundAt = 0;
    // 后端在 snapshot 里报的可选能力（ping / ack）；还没收到快照时是 null
    this.serverFeatures = null;
    this.closedByUs = false;
    this.wakeHandler = () => this.checkAlive(true);
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.wakeHandler);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", this.wakeHandler);
      // 从别的应用切回来：窗口一直可见的话不会有 visibilitychange，只有 focus。
      // 一回来就判一次活，别等用户敲了字才发现连接早断了
      window.addEventListener("focus", this.wakeHandler);
    }
  }

  connect(backendState) {
    this.backendState = backendState;
    this.closedByUs = false;
    // 每连一条都重新认一遍能力（后端可能刚重启过、换了版本）
    this.serverFeatures = null;
    const socket = new WebSocket(socketUrl(backendState, this.sessionId));
    this.socket = socket;
    this.lastInboundAt = Date.now();

    socket.addEventListener("open", () => {
      this.retryCount = 0;
      this.lastInboundAt = Date.now();
      this.flush();
      this.startHeartbeat();
      this.handlers.onOpen?.();
    });

    socket.addEventListener("message", (event) => {
      this.lastInboundAt = Date.now();
      let message = null;
      try {
        message = JSON.parse(event.data);
      } catch (notJson) {
        return;
      }
      if (message.type === "ack") {
        this.outbox.delete(message.client_id);
      }
      if (message.type === "snapshot") {
        this.serverFeatures = new Set(message.features || []);
        if (!this.serverFeatures.has("ack")) {
          // 老后端不会 ack：消息别一直留在 outbox 里，不然重连后会重复发一遍（那边没有去重）
          this.outbox.clear();
        }
      }
      this.handlers.onMessage?.(message);
    });

    socket.addEventListener("close", () => {
      if (this.socket !== socket) {
        return;
      }
      this.socket = null;
      this.stopHeartbeat();
      this.handlers.onClose?.();
      this.scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      // close 事件里统一处理重连
    });
  }

  isOpen() {
    return Boolean(this.socket) && this.socket.readyState === WebSocket.OPEN;
  }

  // 按发送顺序把没送到的东西再发一遍；重连之后后端会先去重
  flush() {
    for (const message of this.outbox.values()) {
      this.transmit(message);
    }
  }

  transmit(message) {
    if (!this.isOpen()) {
      return false;
    }
    try {
      this.socket.send(JSON.stringify(message));
      return true;
    } catch (sendError) {
      // 正好在这一刻断了：消息还留在 outbox 里，重连后补发
      return false;
    }
  }

  startHeartbeat() {
    this.stopHeartbeat();
    this.pingTimer = setInterval(() => this.transmit({ type: "ping", t: Date.now() }), PING_INTERVAL_MS);
    this.watchdogTimer = setInterval(() => this.checkAlive(), WATCHDOG_INTERVAL_MS);
  }

  stopHeartbeat() {
    clearInterval(this.pingTimer);
    clearInterval(this.watchdogTimer);
    this.pingTimer = null;
    this.watchdogTimer = null;
  }

  // 判活：窗口在后台时不判（定时器被限流，心跳本来就会变慢，判了只会来回重连），
  // 切回来、网络恢复的时候再判一次。
  checkAlive(wake = false) {
    if (this.closedByUs) {
      return;
    }
    if (!wake && typeof document !== "undefined" && document.hidden) {
      return;
    }
    if (!this.socket) {
      // 已经断了、还在等下一次重试：刚回到前台就别再等那几秒
      if (wake && this.backendState) {
        clearTimeout(this.retryTimer);
        this.retryCount = 0;
        this.connect(this.backendState);
      }
      return;
    }
    // 老后端不回 pong：认了就关掉判活，不然每 45 秒白重连一次
    if (this.serverFeatures && !this.serverFeatures.has("ping")) {
      return;
    }
    if (Date.now() - this.lastInboundAt <= ALIVE_TIMEOUT_MS) {
      return;
    }
    this.dropConnection();
  }

  // 连接判死了：主动丢掉重连。不走 socket.close() 等 close 事件——半死的连接上它可能永远不来
  dropConnection() {
    const socket = this.socket;
    this.socket = null;
    this.stopHeartbeat();
    try {
      socket?.close();
    } catch (closeError) {
      // 已经断了的连接关不掉也无所谓
    }
    this.handlers.onClose?.();
    this.scheduleReconnect();
  }

  scheduleReconnect() {
    if (this.closedByUs) {
      return;
    }
    clearTimeout(this.retryTimer);
    const delay = Math.min(500 * 2 ** this.retryCount, MAX_RETRY_DELAY_MS);
    this.retryCount += 1;
    this.retryTimer = setTimeout(() => {
      if (!this.closedByUs) {
        this.connect(this.backendState);
      }
    }, delay);
  }

  send(message) {
    // 这两类不进 outbox：remove_queued 是按序号删的（补发会删错一条），ping 是心跳（没必要留底）
    if (message.type === "remove_queued" || message.type === "ping") {
      return this.transmit(message);
    }
    if (!message.client_id) {
      message.client_id = newClientId();
    }
    this.outbox.set(message.client_id, message);
    return this.transmit(message);
  }

  // 关掉这条连接，返回还没被后端确认的消息：调用方负责把内容还给用户（放回输入框），
  // 不能因为切了任务 / 关了窗口就让打出去的字无声消失
  close() {
    this.closedByUs = true;
    clearTimeout(this.retryTimer);
    this.stopHeartbeat();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.wakeHandler);
    }
    if (typeof window !== "undefined") {
      window.removeEventListener("online", this.wakeHandler);
      window.removeEventListener("focus", this.wakeHandler);
    }
    const socket = this.socket;
    this.socket = null;
    try {
      socket?.close();
    } catch (closeError) {
      // 已经断了
    }
    const unsent = [...this.outbox.values()];
    this.outbox.clear();
    return unsent;
  }
}
