// 调后端 REST。地址都是相对路径，挂在网关子路径下也能用。

// token 除了放 cookie 还要自己带上：cookie 在同一个主机上不分端口，桌面端或者另一个
// --web 实例用 ?token= 打开页面时会把这里的 cookie 冲掉，已经开着的页面就全 401 了。
// 所以页面地址里带了 token 就记到浏览器本地，之后每个请求、WebSocket、图片地址都带上。
const TOKEN_STORAGE_PREFIX = "pocketagent-token";

function tokenStorageKey() {
  // 按 origin + 子路径前缀分开：不同端口、网关下不同子路径的实例互不覆盖
  const basePath = new URL(".", window.location.href).pathname;
  return `${TOKEN_STORAGE_PREFIX}:${basePath}`;
}

function readToken() {
  const fromUrl = new URL(window.location.href).searchParams.get("token");
  try {
    if (fromUrl) {
      window.localStorage.setItem(tokenStorageKey(), fromUrl);
      return fromUrl;
    }
    return window.localStorage.getItem(tokenStorageKey()) || "";
  } catch (storageUnavailable) {
    return fromUrl || "";
  }
}

const token = readToken();

export function withToken(path) {
  const url = new URL(path, window.location.href);
  if (token) {
    url.searchParams.set("token", token);
  }
  return url.href;
}

export async function requestJson(path, options) {
  const response = await fetch(withToken(path), options);
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      message = JSON.parse(text).error || text;
    } catch (notJson) {
      // 不是 JSON 就原样显示
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export function sendJson(path, method, body) {
  return requestJson(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

export async function uploadFiles(sessionId, files) {
  if (files.length === 0) {
    return [];
  }
  const form = new FormData();
  for (const file of files) {
    form.append("file", file, file.name || "粘贴的图片.png");
  }
  const result = await requestJson(`api/sessions/${encodeURIComponent(sessionId)}/attachments`, {
    method: "POST",
    body: form,
  });
  return result.attachments;
}

export function attachmentUrl(sessionId, attachmentId) {
  return withToken(`api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}`);
}

export function sessionFileUrl(sessionId, path) {
  return withToken(`api/sessions/${encodeURIComponent(sessionId)}/file/raw?path=${encodeURIComponent(path)}`);
}

export function socketUrl(sessionId) {
  const url = new URL(`ws/${encodeURIComponent(sessionId)}`, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (token) {
    url.searchParams.set("token", token);
  }
  url.hash = "";
  return url.href;
}
