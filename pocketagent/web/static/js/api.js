// 调后端 REST。地址都是相对路径，挂在网关子路径下也能用。

export async function requestJson(path, options) {
  const response = await fetch(path, options);
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
  return `api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(attachmentId)}`;
}

export function socketUrl(sessionId) {
  const url = new URL(`ws/${encodeURIComponent(sessionId)}`, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.hash = "";
  return url.href;
}
