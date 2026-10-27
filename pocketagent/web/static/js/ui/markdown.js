// 够用的 Markdown 子集：先整体转义再加标签，不会注入 HTML。

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const IMAGE_ANGLE_PATTERN = /!\[([^\]]*)\]\(&lt;([\s\S]*?)&gt;\)/g;
const IMAGE_PATTERN = /!\[([^\]]*)\]\(([^)\s]+)\)/g;

function renderInlineText(escapedText, options) {
  return escapedText
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(IMAGE_ANGLE_PATTERN, (whole, alt, src) => renderImage(alt, src, options) || whole)
    .replace(IMAGE_PATTERN, (whole, alt, src) => renderImage(alt, src, options) || whole)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function unescapeHtml(text) {
  return text.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"');
}

// 图片地址只认 http(s)，本地路径交给调用方（聊天里换成会话取图地址）；认不出来就按原文显示
function renderImage(alt, escapedSource, options) {
  const source = unescapeHtml(escapedSource);
  let url = null;
  if (/^https?:\/\//.test(source)) {
    url = source;
  } else if (options.imageUrl) {
    url = options.imageUrl(source);
  }
  if (!url) {
    return null;
  }
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener"><img src="${escapeHtml(url)}" alt="${alt}" loading="lazy"></a>`;
}

function renderInline(text, options) {
  // 按反引号切开：奇数段是行内代码，不做加粗、链接处理；最后一个反引号没配对时当普通文字
  const segments = escapeHtml(text).split("`");
  const parts = [];
  segments.forEach((segment, index) => {
    const isOddSegment = index % 2 === 1;
    const hasClosingBacktick = index < segments.length - 1;
    if (isOddSegment && hasClosingBacktick) {
      parts.push(`<code>${segment}</code>`);
    } else if (isOddSegment) {
      parts.push(renderInlineText("`" + segment, options));
    } else {
      parts.push(renderInlineText(segment, options));
    }
  });
  return parts.join("");
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

function isBlockStart(line) {
  return /^```/.test(line) || /^#{1,6}\s/.test(line) || /^\s*([-*]|\d+\.)\s+/.test(line) || /^>\s?/.test(line);
}

function readCodeBlock(lines, startIndex) {
  const codeLines = [];
  let index = startIndex + 1;
  // 流式输出时代码块可能还没收尾，读到末尾就算结束
  while (index < lines.length && !/^```/.test(lines[index])) {
    codeLines.push(lines[index]);
    index += 1;
  }
  return { html: `<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`, nextIndex: index + 1 };
}

function readList(lines, startIndex, options) {
  const ordered = /^\s*\d+\.\s+/.test(lines[startIndex]);
  const pattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
  const items = [];
  let index = startIndex;
  while (index < lines.length && pattern.test(lines[index])) {
    items.push(`<li>${renderInline(lines[index].replace(pattern, ""), options)}</li>`);
    index += 1;
  }
  const tag = ordered ? "ol" : "ul";
  return { html: `<${tag}>${items.join("")}</${tag}>`, nextIndex: index };
}

function readQuote(lines, startIndex, options) {
  const quoteLines = [];
  let index = startIndex;
  while (index < lines.length && /^>\s?/.test(lines[index])) {
    quoteLines.push(renderInline(lines[index].replace(/^>\s?/, ""), options));
    index += 1;
  }
  return { html: `<blockquote>${quoteLines.join("<br>")}</blockquote>`, nextIndex: index };
}

function readTable(lines, startIndex, options) {
  const headerCells = splitTableRow(lines[startIndex]).map((cell) => `<th>${renderInline(cell, options)}</th>`);
  const bodyRows = [];
  let index = startIndex + 2;
  while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
    const cells = splitTableRow(lines[index]).map((cell) => `<td>${renderInline(cell, options)}</td>`);
    bodyRows.push(`<tr>${cells.join("")}</tr>`);
    index += 1;
  }
  const html = `<table><thead><tr>${headerCells.join("")}</tr></thead><tbody>${bodyRows.join("")}</tbody></table>`;
  return { html, nextIndex: index };
}

function readParagraph(lines, startIndex, options) {
  const paragraphLines = [];
  let index = startIndex;
  while (index < lines.length && lines[index].trim() !== "" && !isBlockStart(lines[index])) {
    paragraphLines.push(renderInline(lines[index], options));
    index += 1;
  }
  return { html: `<p>${paragraphLines.join("<br>")}</p>`, nextIndex: index };
}

function readBlock(lines, index, options) {
  const line = lines[index];
  if (/^```/.test(line)) {
    return readCodeBlock(lines, index);
  }
  const heading = line.match(/^(#{1,6})\s+(.*)$/);
  if (heading) {
    const level = Math.min(heading[1].length, 4);
    return { html: `<h${level}>${renderInline(heading[2], options)}</h${level}>`, nextIndex: index + 1 };
  }
  if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
    return readList(lines, index, options);
  }
  if (/^>\s?/.test(line)) {
    return readQuote(lines, index, options);
  }
  if (line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
    return readTable(lines, index, options);
  }
  if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
    return { html: "<hr>", nextIndex: index + 1 };
  }
  if (line.trim() === "") {
    return { html: "", nextIndex: index + 1 };
  }
  return readParagraph(lines, index, options);
}

export function renderMarkdown(source, options = {}) {
  const lines = source.split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const block = readBlock(lines, index, options);
    html.push(block.html);
    index = block.nextIndex;
  }
  return html.join("");
}
