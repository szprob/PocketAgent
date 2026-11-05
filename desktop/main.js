// Electron 主进程：只管窗口、原生能力和后端的拉起/关闭。
// 后端是共享的那套（python -m pocketagent --web），桌面端前端在 renderer/ 里，
// 只通过 REST + WebSocket 和后端说话。和 web 一样的界面代码（Markdown、活动折叠、触发器表单……）
// 不再抄一份，直接用 pocketagent/web/static/js/shared/，见 registerProtocolHandler。

const { app, BrowserWindow, Menu, ipcMain, net, protocol, screen, shell } = require("electron");
const { execFileSync, spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const APP_SCHEME = "pocketagent";
const RENDERER_DIRECTORY = path.join(__dirname, "renderer");
// web 的静态目录：开发时就是仓库里那份；打包后由 electron-builder 的 extraResources 拷到 resources/static
const WEB_STATIC_DIRECTORY = app.isPackaged
  ? path.join(process.resourcesPath, "static")
  : path.join(__dirname, "..", "pocketagent", "web", "static");
// 只放行和 web 共用的这两块，web 自己的页面和脚本桌面端用不到
const SHARED_STATIC_PREFIXES = ["js/shared/", "vendor/"];
const BACKEND_URL_PATTERN = /PocketAgent web 界面：(http\S+)/;
const BACKEND_LOG_LIMIT = 300;

let mainWindow = null;
let backendProcess = null;
let backendState = { status: "starting", baseUrl: "", token: "", error: "" };
const backendLogLines = [];

// renderer 用自定义协议加载：file:// 下 ES module 会被 CORS 拦掉，自定义协议还方便收紧 CSP。
protocol.registerSchemesAsPrivileged([
  {
    scheme: APP_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true },
  },
]);

// 从 Finder / 开始菜单 / Dock 启动的打包版拿到的 PATH 很短（macOS 上只有 /usr/bin:/bin:/usr/sbin:/sbin），
// 用户在 shell 里装的 pocketagent、pyenv / uv / conda 的 Python 都看不见。从登录 shell 里取一次完整的 PATH。
// 开发时从终端 npm start，PATH 本来就是全的，不用多跑这一趟。
const PATH_MARKER = "__POCKETAGENT_PATH__";

