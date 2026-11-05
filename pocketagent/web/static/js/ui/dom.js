// web 端的 DOM 工具：和桌面端一样的部分都在 shared/ 里，这里转一手，
// 自己只留图标——web 的图标外面包一层 span，样式表是按这个结构写的。

import { el } from "../shared/dom.js";
import { iconSvg } from "./icons.js";

export * from "../shared/dom.js";
export * from "../shared/format.js";

export function icon(name, className) {
  const wrapper = el("span", `icon ${className || ""}`);
  // 图标是 icons.js 里写死的常量，不含用户输入
  wrapper.innerHTML = iconSvg(name);
  return wrapper;
}
