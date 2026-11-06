// 挂在按钮上的弹层（选模型面板这类）往上还是往下弹、最多多高、要不要往左挪。
//
// 弹层是 absolute 定位，放在会滚动的容器里时，超出容器的部分会被裁掉：桌面端新任务页的输入框
// 在 overflow: auto 的容器里，面板往上弹就被容器顶边切掉一截。所以「放得下」要按窗口和所有会裁内容的
// 祖先的交集算，只看窗口不够。
//
// 弹层的 CSS 约定：默认贴在按钮上方（bottom: 100% + 间距），加 opens-down 类时贴在下方，left: 0 对齐按钮左边。

const DEFAULTS = { gap: 8, margin: 12, maxHeight: 560, minHeight: 160 };

export function placePopover(popover, anchor, options = {}) {
  const { gap, margin, maxHeight, minHeight } = { ...DEFAULTS, ...options };
  const bounds = visibleBounds(anchor);
  const anchorRect = anchor.getBoundingClientRect();

  const spaceAbove = anchorRect.top - bounds.top - gap - margin;
  const spaceBelow = bounds.bottom - anchorRect.bottom - gap - margin;
  // scrollHeight 不受之前设的 max-height 影响，就是内容本来要多高
  const naturalHeight = Math.min(maxHeight, popover.scrollHeight);
  const opensDown = spaceAbove < naturalHeight && spaceBelow > spaceAbove;
  const available = opensDown ? spaceBelow : spaceAbove;
  popover.classList.toggle("opens-down", opensDown);
  popover.style.maxHeight = `${Math.max(minHeight, Math.min(maxHeight, available))}px`;

  // 右边放不下就往左挪，但不越过可见区域的左边
  popover.style.left = "";
  const popoverRect = popover.getBoundingClientRect();
  const overflowRight = popoverRect.right - (bounds.right - margin);
  const roomOnLeft = popoverRect.left - (bounds.left + margin);
  const shift = Math.min(overflowRight, roomOnLeft);
  if (shift > 0) {
    popover.style.left = `${popover.offsetLeft - shift}px`;
  }
  return opensDown ? "below" : "above";
}

// 窄屏下弹层改由 CSS 固定在底部时，把上面算出来的内联样式清掉
export function resetPopoverPlacement(popover) {
  popover.classList.remove("opens-down");
  popover.style.maxHeight = "";
  popover.style.left = "";
}

// 元素在窗口里真正能露出来的范围：窗口和每个 overflow 不是 visible 的祖先求交集
export function visibleBounds(element) {
  const bounds = { top: 0, left: 0, bottom: window.innerHeight, right: window.innerWidth };
  for (let node = element.parentElement; node && node !== document.body; node = node.parentElement) {
    const style = window.getComputedStyle(node);
    if (clipsContent(style.overflowY) || clipsContent(style.overflowX)) {
      const rect = node.getBoundingClientRect();
      bounds.top = Math.max(bounds.top, rect.top);
      bounds.left = Math.max(bounds.left, rect.left);
      bounds.bottom = Math.min(bounds.bottom, rect.bottom);
      bounds.right = Math.min(bounds.right, rect.right);
    }
  }
  return bounds;
}

function clipsContent(overflow) {
  return overflow !== "visible" && overflow !== "";
}
