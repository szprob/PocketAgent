// 模型回复的 Markdown 渲染。全部用 DOM 节点拼，不用 innerHTML，避免模型输出带脚本。
// 块和行内的规则和 web 端 ui/markdown.js 一样，改一边记得改另一边。

import { binaryDataUrl, openExternal } from "./api.js";
import { el } from "./dom.js";

// 行内语法一次扫完，替换出来的节点不会被后面的规则二次加工；
// 顺序有讲究：粗斜体在粗体前、粗体在斜体前，否则 *** 会被拆成零散的星号。
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
    return "md-align-center";
  }
  if (alignment === "right") {
    return "md-align-right";
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

function buildTable(lines, startIndex, options) {
  const table = el("table");
  const headerCells = splitTableRow(lines[startIndex]);
  const alignments = tableAlignments(lines[startIndex + 1]);
  const headRow = el("tr");
  headerCells.forEach((cell, columnIndex) => {
    const headCell = el("th", alignmentClass(alignments[columnIndex]));
    appendInline(headCell, cell, options);
    headRow.append(headCell);
  });

  // 注意 DOM 的 append 不返回节点，不能链式写
  const head = el("thead");
  head.append(headRow);
  table.append(head);

  const body = el("tbody");
  let index = startIndex + 2;
  while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
    const cells = splitTableRow(lines[index]);
    const row = el("tr");
    // 单元格数以表头为准：多出来的丢掉，缺的补空，行长短不一时表格不会错位
    for (let columnIndex = 0; columnIndex < headerCells.length; columnIndex += 1) {
      const dataCell = el("td", alignmentClass(alignments[columnIndex]));
      appendInline(dataCell, cells[columnIndex] ?? "", options);
      row.append(dataCell);
    }
    body.append(row);
    index += 1;
  }
  table.append(body);

  const wrapper = el("div", "md-table-wrap");
  wrapper.append(table);
  return { table: wrapper, nextIndex: index };
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
  const item = el("li", isTask ? "md-task-item" : "");
  if (isTask) {
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.disabled = true;
    checkbox.checked = task[1].toLowerCase() === "x";
    item.append(checkbox);
  }
  appendInline(item, isTask ? task[2] : marker.text, options);

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
      item.append(nested.node);
      index = nested.nextIndex;
      continue;
    }
    if (isBlockStart(lines, index)) {
      break;
    }
    // 缩进或顶格的续行都算这一项的文字（CommonMark 的 lazy continuation）
    item.append(document.createElement("br"));
    appendInline(item, line.trim(), options);
    index += 1;
  }
  return { node: item, isTask, nextIndex: index };
}

function readList(lines, startIndex, options) {
  const firstMarker = parseListLine(lines[startIndex]);
  const list = el(firstMarker.ordered ? "ol" : "ul");
  let sawTask = false;
  let index = startIndex;
  while (index < lines.length) {
    const marker = parseListLine(lines[index]);
    if (!marker || marker.indent !== firstMarker.indent || marker.ordered !== firstMarker.ordered) {
      break;
    }
    const item = readListItem(lines, index, marker, options);
    list.append(item.node);
    sawTask = sawTask || item.isTask;
    index = skipBlankLinesBeforeSibling(lines, item.nextIndex, firstMarker);
  }
  if (sawTask) {
    list.className = "md-task-list";
  }
  if (firstMarker.ordered && firstMarker.number !== 1) {
    list.setAttribute("start", String(firstMarker.number));
  }
  return { node: list, nextIndex: index };
}

function buildCodeBlock(language, code) {
  const wrapper = el("div", "code-block");
  const head = el("div", "code-head");
  head.append(el("span", "", language || "code"));

  const copyButton = el("button", "code-copy", "复制");
  copyButton.type = "button";
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(code);
      copyButton.textContent = "已复制";
      setTimeout(() => {
        copyButton.textContent = "复制";
      }, 1200);
    } catch (clipboardError) {
      copyButton.textContent = "复制失败";
    }
  });
  head.append(copyButton);

  const pre = el("pre");
  pre.append(el("code", "", code));
  wrapper.append(head, pre);
  return wrapper;
}

function readCodeBlock(lines, startIndex, fence) {
  const codeLines = [];
  let index = startIndex + 1;
  // 流式输出时代码块可能还没收尾，读到末尾就算结束
  while (index < lines.length && !isFenceClose(lines[index], fence.marker)) {
    codeLines.push(lines[index]);
    index += 1;
  }
  return { node: buildCodeBlock(fence.language, codeLines.join("\n")), nextIndex: index + 1 };
}

