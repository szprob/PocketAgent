// 够用的 Markdown 子集：先整体转义再加标签，不会注入 HTML。

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInlineText(escapedText) {
  return escapedText
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderInline(text) {
  // 按反引号切开：奇数段是行内代码，不做加粗、链接处理；最后一个反引号没配对时当普通文字
  const segments = escapeHtml(text).split("`");
  const parts = [];
  segments.forEach((segment, index) => {
    const isOddSegment = index % 2 === 1;
    const hasClosingBacktick = index < segments.length - 1;
    if (isOddSegment && hasClosingBacktick) {
      parts.push(`<code>${segment}</code>`);
    } else if (isOddSegment) {
      parts.push(renderInlineText("`" + segment));
    } else {
      parts.push(renderInlineText(segment));
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

function readList(lines, startIndex) {
  const ordered = /^\s*\d+\.\s+/.test(lines[startIndex]);
  const pattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
  const items = [];
  let index = startIndex;
  while (index < lines.length && pattern.test(lines[index])) {
    items.push(`<li>${renderInline(lines[index].replace(pattern, ""))}</li>`);
    index += 1;
  }
  const tag = ordered ? "ol" : "ul";
  return { html: `<${tag}>${items.join("")}</${tag}>`, nextIndex: index };
}

function readQuote(lines, startIndex) {
  const quoteLines = [];
  let index = startIndex;
  while (index < lines.length && /^>\s?/.test(lines[index])) {
    quoteLines.push(renderInline(lines[index].replace(/^>\s?/, "")));
    index += 1;
  }
  return { html: `<blockquote>${quoteLines.join("<br>")}</blockquote>`, nextIndex: index };
}

function readTable(lines, startIndex) {
  const headerCells = splitTableRow(lines[startIndex]).map((cell) => `<th>${renderInline(cell)}</th>`);
  const bodyRows = [];
  let index = startIndex + 2;
  while (index < lines.length && lines[index].includes("|") && lines[index].trim() !== "") {
    const cells = splitTableRow(lines[index]).map((cell) => `<td>${renderInline(cell)}</td>`);
    bodyRows.push(`<tr>${cells.join("")}</tr>`);
    index += 1;
  }
  const html = `<table><thead><tr>${headerCells.join("")}</tr></thead><tbody>${bodyRows.join("")}</tbody></table>`;
  return { html, nextIndex: index };
}

function readParagraph(lines, startIndex) {
  const paragraphLines = [];
  let index = startIndex;
  while (index < lines.length && lines[index].trim() !== "" && !isBlockStart(lines[index])) {
    paragraphLines.push(renderInline(lines[index]));
    index += 1;
  }
  return { html: `<p>${paragraphLines.join("<br>")}</p>`, nextIndex: index };
}

function readBlock(lines, index) {
  const line = lines[index];
  if (/^```/.test(line)) {
    return readCodeBlock(lines, index);
  }
  const heading = line.match(/^(#{1,6})\s+(.*)$/);
  if (heading) {
    const level = Math.min(heading[1].length, 4);
    return { html: `<h${level}>${renderInline(heading[2])}</h${level}>`, nextIndex: index + 1 };
  }
  if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
    return readList(lines, index);
  }
  if (/^>\s?/.test(line)) {
    return readQuote(lines, index);
  }
  if (line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
    return readTable(lines, index);
  }
  if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
    return { html: "<hr>", nextIndex: index + 1 };
  }
  if (line.trim() === "") {
    return { html: "", nextIndex: index + 1 };
  }
  return readParagraph(lines, index);
}

export function renderMarkdown(source) {
  const lines = source.split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const block = readBlock(lines, index);
    html.push(block.html);
    index = block.nextIndex;
  }
  return html.join("");
}
