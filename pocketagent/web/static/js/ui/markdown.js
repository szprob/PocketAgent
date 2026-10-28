// 够用的 Markdown 子集：所有文字都过 escapeHtml，标签只由这里生成，模型输出带 HTML 也不会被执行。

// 行内语法一次扫完，不再做多次 replace：替换出来的标签和属性不会被后面的正则二次加工
// （比如链接里的下划线不会被当成斜体）。规则和 desktop/renderer/markdown.js 保持一致。
const INLINE_PATTERN = new RegExp(
  [
    /`[^`\n]+`/.source, // 行内代码
    /!\[[^\]\n]*\]\(<[^>\n]+>\)/.source, // 图片：尖括号包住路径
    /!\[[^\]\n]*\]\([^)\s]+\)/.source, // 图片
    /\[[^\]\n]+\]\(https?:\/\/[^)\s]+\)/.source, // 链接只认 http(s)，避免 javascript: 之类
    /\*\*\*[^*\n]+\*\*\*/.source, // 粗斜体
    /___[^_\n]+___/.source,
    /\*\*[^*\n]+\*\*/.source, // 粗体
    /__[^_\n]+__/.source,
    /~~[^~\n]+~~/.source, // 删除线
    /\*(?!\s)[^*\n]*[^\s*]\*/.source, // 斜体：两侧不能是空白，2 * 3 * 4 不会被误判
    /(?<![A-Za-z0-9_])_(?!\s)[^_\n]*[^\s_](?![A-Za-z0-9_])/.source, // 斜体：别碰 snake_case
  ].join("|"),
  "g",
);

// 强调、链接文字里还能再嵌行内语法，给个深度上限，畸形输入不至于把调用栈打穿
const INLINE_DEPTH_LIMIT = 6;

const HEADING_PATTERN = /^(#{1,6})\s+(.*)$/;
const FENCE_PATTERN = /^\s*(`{3,}|~{3,})(.*)$/;
const LIST_PATTERN = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEPARATOR_PATTERN = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const HORIZONTAL_RULE_PATTERN = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE_PATTERN = /^>\s?/;

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function tokenKind(token) {
  if (token.startsWith("`")) {
    return "code";
  }
  if (token.startsWith("![")) {
    return "image";
  }
  if (token.startsWith("[")) {
    return "link";
  }
  if (/^(\*{3}|_{3})/.test(token)) {
    return "strongEmphasis";
  }
  if (/^(\*{2}|_{2})/.test(token)) {
    return "strong";
  }
  if (token.startsWith("~~")) {
    return "strike";
  }
  return "emphasis";
}

function tokenMarker(token) {
  return token.match(/^(\*{1,3}|_{1,3}|~~)/)[0];
}

function tokenContent(token) {
  const marker = tokenMarker(token);
  return token.slice(marker.length, token.length - marker.length);
}

function imageParts(token) {
  const labelEnd = token.indexOf("](");
  const alt = token.slice(2, labelEnd);
  let source = token.slice(labelEnd + 2, -1);
  if (source.startsWith("<") && source.endsWith(">")) {
    source = source.slice(1, -1);
  }
  return { alt, source };
}

function linkParts(token) {
  const labelEnd = token.indexOf("](");
  return { label: token.slice(1, labelEnd), url: token.slice(labelEnd + 2, -1) };
}

