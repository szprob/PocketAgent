// 右侧栏：任务概要（token 用量、上下文）、文件变更（点开看 diff、可撤销）、产出、检查点。

import { binaryDataUrl, request, sendJson, sessionFilePath } from "./api.js";
import { button, el, formatClock, formatNumber, formatTokens, isImagePath, renderDiff, workspaceName } from "./dom.js";
import { icon } from "./icons.js";
import { openFilePreview } from "./filepreview.js";

const STATUS_LABELS = { added: "新建", modified: "修改", deleted: "删除" };
const CHECKPOINT_KINDS = { baseline: "任务开始", turn: "这一轮之前", safety: "撤销 / 恢复之前" };

export class RightPanel {
  constructor({ overviewCard, changeList, changesCount, outputList, checkpointList, refreshButton }) {
    this.overviewCard = overviewCard;
    this.changeList = changeList;
    this.outputList = outputList;
    this.checkpointList = checkpointList;
    this.changesCount = changesCount;

    this.taskId = null;
    this.status = null;
    this.usage = null;
    this.turns = 0;
    this.toolCalls = 0;
    this.latestReply = "";
    this.updatedAt = null;
    this.changes = [];
    this.checkpoints = [];
    this.checkpointsEnabled = true;
    this.checkpointsDisabledReason = "";
    this.expandedPath = null;
    this.refreshTimer = null;
    this.taskRefreshTimer = null;

    refreshButton.append(icon("refresh"));
    refreshButton.addEventListener("click", () => this.refreshChanges());
    this.renderOverview();
    this.renderChanges();
  }

  showTask(taskId) {
    this.taskId = taskId;
    this.status = null;
    this.usage = null;
    this.turns = 0;
    this.toolCalls = 0;
    this.latestReply = "";
    this.updatedAt = null;
    this.changes = [];
    this.checkpoints = [];
    this.checkpointsEnabled = true;
    this.checkpointsDisabledReason = "";
    this.expandedPath = null;
    this.renderOverview();
    this.renderChanges();
    this.refreshTask();
  }

  clear() {
    this.taskId = null;
    this.status = null;
    this.usage = null;
    this.turns = 0;
    this.toolCalls = 0;
    this.latestReply = "";
    this.updatedAt = null;
    this.changes = [];
    this.checkpoints = [];
    this.renderOverview();
    this.renderChanges();
  }

  updateStatus(status) {
    this.status = status;
    if (status.updated_at) {
      this.updatedAt = status.updated_at;
    }
    this.renderOverview();
  }

  handleTurnEnd(finalText) {
    if (finalText) {
      this.latestReply = finalText;
    }
    this.updatedAt = new Date().toISOString();
    this.refreshTask();
  }

  handleModelResponded() {
    clearTimeout(this.taskRefreshTimer);
    this.taskRefreshTimer = setTimeout(() => this.refreshTask(), 600);
  }

  async refreshTask() {
    const taskId = this.taskId;
    if (!taskId) {
      return;
    }
    try {
      const overview = await request(`/api/sessions/${encodeURIComponent(taskId)}/overview`);
      if (taskId !== this.taskId) {
        return;
      }
      this.usage = overview.usage;
      this.turns = overview.turns ?? 0;
      this.toolCalls = overview.tool_calls ?? 0;
      this.latestReply = overview.latest_reply || this.latestReply;
      this.updatedAt = overview.updated_at || this.updatedAt;
      this.status = { ...(this.status || {}), ...overview };
    } catch (error) {
      return;
    }
    this.renderOverview();
    await this.refreshChanges();
  }

