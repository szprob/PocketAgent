// 渲染进程能用的原生能力入口。
// REST 由主进程代发，绕开自定义协议到 http://127.0.0.1 的跨域限制；WebSocket 由渲染进程直连（带 token）。
// 这里只做转发，不放业务逻辑。

const { contextBridge, ipcRenderer } = require("electron");

function subscribe(channel, callback) {
  const listener = (event, payload) => callback(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld("pocketagentDesktop", {
  getBackendState: () => ipcRenderer.invoke("backend:state"),
  restartBackend: () => ipcRenderer.invoke("backend:restart"),
  request: (requestPath, options = {}) =>
    ipcRenderer.invoke("backend:request", {
      path: requestPath,
      method: options.method,
      body: options.body,
    }),
  upload: (sessionId, files) => ipcRenderer.invoke("backend:upload", { sessionId, files }),
  readBinary: (requestPath) => ipcRenderer.invoke("backend:binary", requestPath),
  openExternal: (url) => ipcRenderer.invoke("app:open-external", url),
  openInBrowser: () => ipcRenderer.invoke("app:open-in-browser"),
  onBackendState: (callback) => subscribe("backend-state", callback),
  onBackendLog: (callback) => subscribe("backend-log", callback),
});
