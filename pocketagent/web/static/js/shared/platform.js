// 两个前端各自的「端适配」：共享模块要用到、但 web 和桌面端做法不同的几件事。
// 各端在入口（web 的 main.js、桌面端的 app.js）最先调 configurePlatform 填好，共享模块只通过这里取。
//
// - icon(name, className)：返回图标节点。web 是 <span class="icon"><svg>，桌面端直接是 <svg class="icon">，
//   两边样式表按各自的结构写，所以图标不合并。
// - requestJson(path, options) / sendJson(path, method, body)：path 一律写成 "/api/..."，
//   web 自己去掉开头的 / 变成相对地址（挂在网关子路径下也能用），桌面端交给主进程转发。
// - imageSource(apiPath)：要带 token 的图片地址 → 可以放进 <img src> 的地址（字符串或 Promise）。
//   web 是在地址上拼 token，桌面端由主进程取回来转成 data URL。

const platform = {
  icon: null,
  requestJson: null,
  sendJson: null,
  imageSource: null,
};

export function configurePlatform(implementation) {
  for (const name of Object.keys(platform)) {
    if (typeof implementation[name] !== "function") {
      throw new Error(`configurePlatform 缺少 ${name}`);
    }
    platform[name] = implementation[name];
  }
}

function required(name) {
  const implementation = platform[name];
  if (!implementation) {
    throw new Error(`共享模块用到 ${name} 之前要先调 configurePlatform`);
  }
  return implementation;
}

export function icon(name, className = "") {
  return required("icon")(name, className);
}

export function requestJson(path, options) {
  return required("requestJson")(path, options);
}

export function sendJson(path, method, body) {
  return required("sendJson")(path, method, body);
}

export function imageSource(apiPath) {
  return required("imageSource")(apiPath);
}