// 图片：http(s) 直接加载（CSP 拦不住 https），本地路径交给 options.resolveImage
// 换成会话取图接口；解析不出来就按原文显示。
function buildImage(token, options) {
  const { alt, source } = imageParts(token);
  const image = el("img", "md-image");
  image.alt = alt;
  image.loading = "lazy";

  if (/^https?:\/\//.test(source)) {
    image.src = source;
    if (options.onOpenExternalImage) {
      image.addEventListener("click", () => options.onOpenExternalImage(source));
    }
    return image;
  }

  const apiPath = options.resolveImage ? options.resolveImage(source) : null;
  if (!apiPath) {
    return document.createTextNode(token);
  }
  options.onImage?.(apiPath);
  if (options.onOpenImage) {
    image.addEventListener("click", () => options.onOpenImage(source));
  }
  binaryDataUrl(apiPath)
    .then((url) => {
      image.src = url;
    })
    .catch(() => {
      image.remove();
    });
  return image;
}

function buildLink(label, url, options, depth) {
  const anchor = el("a", "md-link");
  anchor.href = "#";
  anchor.addEventListener("click", (event) => {
    event.preventDefault();
    openExternal(url);
  });
  appendInline(anchor, label, options, depth + 1);
  return anchor;
}

function appendEmphasis(container, kind, content, options, depth) {
  if (kind === "strongEmphasis") {
    const strong = el("strong");
    const emphasis = el("em");
    appendInline(emphasis, content, options, depth + 1);
    strong.append(emphasis);
    container.append(strong);
    return;
  }
  const tag = kind === "strong" ? "strong" : kind === "strike" ? "del" : "em";
  const node = el(tag);
  appendInline(node, content, options, depth + 1);
  container.append(node);
}

function appendInlineToken(container, token, options, depth) {
  const kind = tokenKind(token);
  if (kind === "code") {
    container.append(el("code", "md-inline-code", token.slice(1, -1)));
    return;
  }
  if (kind === "image") {
    container.append(buildImage(token, options));
    return;
  }
  if (kind === "link") {
    const { label, url } = linkParts(token);
    container.append(buildLink(label, url, options, depth));
    return;
  }
  appendEmphasis(container, kind, tokenContent(token), options, depth);
}

function appendInline(container, text, options, depth = 0) {
  const source = String(text);
  if (depth > INLINE_DEPTH_LIMIT) {
    container.append(document.createTextNode(source));
    return;
  }
  let lastIndex = 0;
  for (const match of source.matchAll(INLINE_PATTERN)) {
    if (match.index > lastIndex) {
      container.append(document.createTextNode(source.slice(lastIndex, match.index)));
    }
    appendInlineToken(container, match[0], options, depth);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < source.length) {
    container.append(document.createTextNode(source.slice(lastIndex)));
  }
}

function readQuote(lines, startIndex, options) {
  const quote = el("blockquote");
  let firstLine = true;
  let index = startIndex;
  while (index < lines.length && QUOTE_PATTERN.test(lines[index])) {
    if (!firstLine) {
      quote.append(document.createElement("br"));
    }
    appendInline(quote, lines[index].replace(QUOTE_PATTERN, ""), options);
    firstLine = false;
    index += 1;
  }
  return { node: quote, nextIndex: index };
}

function readParagraph(lines, startIndex, options) {
  const paragraph = el("p");
  let firstLine = true;
  let index = startIndex;
  while (index < lines.length && lines[index].trim() !== "" && !isBlockStart(lines, index)) {
    if (!firstLine) {
      paragraph.append(document.createElement("br"));
    }
    appendInline(paragraph, lines[index], options);
    firstLine = false;
    index += 1;
  }
  return { node: paragraph, nextIndex: index };
}

export function renderMarkdown(text, options = {}) {
  const fragment = document.createDocumentFragment();
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    const fence = parseFence(line);
    if (fence) {
      const block = readCodeBlock(lines, index, fence);
      fragment.append(block.node);
      index = block.nextIndex;
      continue;
    }

    const heading = line.match(HEADING_PATTERN);
    if (heading) {
      const node = el(`h${Math.min(heading[1].length, 4)}`);
      appendInline(node, heading[2], options);
      fragment.append(node);
      index += 1;
      continue;
    }

    if (isHorizontalRule(line)) {
      fragment.append(el("hr"));
      index += 1;
      continue;
    }

    if (QUOTE_PATTERN.test(line)) {
      const block = readQuote(lines, index, options);
      fragment.append(block.node);
      index = block.nextIndex;
      continue;
    }

    if (isTableStart(lines, index)) {
      const block = buildTable(lines, index, options);
      fragment.append(block.table);
      index = block.nextIndex;
      continue;
    }

    if (parseListLine(line) !== null) {
      const block = readList(lines, index, options);
      fragment.append(block.node);
      index = block.nextIndex;
      continue;
    }

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const paragraph = readParagraph(lines, index, options);
    fragment.append(paragraph.node);
    index = paragraph.nextIndex;
  }

  return fragment;
}
