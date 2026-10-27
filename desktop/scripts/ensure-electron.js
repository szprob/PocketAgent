// npm 在本机没执行 electron 自带的 postinstall，二进制没下载（只有 JS 包）。
// 这里在 npm install 之后补一刀：已经装好就跳过；没装好就按镜像下载。
// 换机器重装依赖时不用再手动 node node_modules/electron/install.js。

const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const electronDirectory = path.join(__dirname, "..", "node_modules", "electron");
const installer = path.join(electronDirectory, "install.js");
const installedVersionFile = path.join(electronDirectory, "dist", "version");

if (!fs.existsSync(installer) || fs.existsSync(installedVersionFile)) {
  process.exit(0);
}

console.log("Electron 二进制缺失，开始下载…");
execFileSync(process.execPath, [installer], {
  stdio: "inherit",
  env: {
    ...process.env,
    ELECTRON_MIRROR: process.env.ELECTRON_MIRROR || "https://npmmirror.com/mirrors/electron/",
  },
});
