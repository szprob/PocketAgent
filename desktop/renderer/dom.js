// 桌面端的 DOM 工具：和 web 一样的部分都在共享目录里（static/js/shared/），这里转一手，
// 让 renderer 里的文件照旧从 ./dom.js 拿。图标在 icons.js（桌面端直接是 <svg class="icon">）。

export * from "./static/js/shared/dom.js";
export * from "./static/js/shared/format.js";
