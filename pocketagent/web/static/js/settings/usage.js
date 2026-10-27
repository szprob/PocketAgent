// 设置 → 用量：本机记录的 token 用量（统计块、近 30 天柱状图、按模型）和各家账户余额。
// 图表两个系列（输入 / 输出）用参考色板的 1、2 号色，亮暗两套都过了色板校验。

import { requestJson } from "../core/api.js";
import { button, el, formatNumber, formatTokens } from "../ui/dom.js";

const CHART_HEIGHT = 160;
const X_LABEL_EVERY_DAYS = 5;

export async function renderUsage(container) {
  container.replaceChildren(el("div", "muted", "加载中…"));
  let summary;
  try {
    summary = await requestJson("api/usage");
  } catch (error) {
    container.replaceChildren(el("div", "error-text", `读取用量失败：${error.message}`));
    return;
  }
  container.replaceChildren(
    renderTiles(summary.periods),
    renderDailyChart(summary.daily),
    renderModelTable(summary.by_model),
    renderBalanceSection()
  );
}

// ---- 统计块 ----

function renderTiles(periods) {
  const tiles = el("div", "usage-tiles");
  for (const period of periods) {
    const tile = el("div", "usage-tile");
    const detail = `输入 ${formatTokens(period.prompt)} · 输出 ${formatTokens(period.completion)} · ${period.calls} 次`;
    tile.append(
      el("div", "tile-label", period.label),
      el("div", "tile-value", formatTokens(period.total)),
      el("div", "tile-detail", detail),
      el("div", "tile-detail", `缓存命中 ${percentText(period.cache_hit_rate)}${period.cost > 0 ? ` · $${period.cost.toFixed(3)}` : ""}`)
    );
    if (period.estimated_calls) {
      tile.appendChild(el("div", "tile-note", `其中 ${period.estimated_calls} 次接口没报用量，按字数估算`));
    }
    tiles.appendChild(tile);
  }
  return tiles;
}

function percentText(rate) {
  return rate === null || rate === undefined ? "—" : `${Math.round(rate * 100)}%`;
}

// ---- 近 30 天柱状图 ----

function niceMaximum(value) {
  if (value <= 0) {
    return 1;
  }
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * magnitude >= value) {
      return step * magnitude;
    }
  }
  return 10 * magnitude;
}

function legendItem(seriesClass, label) {
  const item = el("span", "legend-item");
  item.append(el("span", `legend-swatch ${seriesClass}`), el("span", "", label));
  return item;
}

function renderDailyChart(daily) {
  const section = el("section", "usage-section");
  const header = el("div", "usage-section-header");
  const legend = el("div", "chart-legend");
  legend.append(legendItem("series-input", "输入 tokens"), legendItem("series-output", "输出 tokens"));
  header.append(el("h3", "", "近 30 天每日用量"), legend);
  section.appendChild(header);

  const maximum = niceMaximum(Math.max(...daily.map((day) => day.total), 0));
  const chart = el("div", "usage-chart");
  const plot = el("div", "chart-plot");
  plot.style.height = `${CHART_HEIGHT}px`;
  for (const fraction of [1, 0.5, 0]) {
    const gridLine = el("div", "chart-grid-line");
    gridLine.style.bottom = `${fraction * 100}%`;
    gridLine.appendChild(el("span", "chart-tick", formatTokens(maximum * fraction)));
    plot.appendChild(gridLine);
  }
  const columns = el("div", "chart-columns");
  const tooltip = el("div", "chart-tooltip hidden");
  daily.forEach((day) => columns.appendChild(renderColumn(day, maximum, tooltip, plot)));
  plot.append(columns, tooltip);

  const xLabels = el("div", "chart-x-labels");
  daily.forEach((day, index) => {
    const isLabelled = (daily.length - 1 - index) % X_LABEL_EVERY_DAYS === 0;
    xLabels.appendChild(el("span", "chart-x-label", isLabelled ? day.date.slice(5) : ""));
  });
  chart.append(plot, xLabels);
  section.append(chart, renderDailyTable(daily));
  return section;
}

