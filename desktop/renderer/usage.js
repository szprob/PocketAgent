// 设置 → 用量：本机记录的 token 用量（统计块、近 30 天柱状图、按模型）和各家账户余额。

import { request } from "./api.js";
import { el, formatNumber, formatTokens } from "./dom.js";

export async function renderUsage(container) {
  container.replaceChildren(el("div", "muted", "加载中…"));
  let usage;
  let balances;
  try {
    [usage, balances] = await Promise.all([request("/api/usage"), request("/api/usage/balance")]);
  } catch (error) {
    container.replaceChildren(el("div", "error-text", `读取用量失败：${error.message}`));
    return;
  }

  container.replaceChildren();
  const stats = el("div", "usage-grid");
  for (const period of usage.periods || []) {
    const card = el("div", "usage-stat");
    card.append(el("div", "usage-stat-label", period.label));
    card.append(el("div", "usage-stat-value", formatTokens(period.total)));
    card.append(
      el(
        "div",
        "usage-stat-detail",
        `${period.calls} 次 · 输入 ${formatTokens(period.prompt)} / 输出 ${formatTokens(period.completion)}`,
      ),
    );
    if (period.cost > 0) {
      card.append(el("div", "usage-stat-detail", `费用 $${Number(period.cost).toFixed(4)}`));
    }
    stats.append(card);
  }
  container.append(el("h3", "", "本机 token 用量"), stats);

  container.append(el("h3", "", "近 30 天"));
  const chart = el("div", "usage-chart");
  const daily = usage.daily || [];
  const peak = Math.max(1, ...daily.map((day) => day.total || 0));
  for (const day of daily) {
    const bar = el("div", `usage-bar${day.total ? "" : " empty"}`);
    bar.style.height = `${Math.max(2, Math.round(((day.total || 0) / peak) * 100))}%`;
    bar.title = `${day.date}：${formatNumber(day.total)} tokens，${day.calls} 次`;
    chart.append(bar);
  }
  container.append(chart);

  container.append(el("h3", "", "按模型"));
  if ((usage.by_model || []).length === 0) {
    container.append(el("div", "panel-empty", "还没有用量记录"));
  } else {
    const table = el("table", "usage-table");
    const head = el("tr");
    for (const label of ["模型", "调用", "输入", "输出", "缓存", "费用"]) {
      head.append(el("th", "", label));
    }
    table.append(head);
    for (const model of usage.by_model) {
      const row = el("tr");
      row.append(el("td", "", model.model));
      row.append(el("td", "number", String(model.calls)));
      row.append(numberCell(model.prompt));
      row.append(numberCell(model.completion));
      row.append(numberCell(model.cached));
      row.append(el("td", "number", `$${Number(model.cost).toFixed(4)}`));
      table.append(row);
    }
    container.append(table);
  }

  container.append(el("h3", "", "账户余额"));
  const reports = balances.balances || [];
  if (reports.length === 0) {
    container.append(el("div", "panel-empty", "还没有配置模型，没有可查的余额"));
  }
  for (const report of reports) {
    const item = el("div", "balance-item");
    item.append(el("div", "balance-title", `${report.provider} · ${report.models.join("、")}`));
    if (report.ok) {
      for (const line of report.lines) {
        item.append(el("div", "balance-line", line));
      }
    } else {
      item.append(el("div", "balance-line", `查询失败：${report.error || "未知原因"}`));
    }
    container.append(item);
  }
}


function numberCell(count) {
  // 表格里的 token 数缩成 k / m / b，悬停看精确值
  const cell = el("td", "number", formatTokens(count));
  cell.title = formatNumber(count);
  return cell;
}
