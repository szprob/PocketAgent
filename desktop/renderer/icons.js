// 界面上用到的小图标，统一用内联 SVG（不引外部字体/图片，CSP 也干净）。

const PATHS = {
  panelLeft: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="9" y1="4" x2="9" y2="20"/>',
  panelRight: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="15" y1="4" x2="15" y2="20"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  close: '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 6"/><polyline points="20 4 20 11 13 11"/>',
  settings:
    '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.3 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.4 2.1a7 7 0 0 0-1.7 1l-2.3-1-2 3.4L5.1 11a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.4 2.1h5l.4-2.1a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.5c.1-.3.1-.7.1-1z"/>',
  clock: '<circle cx="12" cy="12" r="8"/><polyline points="12 7 12 12 15.5 14"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file: '<path d="M6 3h7l5 5v13H6z"/><polyline points="13 3 13 8 18 8"/>',
  chevronDown: '<polyline points="6 9 12 15 18 9"/>',
  chevronRight: '<polyline points="9 6 15 12 9 18"/>',
  star: '<polygon points="12 3.5 14.7 9 20.7 9.8 16.3 14 17.4 20 12 17.1 6.6 20 7.7 14 3.3 9.8 9.3 9"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  paperclip:
    '<path d="M8.5 12.5l6-6a3 3 0 0 1 4.3 4.3l-8.6 8.6a5 5 0 0 1-7.1-7.1l8-8"/>',
  arrowUp: '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  shield: '<path d="M12 3l7 3v6c0 5-3.4 8-7 9-3.6-1-7-4-7-9V6z"/>',
  sparkle:
    '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M18.5 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
  check: '<polyline points="4 12 9 17 20 6"/>',
  model: '<rect x="4" y="6" width="16" height="12" rx="3"/><circle cx="12" cy="12" r="2.2"/>',
  more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h9a6 6 0 0 1 0 12H7"/>',
  trash: '<line x1="4" y1="7" x2="20" y2="7"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="M4 18l5-5 4 4 3-3 4 4"/>',
  monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><line x1="9" y1="21" x2="15" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  search: '<circle cx="11" cy="11" r="6"/><line x1="16" y1="16" x2="21" y2="21"/>',
  listCheck:
    '<path d="m3.5 6 1 1L7 4.5"/><path d="m3.5 12.5 1 1L7 11"/><path d="M11 6.5h10M11 13h10"/><path d="M11 19h10"/><path d="m3.5 18.5 1 1L7 17"/>',
};

export function icon(name, className = "") {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `icon ${className}`.trim());
  svg.innerHTML = PATHS[name] || PATHS.file;
  return svg;
}