function loadLoginShellPath() {
  if (!app.isPackaged || process.platform === "win32") {
    return;
  }
  const shell = process.env.SHELL || "/bin/zsh";
  try {
    // -i -l 才会读 .zshrc / .bash_profile；rc 文件可能往 stdout 打东西，所以用标记切出 PATH
    const output = execFileSync(shell, ["-ilc", `printf '${PATH_MARKER}%s' "$PATH"`], {
      encoding: "utf8",
      timeout: 5000,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const markerIndex = output.lastIndexOf(PATH_MARKER);
    const shellPath = markerIndex >= 0 ? output.slice(markerIndex + PATH_MARKER.length).trim() : "";
    if (shellPath) {
      const merged = [...shellPath.split(path.delimiter), ...(process.env.PATH || "").split(path.delimiter)];
      process.env.PATH = [...new Set(merged.filter(Boolean))].join(path.delimiter);
    }
  } catch {
    // shell 起不来或者超时：就用原来的 PATH，后面找不到 Python 会在界面上说清楚
  }
}

function findOnPath(name) {
  const extensions = process.platform === "win32" ? [".exe", ".cmd", ""] : [""];
  for (const directory of (process.env.PATH || "").split(path.delimiter)) {
    for (const extension of extensions) {
      const candidate = path.join(directory, name + extension);
      if (directory && fs.existsSync(candidate)) {
        return candidate;
      }
    }
  }
  return null;
}

// pip / pipx / uv tool 装出来的 pocketagent 命令知道自己用的是哪个 Python：
// POSIX 上看脚本第一行的 #!，Windows 上 Scripts\pocketagent.exe 的上一级就是 python.exe 所在的目录
function launcherPython() {
  const launcher = findOnPath("pocketagent");
  if (!launcher) {
    return null;
  }
  if (process.platform === "win32") {
    const python = path.join(path.dirname(path.dirname(launcher)), "python.exe");
    return fs.existsSync(python) ? python : null;
  }
  try {
    const firstLine = fs.readFileSync(launcher, "utf8").split("\n", 1)[0];
    const interpreter = firstLine.startsWith("#!") ? firstLine.slice(2).trim().split(/\s+/)[0] : "";
    return interpreter && fs.existsSync(interpreter) ? interpreter : null;
  } catch {
    return null;
  }
}

function pythonCandidates() {
  const candidates = [];
  // 本机约定的主环境是 ~/env/s11（和 AGENTS.md 里 make test 的 PYTHON 一致）
  const localEnvironment = path.join(os.homedir(), "env", "s11", "bin", "python");
  if (fs.existsSync(localEnvironment)) {
    candidates.push({ command: localEnvironment, arguments: [] });
  }
  const fromLauncher = launcherPython();
  if (fromLauncher) {
    candidates.push({ command: fromLauncher, arguments: [] });
  }
  if (process.platform === "win32") {
    // PATH 里的 python 常常是应用商店占位符，先试官方安装器带的 py 启动器
    if (findOnPath("py")) {
      candidates.push({ command: "py", arguments: ["-3"] });
    }
    candidates.push({ command: "python", arguments: [] });
  } else {
    candidates.push({ command: "python3", arguments: [] }, { command: "python", arguments: [] });
  }
  return candidates;
}

function canImportPocketagent(python) {
  try {
    execFileSync(python.command, [...python.arguments, "-c", "import pocketagent"], { stdio: "ignore", timeout: 20000 });
    return true;
  } catch {
    return false;
  }
}

// 显式指定的直接用（用错了让它在日志里报错，比悄悄换一个强）；否则挨个试，挑第一个 import 得了 pocketagent 的
function resolvePython() {
  if (process.env.POCKETAGENT_PYTHON) {
    return { command: process.env.POCKETAGENT_PYTHON, arguments: [] };
  }
  return pythonCandidates().find(canImportPocketagent) || null;
}

const PYTHON_MISSING_MESSAGE =
  "没找到装了 pocketagent 的 Python。先在终端里装好后端（如 pip install \"pocketagent[web] @ git+https://github.com/szprob/PocketAgent\"，" +
  "或者 pipx / uv tool install），再点「重新启动后端」；Python 装在别处就用环境变量 POCKETAGENT_PYTHON 指定。";

function resolveWorkspace() {
  return process.env.POCKETAGENT_WORKSPACE || os.homedir();
}

function workspaceArguments() {
  // 只有用户用 POCKETAGENT_WORKSPACE 明确指定时才传 -C（后端会把它加进工作区列表）；
  // 默认的家目录只是进程的 cwd，不传 -C，免得用户在界面里移除的工作区每次启动又被加回来
  return process.env.POCKETAGENT_WORKSPACE ? ["-C", process.env.POCKETAGENT_WORKSPACE] : [];
}

function sendToWindow(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function updateBackendState(patch) {
  backendState = { ...backendState, ...patch };
  sendToWindow("backend-state", { ...backendState });
}

function pushBackendLog(line) {
  if (!line.trim()) {
    return;
  }
  backendLogLines.push(line);
  if (backendLogLines.length > BACKEND_LOG_LIMIT) {
    backendLogLines.shift();
  }
  sendToWindow("backend-log", line);
}

function startBackend() {
  const token = crypto.randomBytes(24).toString("base64url");
  const python = resolvePython();
  const workspace = resolveWorkspace();
  backendState = { status: "starting", baseUrl: "", token, error: "" };
  if (!python) {
    updateBackendState({ status: "exited", error: PYTHON_MISSING_MESSAGE });
    return;
  }

  const child = spawn(
    python.command,
    [
      ...python.arguments,
      "-m",
      "pocketagent",
      "--web",
      "--no-browser",
      "--host",
      "127.0.0.1",
      "--token",
      token,
      ...workspaceArguments(),
    ],
    { cwd: workspace, env: process.env },
  );
  backendProcess = child;

  let stdoutRest = "";
  let stderrRest = "";

  child.stdout.on("data", (chunk) => {
    const lines = (stdoutRest + chunk.toString()).split("\n");
    stdoutRest = lines.pop();
    for (const line of lines) {
      pushBackendLog(line);
      const match = line.match(BACKEND_URL_PATTERN);
      if (match) {
        const url = new URL(match[1]);
        updateBackendState({ status: "ready", baseUrl: url.origin, error: "" });
      }
    }
  });

  child.stderr.on("data", (chunk) => {
    const lines = (stderrRest + chunk.toString()).split("\n");
    stderrRest = lines.pop();
    for (const line of lines) {
      pushBackendLog(line);
    }
  });

  child.on("error", (error) => {
    updateBackendState({
      status: "exited",
      error: `启动 ${python.command} 失败：${error.message}。用 POCKETAGENT_PYTHON 指定装了 pocketagent 的 Python。`,
    });
  });

  child.on("exit", (code, signal) => {
    if (backendProcess !== child) {
      return;
    }
    backendProcess = null;
    if (app.isQuitting) {
      return;
    }
    const reason = signal ? `收到信号 ${signal}` : `退出码 ${code}`;
    updateBackendState({ status: "exited", error: `后端已退出（${reason}）` });
  });
}

function stopBackend() {
  if (!backendProcess) {
    return;
  }
  const child = backendProcess;
  backendProcess = null;
  child.kill("SIGTERM");
}

const PREFERRED_WINDOW_SIZE = { width: 1320, height: 880 };
const MINIMUM_WINDOW_SIZE = { width: 1080, height: 640 };

function initialWindowBounds() {
  // 窗口不能比屏幕可用区域大：13 寸笔记本是 1280×800，按 1320×880 开、再居中，左右各有一截在屏幕外，
  // 红绿灯和左边的字被切掉、输入框贴到右边。最小尺寸也跟着收，不然用户缩不回屏幕里。
  // 位置也自己算：放在可用区域（扣掉菜单栏和 Dock）正中间，默认的居中会让顶上一截跑到菜单栏上面
  const workArea = screen.getPrimaryDisplay().workArea;
  const width = Math.min(PREFERRED_WINDOW_SIZE.width, workArea.width);
  const height = Math.min(PREFERRED_WINDOW_SIZE.height, workArea.height);
  return {
    x: workArea.x + Math.round((workArea.width - width) / 2),
    y: workArea.y + Math.round((workArea.height - height) / 2),
    width,
    height,
    minWidth: Math.min(MINIMUM_WINDOW_SIZE.width, workArea.width),
    minHeight: Math.min(MINIMUM_WINDOW_SIZE.height, workArea.height),
  };
}

function createWindow() {
  mainWindow = new BrowserWindow({
    ...initialWindowBounds(),
    backgroundColor: "#101012",
    show: false,
    title: "PocketAgent",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    trafficLightPosition: { x: 16, y: 18 },
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadURL(`${APP_SCHEME}://app/index.html`);
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// pocketagent://app/xxx → renderer/xxx；pocketagent://app/static/js/shared/xxx → web 的共享目录
function resolveAppFile(relativePath) {
  if (relativePath.startsWith("static/")) {
    const staticPath = relativePath.slice("static/".length);
    if (!SHARED_STATIC_PREFIXES.some((prefix) => staticPath.startsWith(prefix))) {
      return null;
    }
    return resolveInside(WEB_STATIC_DIRECTORY, staticPath);
  }
  return resolveInside(RENDERER_DIRECTORY, relativePath);
}

// 拼出来的路径必须还在目录里面，挡住 ../ 跳出去读别的文件
function resolveInside(directory, relativePath) {
  const filePath = path.resolve(directory, relativePath);
  return filePath.startsWith(directory + path.sep) ? filePath : null;
}

function registerProtocolHandler() {
  protocol.handle(APP_SCHEME, (request) => {
    const url = new URL(request.url);
    const relativePath = decodeURIComponent(url.pathname).replace(/^\/+/, "") || "index.html";
    const filePath = resolveAppFile(relativePath);
    if (!filePath) {
      return new Response("forbidden", { status: 403 });
    }
    return net.fetch(pathToFileURL(filePath).toString());
  });
}

async function openInBrowser() {
  if (backendState.status !== "ready") {
    return;
  }
  await shell.openExternal(`${backendState.baseUrl}/?token=${encodeURIComponent(backendState.token)}`);
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  const template = [
    ...(isMac ? [{ role: "appMenu" }] : []),
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "复制" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { role: "reload", label: "重新加载界面" },
        { role: "forceReload", label: "强制重新加载" },
        { role: "toggleDevTools", label: "开发者工具" },
        { type: "separator" },
        { role: "resetZoom", label: "实际大小" },
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { type: "separator" },
        { role: "togglefullscreen", label: "全屏" },
      ],
    },
    {
      label: "窗口",
      submenu: [{ role: "minimize", label: "最小化" }, { role: "close", label: "关闭窗口" }],
    },
    {
      label: "帮助",
      submenu: [
        { label: "在浏览器中打开", click: openInBrowser },
        { label: "打开数据目录", click: () => shell.openPath(path.join(os.homedir(), ".pocketagent")) },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function registerIpcHandlers() {
  ipcMain.handle("backend:state", () => ({ ...backendState, log: backendLogLines.slice() }));

  ipcMain.handle("backend:restart", () => {
    stopBackend();
    updateBackendState({ status: "starting", baseUrl: "", error: "" });
    startBackend();
    return true;
  });

  ipcMain.handle("backend:request", async (event, request) => {
    const requestPath = request?.path;
    if (typeof requestPath !== "string" || !requestPath.startsWith("/api/")) {
      return { ok: false, status: 0, error: "只允许访问 /api/ 下的接口" };
    }
    if (backendState.status !== "ready") {
      return { ok: false, status: 0, error: "后端还没启动好" };
    }
    const url = new URL(backendState.baseUrl + requestPath);
    url.searchParams.set("token", backendState.token);
    try {
      const response = await fetch(url, {
        method: request.method || "GET",
        headers: request.body === undefined ? undefined : { "Content-Type": "application/json" },
        body: request.body === undefined ? undefined : JSON.stringify(request.body),
      });
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (notJson) {
        data = null;
      }
      if (!response.ok) {
        return { ok: false, status: response.status, error: data?.error || text || `请求失败（${response.status}）` };
      }
      return { ok: true, status: response.status, data };
    } catch (error) {
      return { ok: false, status: 0, error: error.message || String(error) };
    }
  });

  ipcMain.handle("backend:upload", async (event, request) => {
    if (backendState.status !== "ready") {
      return { ok: false, status: 0, error: "后端还没启动好" };
    }
    const sessionId = request?.sessionId;
    const files = Array.isArray(request?.files) ? request.files : [];
    if (typeof sessionId !== "string" || files.length === 0) {
      return { ok: false, status: 0, error: "没有要上传的文件" };
    }
    const form = new FormData();
    for (const file of files) {
      form.append("file", new Blob([file.bytes], { type: file.type || "application/octet-stream" }), file.name || "粘贴的文件");
    }
    const url = new URL(`${backendState.baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/attachments`);
    url.searchParams.set("token", backendState.token);
    try {
      const response = await fetch(url, { method: "POST", body: form });
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (notJson) {
        data = null;
      }
      if (!response.ok) {
        return { ok: false, status: response.status, error: data?.error || text || `上传失败（${response.status}）` };
      }
      return { ok: true, status: response.status, data };
    } catch (error) {
      return { ok: false, status: 0, error: error.message || String(error) };
    }
  });

  ipcMain.handle("backend:binary", async (event, requestPath) => {
    if (typeof requestPath !== "string" || !requestPath.startsWith("/api/")) {
      return { ok: false, error: "只允许访问 /api/ 下的接口" };
    }
    if (backendState.status !== "ready") {
      return { ok: false, error: "后端还没启动好" };
    }
    const url = new URL(backendState.baseUrl + requestPath);
    url.searchParams.set("token", backendState.token);
    try {
      const response = await fetch(url);
      if (!response.ok) {
        return { ok: false, status: response.status, error: `读取失败（${response.status}）` };
      }
      const mime = response.headers.get("content-type") || "application/octet-stream";
      const buffer = Buffer.from(await response.arrayBuffer());
      return { ok: true, data: { mime, base64: buffer.toString("base64") } };
    } catch (error) {
      return { ok: false, error: error.message || String(error) };
    }
  });

  ipcMain.handle("app:open-external", async (event, url) => {
    if (typeof url === "string" && /^https?:\/\//.test(url)) {
      await shell.openExternal(url);
    }
  });

  ipcMain.handle("app:open-in-browser", openInBrowser);
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.setName("PocketAgent");

  app.on("second-instance", () => {
    if (!mainWindow) {
      return;
    }
    if (mainWindow.isMinimized()) {
      mainWindow.restore();
    }
    mainWindow.focus();
  });

  app.whenReady().then(() => {
    loadLoginShellPath();
    registerProtocolHandler();
    registerIpcHandlers();
    buildMenu();
    startBackend();
    createWindow();
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });

  app.on("window-all-closed", () => {
    app.quit();
  });

  app.on("before-quit", () => {
    app.isQuitting = true;
    stopBackend();
  });

  app.on("will-quit", () => {
    stopBackend();
  });

  // 极端情况下（进程被直接结束）也要把子进程带走，别留下占着端口的后端
  process.on("exit", () => {
    if (backendProcess) {
      backendProcess.kill("SIGKILL");
    }
  });
}
