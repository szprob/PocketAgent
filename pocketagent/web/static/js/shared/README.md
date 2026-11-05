# 前端共享代码

web（`pocketagent/web/static/`）和桌面端（`desktop/renderer/`）共用的 ES 模块，只放一份：

- web 按相对路径 import（`../shared/xxx.js`）。
- 桌面端的自定义协议把 `pocketagent://app/static/` 映射到 `pocketagent/web/static/`（只放行 `js/shared/` 和 `vendor/`），
  renderer 里写 `./static/js/shared/xxx.js`；打包时这两个目录作为 extraResources 一起带上。

两个前端做法不同的地方（图标长什么样、REST 走 fetch 还是主进程转发、受保护的图片怎么取）
不在这里写死，由各端启动时调 `platform.js` 的 `configurePlatform()` 填进来。

这里的模块不能 import 任何一端自己的代码，也不能依赖页面上某个元素一定存在（弹窗类组件除外：
两个 index.html 里对应的元素 id 要保持一致）。不碰 DOM 的纯函数（`format.js`、`trigger-form.js`）
有 Node 跑的单测，见 `tests/mock/test_web_js.py`。
