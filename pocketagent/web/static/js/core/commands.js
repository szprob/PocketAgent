// 斜杠命令：发给 /api/command，和命令行共用同一套（见 pocketagent/interaction/commands.py）。
// 输入以 / 开头时先走这里；返回 handled=false 说明是 /技能名 或普通消息，照常发给 agent。

import { requestJson, sendJson } from "./api.js";

// 是不是要先按命令处理：/开头、命令名里没斜杠和空白（/usr/bin 这种路径不是命令）
export function looksLikeCommand(text) {
  return /^\/[^\s/]+(\s.*)?$/.test(text.trim());
}

export async function runCommand(text, { sessionId, workspace } = {}) {
  return sendJson("api/command", "POST", {
    text,
    session_id: sessionId || null,
    workspace: workspace || null,
  });
}

// 输入框补全里要列的技能名（按工作区缓存一份，状态刷新不用反复请求）
const skillNameCache = new Map();

export async function loadSkillNames(workspace) {
  const key = workspace || "";
  if (!skillNameCache.has(key)) {
    const promise = requestJson(`api/skills?workspace=${encodeURIComponent(key)}`)
      .then((data) => (data.skills || []).filter((skill) => skill.enabled).map((skill) => skill.name))
      .catch(() => []);
    skillNameCache.set(key, promise);
  }
  return skillNameCache.get(key);
}
