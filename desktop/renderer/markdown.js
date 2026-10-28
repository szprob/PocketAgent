// 模型回复的 Markdown 渲染。全部用 DOM 节点拼，不用 innerHTML，避免模型输出带脚本。

import { binaryDataUrl, openExternal } from "./api.js";
import { el } from "./dom.js";

// 分组：1 行内代码 / 2 加粗 / 3,4 图片（尖括号路径）/ 5,6 图片 / 7,8 链接
const INLINE_PATTERN =
  /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(!\[[^\]\n]*\]\(<([^>\n]+)>\))|(!\[[^\]\n]*\]\(([^)\s]+)\))|(\[[^\]\n]+\]\(([^)\s]+)\))/g;

export function renderMarkdown(text, options = {}) {
  const fragment = document.createDocumentFragment();
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(/^\s*```(.*)$/);
    if (fence) {
      const language = fence[1].trim();
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      index += 1;
      fragment.append(buildCodeBlock(language, codeLines.join("\n")));
      continue;
    }

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const node = el(`h${Math.min(heading[1].length + 2, 6)}`);
      appendInline(node, heading[2], options);
      fragment.append(node);
      index += 1;
      continue;
    }

    // 下面三种（引用块、表格、分隔线）的规则和 web 端 ui/markdown.js 一样
    if (isQuoteLine(line)) {
      const quote = el("blockquote");
      let firstLine = true;
      while (index < lines.length && isQuoteLine(lines[index])) {
        if (!firstLine) {
          quote.append(document.createElement("br"));
        }
        appendInline(quote, lines[index].replace(/^>\s?/, ""), options);
        firstLine = false;
        index += 1;
      }
      fragment.append(quote);
      continue;
    }

    if (line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const { table, nextIndex } = buildTable(lines, index, options);
      fragment.append(table);
      index = nextIndex;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      fragment.append(el("hr"));
      index += 1;
      continue;
    }

    if (isListLine(line)) {
      const ordered = isOrderedListLine(line);
      const list = el(ordered ? "ol" : "ul");
      while (index < lines.length && isListLine(lines[index]) && isOrderedListLine(lines[index]) === ordered) {
        const itemText = lines[index].replace(ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/, "");
        const item = el("li");
        appendInline(item, itemText, options);
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    const paragraphLines = [];
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = el("p");
    paragraphLines.forEach((paragraphLine, lineIndex) => {
      if (lineIndex > 0) {
        paragraph.append(document.createElement("br"));
      }
      appendInline(paragraph, paragraphLine, options);
    });
    fragment.append(paragraph);
  }

  return fragment;
}

function isListLine(line) {
  return /^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line);
}

function isOrderedListLine(line) {
  return /^\s*\d+\.\s+/.test(line);
}

function isQuoteLine(line) {
  return /^>\s?/.test(line);
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line);
}

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

// 表头一行 + 分隔行 + 若干数据行，遇到空行或不带 | 的行结束；外面包一层，宽表格横向滚动不撑破气泡
function buildTable(lines, startIndex, options) {
  const table = el("table");
  const headRow = el("tr");
  for (const cell of splitTableRow(lines[startIndex])) {
    const headCell = el("th");
    appendInline(headCell, cell, options);
    headRow.append(headCell);
  }
  // 注意 DOM 的 append 不返回节点，不能链式写
  const head = el("thead");
  head.append(headRow);
  table.append(head);
  const body = el("tbody");
  let index = startIndex + 2;
  while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
    const row = el("tr");
    for (const cell of splitTableRow(lines[index])) {
      const dataCell = el("td");
      appendInline(dataCell, cell, options);
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

function isBlockStart(line) {
  return /^\s*```/.test(line) || /^#{1,6}\s+/.test(line) || isListLine(line) || isQuoteLine(line);
}

function appendInline(container, text, options) {
  let lastIndex = 0;
  for (const match of text.matchAll(INLINE_PATTERN)) {
    if (match.index > lastIndex) {
      container.append(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    if (match[1]) {
      container.append(el("code", "md-inline-code", match[1].slice(1, -1)));
    } else if (match[2]) {
      const strong = el("strong");
      strong.textContent = match[2].slice(2, -2);
      container.append(strong);
    } else if (match[3] || match[5]) {
      container.append(buildImage(match[3] || match[5], match[4] || match[6], options));
    } else if (match[7]) {
      container.append(buildLink(match[7], match[8]));
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    container.append(document.createTextNode(text.slice(lastIndex)));
  }
}

// 图片：http(s) 直接加载（CSP 拦不住 https），本地路径交给 options.resolveImage
// 换成会话取图接口；解析不出来就按原文显示。
function buildImage(markdownImage, source, options) {
  const alt = markdownImage.slice(2, markdownImage.indexOf("]"));
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
    return document.createTextNode(markdownImage);
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

function buildLink(markdownLink, url) {
  const label = markdownLink.slice(1, markdownLink.indexOf("]"));
  const anchor = el("a", "md-link", label);
  anchor.href = "#";
  anchor.addEventListener("click", (event) => {
    event.preventDefault();
    openExternal(url);
  });
  return anchor;
}

function buildCodeBlock(language, code) {
  const wrapper = el("div", "code-block");
  const head = el("div", "code-head");
  head.append(el("span", "", language || "code"));

  const copyButton = el("button", "", "复制");
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