function renderColumn(day, maximum, tooltip, plot) {
  const column = el("div", "chart-column");
  column.tabIndex = 0;
  column.setAttribute("aria-label", `${day.date} 输入 ${day.prompt} 输出 ${day.completion}`);
  const stack = el("div", "chart-stack");
  // 输出叠在上面，带圆角的数据端；输入贴着基线
  if (day.completion > 0) {
    const output = el("div", "chart-segment series-output");
    output.style.height = `${(day.completion / maximum) * CHART_HEIGHT}px`;
    stack.appendChild(output);
  }
  if (day.prompt > 0) {
    const input = el("div", "chart-segment series-input");
    input.style.height = `${(day.prompt / maximum) * CHART_HEIGHT}px`;
    stack.appendChild(input);
  }
  column.appendChild(stack);
  const showTooltip = () => {
    tooltip.replaceChildren(
      el("div", "tooltip-title", day.date),
      tooltipRow("series-input", "输入", day.prompt),
      tooltipRow("series-output", "输出", day.completion),
      el("div", "tooltip-total", `合计 ${formatNumber(day.total)} · ${day.calls} 次调用 · 缓存命中 ${percentText(day.cache_hit_rate)}`)
    );
    tooltip.classList.remove("hidden");
    const center = column.offsetLeft + column.offsetWidth / 2;
    const left = Math.min(Math.max(center - tooltip.offsetWidth / 2, 0), plot.clientWidth - tooltip.offsetWidth);
    tooltip.style.left = `${left}px`;
    column.classList.add("hovered");
  };
  const hideTooltip = () => {
    tooltip.classList.add("hidden");
    column.classList.remove("hovered");
  };
  column.addEventListener("mouseenter", showTooltip);
  column.addEventListener("focus", showTooltip);
  column.addEventListener("mouseleave", hideTooltip);
  column.addEventListener("blur", hideTooltip);
  return column;
}

function tooltipRow(seriesClass, label, value) {
  const row = el("div", "tooltip-row");
  row.append(el("span", `legend-swatch ${seriesClass}`), el("span", "", label), el("span", "tooltip-value", formatNumber(value)));
  return row;
}

function renderDailyTable(daily) {
  const details = el("details", "usage-details");
  details.appendChild(el("summary", "", "查看每日明细表"));
  const rows = daily
    .filter((day) => day.calls > 0)
    .reverse()
    .map((day) => [day.date, day.calls, day.prompt, day.completion, day.total, percentText(day.cache_hit_rate)]);
  details.appendChild(dataTable(["日期", "调用", "输入", "输出", "合计", "缓存命中"], rows, "这 30 天还没有用量"));
  return details;
}

// ---- 按模型 ----

function renderModelTable(byModel) {
  const section = el("section", "usage-section");
  section.appendChild(el("h3", "", "按模型（全部时间）"));
  const rows = byModel.map((item) => [
    item.model,
    item.calls,
    item.prompt,
    item.completion,
    item.total,
    percentText(item.cache_hit_rate),
    item.cost > 0 ? `$${item.cost.toFixed(4)}` : "—",
  ]);
  section.appendChild(dataTable(["模型", "调用", "输入", "输出", "合计", "缓存命中", "费用"], rows, "还没有用量记录"));
  return section;
}

function dataTable(headers, rows, emptyText) {
  if (rows.length === 0) {
    return el("div", "muted", emptyText);
  }
  const table = el("table", "data-table");
  // 第一列是名字（模型、日期）左对齐，后面都是数值类，表头和内容一起右对齐
  const head = el("tr");
  headers.forEach((header, index) => head.appendChild(el("th", index > 0 ? "number" : "", header)));
  table.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  for (const row of rows) {
    const tableRow = el("tr");
    row.forEach((cell, index) => {
      const text = typeof cell === "number" ? formatNumber(cell) : String(cell);
      tableRow.appendChild(el("td", index > 0 ? "number" : "", text));
    });
    body.appendChild(tableRow);
  }
  table.appendChild(body);
  return table;
}

// ---- 账户余额 ----

function renderBalanceSection() {
  const section = el("section", "usage-section");
  const header = el("div", "usage-section-header");
  const queryButton = button("ghost-button small", "查询余额");
  header.append(el("h3", "", "账户余额"), queryButton);
  const result = el("div", "balance-list");
  result.appendChild(el("div", "muted", "向各家接口查询账户余额和已用额度（DeepSeek、OpenRouter、OpenAI 兼容网关支持；智谱没有公开接口）。"));
  queryButton.addEventListener("click", async () => {
    queryButton.disabled = true;
    result.replaceChildren(el("div", "muted", "查询中…"));
    try {
      const { balances } = await requestJson("api/usage/balance");
      result.replaceChildren(...balances.map(renderBalance));
    } catch (error) {
      result.replaceChildren(el("div", "error-text", error.message));
    } finally {
      queryButton.disabled = false;
    }
  });
  section.append(header, result);
  return section;
}

function renderBalance(report) {
  const card = el("div", "balance-card");
  card.append(el("div", "balance-provider", report.provider), el("div", "balance-models", report.models.join("、")));
  if (report.ok) {
    report.lines.forEach((line) => card.appendChild(el("div", "balance-line", line)));
  } else {
    card.appendChild(el("div", "balance-error", report.error));
  }
  return card;
}