// 图片地址只认 http(s)，本地路径交给调用方（聊天里换成会话取图地址）；认不出来就按原文显示
function renderImage(alt, source, options) {
  let url = null;
  if (/^https?:\/\//.test(source)) {
    url = source;
  } else if (options.imageUrl) {
    url = options.imageUrl(source);
  }
  if (!url) {
    return null;
  }
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener"><img src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy"></a>`;
}

function renderToken(token, options, depth) {
  const kind = tokenKind(token);
  if (kind === "code") {
    return `<code class="md-inline-code">${escapeHtml(token.slice(1, -1))}</code>`;
  }
  if (kind === "image") {
    const { alt, source } = imageParts(token);
    return renderImage(alt, source, options) || escapeHtml(token);
  }
  if (kind === "link") {
    const { label, url } = linkParts(token);
    return `<a class="md-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">${renderInline(label, options, depth + 1)}</a>`;
  }
  const content = renderInline(tokenContent(token), options, depth + 1);
  if (kind === "strongEmphasis") {
    return `<strong><em>${content}</em></strong>`;
  }
  if (kind === "strong") {
    return `<strong>${content}</strong>`;
  }
  if (kind === "strike") {
    return `<del>${content}</del>`;
  }
  return `<em>${content}</em>`;
}

function renderInline(text, options, depth = 0) {
  const source = String(text);
  if (depth > INLINE_DEPTH_LIMIT) {
    return escapeHtml(source);
  }
  const parts = [];
  let lastIndex = 0;
  for (const match of source.matchAll(INLINE_PATTERN)) {
    parts.push(escapeHtml(source.slice(lastIndex, match.index)));
    parts.push(renderToken(match[0], options, depth));
    lastIndex = match.index + match[0].length;
  }
  parts.push(escapeHtml(source.slice(lastIndex)));
  return parts.join("");
}

function splitTableRow(line) {
  let body = line.trim();
  if (body.startsWith("|")) {
    body = body.slice(1);
  }
  // 结尾的 \| 是单元格里的竖线，不能当边框去掉
  if (body.endsWith("|") && !body.endsWith("\\|")) {
    body = body.slice(0, -1);
  }
  const cells = [];
  let current = "";
  for (let position = 0; position < body.length; position += 1) {
    const character = body[position];
    if (character === "\\" && body[position + 1] === "|") {
      current += "|";
      position += 1;
      continue;
    }
    if (character === "|") {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += character;
  }
  cells.push(current.trim());
  return cells;
}

function isTableSeparator(line) {
  return TABLE_SEPARATOR_PATTERN.test(line);
}

function tableAlignments(separatorLine) {
  return splitTableRow(separatorLine).map((cell) => {
    const alignLeft = cell.startsWith(":");
    const alignRight = cell.endsWith(":");
    if (alignLeft && alignRight) {
      return "center";
    }
    if (alignRight) {
      return "right";
    }
    return "left";
  });
}

function alignmentClass(alignment) {
  if (alignment === "center") {
    return ' class="md-align-center"';
  }
  if (alignment === "right") {
    return ' class="md-align-right"';
  }
  return "";
}

function parseFence(line) {
  const match = line.match(FENCE_PATTERN);
  if (!match) {
    return null;
  }
  return { marker: match[1], language: match[2].trim() };
}

function isFenceClose(line, marker) {
  const trimmed = line.trim();
  if (trimmed.length < marker.length) {
    return false;
  }
  const fenceCharacter = marker[0];
  return trimmed.split("").every((character) => character === fenceCharacter);
}

function parseListLine(line) {
  const match = line.match(LIST_PATTERN);
  if (!match) {
    return null;
  }
  const indent = match[1].replace(/\t/g, "    ").length;
  const ordered = /\d/.test(match[2]);
  return {
    indent,
    ordered,
    number: ordered ? Number.parseInt(match[2], 10) : 1,
    text: match[3],
  };
}

function isTableStart(lines, index) {
  return lines[index].includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1]);
}

function isHorizontalRule(line) {
  return HORIZONTAL_RULE_PATTERN.test(line);
}

function isBlockStart(lines, index) {
  const line = lines[index];
  return (
    parseFence(line) !== null ||
    HEADING_PATTERN.test(line) ||
    isHorizontalRule(line) ||
    QUOTE_PATTERN.test(line) ||
    isTableStart(lines, index) ||
    parseListLine(line) !== null
  );
}

function renderCodeBlock(language, code) {
  return (
    '<div class="code-block"><div class="code-head">' +
    `<span>${escapeHtml(language || "code")}</span>` +
    '<button type="button" class="code-copy">复制</button>' +
    `</div><pre><code>${escapeHtml(code)}</code></pre></div>`
  );
}

function readCodeBlock(lines, startIndex, fence) {
  const codeLines = [];
  let index = startIndex + 1;
  // 流式输出时代码块可能还没收尾，读到末尾就算结束
  while (index < lines.length && !isFenceClose(lines[index], fence.marker)) {
    codeLines.push(lines[index]);
    index += 1;
  }
  return { html: renderCodeBlock(fence.language, codeLines.join("\n")), nextIndex: index + 1 };
}

function readQuote(lines, startIndex, options) {
  const quoteLines = [];
  let index = startIndex;
  while (index < lines.length && QUOTE_PATTERN.test(lines[index])) {
    quoteLines.push(renderInline(lines[index].replace(QUOTE_PATTERN, ""), options));
    index += 1;
  }
  return { html: `<blockquote>${quoteLines.join("<br>")}</blockquote>`, nextIndex: index };
}

function readTable(lines, startIndex, options) {
  const headerCells = splitTableRow(lines[startIndex]);
  const alignments = tableAlignments(lines[startIndex + 1]);
  // 单元格数以表头为准：多出来的丢掉，缺的补空，行长短不一时表格不会错位
  const headHtml = headerCells
    .map((cell, columnIndex) => {
      return `<th${alignmentClass(alignments[columnIndex])}>${renderInline(cell, options)}</th>`;
    })
    .join("");
  const bodyRows = [];
  let index = startIndex + 2;
  while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
    const cells = splitTableRow(lines[index]);
    const rowCells = [];
    for (let columnIndex = 0; columnIndex < headerCells.length; columnIndex += 1) {
      const cell = cells[columnIndex] ?? "";
      rowCells.push(`<td${alignmentClass(alignments[columnIndex])}>${renderInline(cell, options)}</td>`);
    }
    bodyRows.push(`<tr>${rowCells.join("")}</tr>`);
    index += 1;
  }
  const html =
    `<div class="md-table-wrap"><table><thead><tr>${headHtml}</tr></thead>` +
    `<tbody>${bodyRows.join("")}</tbody></table></div>`;
  return { html, nextIndex: index };
}

// 有序列表两个项之间隔着空行时，跨过空行继续算同一个列表，免得中间断开成两块
function skipBlankLinesBeforeSibling(lines, index, firstMarker) {
  let lookahead = index;
  while (lookahead < lines.length && lines[lookahead].trim() === "") {
    lookahead += 1;
  }
  if (lookahead === index) {
    return index;
  }
  const marker = lookahead < lines.length ? parseListLine(lines[lookahead]) : null;
  if (marker && marker.indent === firstMarker.indent && marker.ordered === firstMarker.ordered) {
    return lookahead;
  }
  return index;
}

function readListItem(lines, startIndex, marker, options) {
  const task = marker.text.match(/^\[([ xX])\]\s+(.*)$/);
  const isTask = Boolean(task);
  let contentHtml = renderInline(isTask ? task[2] : marker.text, options);
  const nestedHtml = [];
  let index = startIndex + 1;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === "") {
      break;
    }
    const childMarker = parseListLine(line);
    if (childMarker) {
      if (childMarker.indent <= marker.indent) {
        break;
      }
      const nested = readList(lines, index, options);
      nestedHtml.push(nested.html);
      index = nested.nextIndex;
      continue;
    }
    if (isBlockStart(lines, index)) {
      break;
    }
    // 缩进或顶格的续行都算这一项的文字（CommonMark 的 lazy continuation）
    contentHtml += `<br>${renderInline(line.trim(), options)}`;
    index += 1;
  }
  const checkbox = isTask ? `<input type="checkbox" disabled${task[1].toLowerCase() === "x" ? " checked" : ""}>` : "";
  const itemClass = isTask ? ' class="md-task-item"' : "";
  return {
    html: `<li${itemClass}>${checkbox}${contentHtml}${nestedHtml.join("")}</li>`,
    isTask,
    nextIndex: index,
  };
}

function readList(lines, startIndex, options) {
  const firstMarker = parseListLine(lines[startIndex]);
  const items = [];
  let sawTask = false;
  let index = startIndex;
  while (index < lines.length) {
    const marker = parseListLine(lines[index]);
    if (!marker || marker.indent !== firstMarker.indent || marker.ordered !== firstMarker.ordered) {
      break;
    }
    const item = readListItem(lines, index, marker, options);
    items.push(item.html);
    sawTask = sawTask || item.isTask;
    index = skipBlankLinesBeforeSibling(lines, item.nextIndex, firstMarker);
  }
  const tag = firstMarker.ordered ? "ol" : "ul";
  const classAttribute = sawTask ? ' class="md-task-list"' : "";
  const startAttribute = firstMarker.ordered && firstMarker.number !== 1 ? ` start="${firstMarker.number}"` : "";
  return { html: `<${tag}${classAttribute}${startAttribute}>${items.join("")}</${tag}>`, nextIndex: index };
}

function readParagraph(lines, startIndex, options) {
  const paragraphLines = [];
  let index = startIndex;
  while (index < lines.length && lines[index].trim() !== "" && !isBlockStart(lines, index)) {
    paragraphLines.push(renderInline(lines[index], options));
    index += 1;
  }
  return { html: `<p>${paragraphLines.join("<br>")}</p>`, nextIndex: index };
}

function readBlock(lines, index, options) {
  const line = lines[index];
  const fence = parseFence(line);
  if (fence) {
    return readCodeBlock(lines, index, fence);
  }
  const heading = line.match(HEADING_PATTERN);
  if (heading) {
    const level = Math.min(heading[1].length, 4);
    return { html: `<h${level}>${renderInline(heading[2], options)}</h${level}>`, nextIndex: index + 1 };
  }
  if (isHorizontalRule(line)) {
    return { html: "<hr>", nextIndex: index + 1 };
  }
  if (QUOTE_PATTERN.test(line)) {
    return readQuote(lines, index, options);
  }
  if (isTableStart(lines, index)) {
    return readTable(lines, index, options);
  }
  if (parseListLine(line) !== null) {
    return readList(lines, index, options);
  }
  if (line.trim() === "") {
    return { html: "", nextIndex: index + 1 };
  }
  return readParagraph(lines, index, options);
}

// 消息每次都整段 innerHTML 重渲染，复制按钮每次都是新节点，所以用事件委托，不逐个绑
document.addEventListener("click", async (event) => {
  const button = event.target.closest ? event.target.closest(".code-copy") : null;
  if (!button) {
    return;
  }
  const codeElement = button.closest(".code-block")?.querySelector("pre code");
  try {
    await navigator.clipboard.writeText(codeElement ? codeElement.textContent : "");
    button.textContent = "已复制";
  } catch (clipboardError) {
    button.textContent = "复制失败";
  }
  setTimeout(() => {
    button.textContent = "复制";
  }, 1200);
});

export function renderMarkdown(source, options = {}) {
  const lines = String(source ?? "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const block = readBlock(lines, index, options);
    html.push(block.html);
    index = block.nextIndex;
  }
  return html.join("");
}