  scheduleChangesRefresh() {
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refreshChanges(), 400);
  }

  async refreshChanges() {
    const taskId = this.taskId;
    if (!taskId) {
      return;
    }
    try {
      const result = await request(`/api/sessions/${encodeURIComponent(taskId)}/changes`);
      if (taskId !== this.taskId) {
        return;
      }
      this.applyChangesPayload(result);
      this.renderChanges();
    } catch (error) {
      // 任务刚被删掉之类，忽略
    }
  }

  applyChangesPayload(result) {
    this.changes = result.changes || [];
    this.checkpoints = result.checkpoints || [];
    this.checkpointsEnabled = result.enabled;
    this.checkpointsDisabledReason = result.disabled_reason || "";
  }

  fullPath(relativePath) {
    const workspace = this.status?.workspace || "";
    return workspace ? `${workspace.replace(/\/$/, "")}/${relativePath}` : relativePath;
  }

  // ---- 概要 ----

  renderOverview() {
    const card = this.overviewCard;
    card.replaceChildren();
    const status = this.status;
    if (!status) {
      card.append(el("div", "muted", "加载中…"));
      return;
    }
    const head = el("div", "overview-head");
    head.append(icon("sparkle", "overview-icon"));
    head.append(el("span", "overview-updated", this.updatedAt ? `更新于 ${formatClock(this.updatedAt)}` : "新任务"));
    card.append(head, el("div", "overview-title", status.title || "任务"));

    if (this.latestReply) {
      card.append(el("div", "overview-reply", this.latestReply.replace(/[#*`>|]/g, "").slice(0, 160)));
    }

    const rows = el("div", "overview-rows");
    rows.append(
      this.overviewRow(
        "工作区",
        status.ephemeral ? "临时任务" : workspaceName(status.workspace),
        status.ephemeral ? `临时目录 ${status.workspace}` : status.workspace,
      ),
      this.overviewRow("模型", status.model),
    );

    if (this.usage) {
      const usage = this.usage;
      rows.append(
        this.overviewRow(
          "本任务用量",
          `输入 ${formatTokens(usage.prompt)} + 输出 ${formatTokens(usage.completion)}`,
          `输入 ${formatNumber(usage.prompt)} + 输出 ${formatNumber(usage.completion)}，共 ${formatNumber(usage.total)} tokens，${usage.calls} 次调用模型，思考 ${formatNumber(usage.reasoning)}`,
        ),
      );
      rows.append(
        this.overviewRow(
          "总轮次",
          `${this.turns} 轮 · 工具调用 ${this.toolCalls} 次`,
          "用户说一句话到 agent 停下来算一轮（干活时的插话不算）；工具调用是这个任务里模型调工具的总次数，被拒绝的也算，子 agent 内部的不算",
        ),
      );
      rows.append(this.overviewRow("缓存命中", cacheHitText(usage), "输入里命中缓存的比例；接口不报就是 0"));
      if (usage.cost > 0) {
        rows.append(this.overviewRow("费用", `$${Number(usage.cost).toFixed(4)}`, "接口直接报的费用"));
      }
    }

    const percent = status.context_window ? Math.round((status.context_tokens / status.context_window) * 100) : 0;
    rows.append(
      this.overviewRow(
        "上下文",
        `${percent}%（约 ${formatTokens(status.context_tokens)} / ${formatTokens(status.context_window)}）`,
      ),
    );
    card.append(rows);
  }

  overviewRow(label, value, title, detail) {
    const row = el("div", "overview-row");
    const values = el("div", "overview-values");
    values.append(el("span", "overview-value", value || "—"));
    if (detail) {
      values.append(el("span", "overview-detail", detail));
    }
    if (title) {
      values.title = title;
    }
    row.append(el("span", "overview-label", label), values);
    return row;
  }

  // ---- 变更 / 产出 ----

  renderChanges() {
    this.changeList.replaceChildren();
    this.outputList.replaceChildren();
    this.changesCount.textContent = this.changes.length ? String(this.changes.length) : "";

    if (!this.checkpointsEnabled) {
      this.changeList.append(el("div", "panel-empty", this.checkpointsDisabledReason || "检查点关掉了，看不到变更"));
    } else if (this.changes.length === 0) {
      this.changeList.append(el("div", "panel-empty", "从任务开始到现在，工作区里还没有文件变化"));
    }
    for (const change of this.changes) {
      this.changeList.append(this.renderChangeRow(change));
    }

    const outputs = this.changes.filter((change) => change.status === "added");
    if (outputs.length === 0) {
      this.outputList.append(el("div", "panel-empty", "新建的文件会列在这里"));
    }
    for (const output of outputs) {
      this.outputList.append(this.renderOutputRow(output));
    }
    this.renderCheckpoints();
  }

  renderChangeRow(change) {
    const wrapper = el("div", "change-item");
    const row = el("button", "change-row");
    row.type = "button";
    row.title = this.fullPath(change.path);
    const counts = el("span", "change-counts");
    if (change.binary) {
      counts.textContent = "二进制";
    } else {
      counts.append(el("span", "count-add", `+${change.additions}`), el("span", "count-remove", `−${change.deletions}`));
    }
    row.append(
      el("span", `change-status ${change.status}`, STATUS_LABELS[change.status] || change.status),
      el("span", "change-path", change.path),
      counts,
    );
    row.addEventListener("click", () => {
      this.expandedPath = this.expandedPath === change.path ? null : change.path;
      this.renderChanges();
    });
    wrapper.append(row);
    if (this.expandedPath === change.path) {
      wrapper.append(this.renderDiffPanel(change));
    }
    return wrapper;
  }

  renderDiffPanel(change) {
    const container = el("div", "change-diff");
    container.append(el("div", "muted", "加载 diff…"));
    const taskId = encodeURIComponent(this.taskId);
    request(`/api/sessions/${taskId}/changes/diff?path=${encodeURIComponent(change.path)}`)
      .then((result) => {
        const revert = button("ghost-button small", "撤销这个文件的改动", "undo");
        revert.addEventListener("click", () => this.revertChange(change));
        container.replaceChildren(renderDiff(result.diff), revert);
      })
      .catch((error) => container.replaceChildren(el("div", "error-text", error.message)));
    return container;
  }

  async revertChange(change) {
    const action = change.status === "added" ? "删除这个新建的文件" : "把文件恢复成任务开始时的样子";
    if (!window.confirm(`${action}？\n${change.path}\n\n撤销前会先拍一张检查点，撤销本身也能撤销。`)) {
      return;
    }
    try {
      const result = await sendJson(`/api/sessions/${encodeURIComponent(this.taskId)}/changes/revert`, "POST", {
        path: change.path,
      });
      this.applyChangesPayload(result);
      this.expandedPath = null;
      this.renderChanges();
      this.onFilesChanged?.();
    } catch (error) {
      window.alert(`撤销失败：${error.message}`);
    }
  }

  renderOutputRow(output) {
    const row = el("div", "output-row");
    const meta = el("div", "output-meta");
    meta.append(icon("file", "output-icon"), el("span", "output-path", output.path));
    if (isImagePath(output.path)) {
      row.classList.add("with-image");
      const link = el("a", "output-thumb");
      link.href = "#";
      link.title = `打开原图：${output.path}`;
      const image = el("img");
      image.alt = output.path;
      link.append(image);
      link.addEventListener("click", (event) => {
        event.preventDefault();
        if (this.status?.workspace && !this.status?.ephemeral) {
          openFilePreview(this.status.workspace, output.path);
        }
      });
      binaryDataUrl(sessionFilePath(this.taskId, output.path))
        .then((url) => {
          image.src = url;
        })
        .catch(() => {
          link.remove();
          row.classList.remove("with-image");
        });
      row.append(link, meta);
    } else {
      row.append(meta);
    }
    row.title = this.fullPath(output.path);
    return row;
  }

  // ---- 检查点 ----

  renderCheckpoints() {
    this.checkpointList.replaceChildren();
    if (this.checkpoints.length === 0) {
      this.checkpointList.append(el("div", "panel-empty", "每一轮开始前会给工作区拍一张，可以整体恢复到那时"));
      return;
    }
    for (const checkpoint of [...this.checkpoints].reverse()) {
      this.checkpointList.append(this.renderCheckpointRow(checkpoint));
    }
  }

  renderCheckpointRow(checkpoint) {
    const row = el("div", "checkpoint-row");
    const text = el("div", "checkpoint-text");
    text.append(
      el("div", "checkpoint-label", checkpoint.label),
      el("div", "checkpoint-meta", `${CHECKPOINT_KINDS[checkpoint.kind] || checkpoint.kind} · ${formatClock(checkpoint.created_at)}`),
    );
    text.title = `${checkpoint.label}\n${checkpoint.commit.slice(0, 12)}`;
    const restore = button("ghost-button small", "恢复到这里");
    restore.addEventListener("click", () => this.restoreCheckpoint(checkpoint));
    row.append(text, restore);
    return row;
  }

  async restoreCheckpoint(checkpoint) {
    const message = `把整个工作区恢复到「${checkpoint.label}」时的样子？\n\n恢复前会先拍一张「恢复之前的样子」，恢复本身也能撤销。`;
    if (!window.confirm(message)) {
      return;
    }
    try {
      const result = await sendJson(`/api/sessions/${encodeURIComponent(this.taskId)}/checkpoints/restore`, "POST", {
        commit: checkpoint.commit,
      });
      this.applyChangesPayload(result);
      this.expandedPath = null;
      this.renderChanges();
      this.onFilesChanged?.();
    } catch (error) {
      window.alert(`恢复失败：${error.message}`);
    }
  }
}

function cacheHitText(usage) {
  if (usage.cache_hit_rate === null || usage.cache_hit_rate === undefined) {
    return "—";
  }
  return `${Math.round(usage.cache_hit_rate * 100)}%（${formatTokens(usage.cached)}）`;
}
