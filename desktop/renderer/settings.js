// 设置弹窗：模型、通用、技能、记忆、用量五个页签。
// 所有改动先在本地草稿里，点「保存」才 PUT /api/config（和 web 一样整份提交）。

import { invalidateSkillNames, request, sendJson } from "./api.js";
import { button, el, show } from "./dom.js";
import { REASONING_LABELS } from "./modelpicker.js";
import { state } from "./state.js";
import { renderUsage } from "./usage.js";

const MODE_LABELS = { ask: "每次询问", ai_auto: "AI 自动审批", full_auto: "完全访问" };
const DEFAULT_CONTEXT_WINDOW = 256000;

const elements = {};
let callbacks = {};
let config = null;
let draft = null;
let selectedModelId = null;
let disabledSkills = new Set();
let skillContentCache = new Map();
let memoryContentCache = new Map();
let memoryWorkspace = null;
let pendingClearSearchKey = false;
let currentTab = "models";

export function initSettings(handlers) {
  callbacks = handlers;
  elements.modal = document.getElementById("settings-modal");
  elements.tabs = [...document.querySelectorAll(".settings-tab")];
  elements.pages = [...document.querySelectorAll(".settings-page")];
  elements.message = document.getElementById("settings-message");

  elements.modelList = document.getElementById("model-list");
  elements.addModel = document.getElementById("add-model");
  elements.modelForm = document.getElementById("model-form");
  elements.modelFormEmpty = document.getElementById("model-form-empty");
  elements.modelFormFields = document.getElementById("model-form-fields");
  elements.supportsImages = document.getElementById("supports-images");
  elements.modelVisible = document.getElementById("model-visible");
  elements.apiKeyHint = document.getElementById("api-key-hint");
  elements.testResult = document.getElementById("test-result");

  elements.userName = document.getElementById("user-name-input");
  elements.defaultWorkspace = document.getElementById("default-workspace-select");
  elements.defaultWorkspaceHint = document.getElementById("default-workspace-hint");
  elements.defaultMode = document.getElementById("default-mode-select");
  elements.maxSteps = document.getElementById("max-steps-input");
  elements.autoCompact = document.getElementById("auto-compact-input");
  elements.compactThreshold = document.getElementById("compact-threshold-select");
  elements.webSearchEnabled = document.getElementById("web-search-enabled");
  elements.webSearchProvider = document.getElementById("web-search-provider");
  elements.webSearchEngineField = document.getElementById("web-search-engine-field");
  elements.webSearchEngine = document.getElementById("web-search-engine");
  elements.webSearchKeyField = document.getElementById("web-search-key-field");
  elements.webSearchKey = document.getElementById("web-search-key");
  elements.webSearchKeyLabel = document.getElementById("web-search-key-label");
  elements.webSearchKeyHint = document.getElementById("web-search-key-hint");
  elements.webFetchEnabled = document.getElementById("web-fetch-enabled");
  elements.computerUseEnabled = document.getElementById("computer-use-enabled");
  elements.computerUseHint = document.getElementById("computer-use-hint");
  elements.sandboxEnabled = document.getElementById("sandbox-enabled");
  elements.sandboxNetwork = document.getElementById("sandbox-network");
  elements.sandboxHint = document.getElementById("sandbox-hint");
  elements.allowOutsideWorkspace = document.getElementById("allow-outside-workspace");
  elements.trustedDirs = document.getElementById("trusted-dirs-input");
  elements.configPath = document.getElementById("config-path");

  elements.skillsEnabled = document.getElementById("skills-enabled");
  elements.skillList = document.getElementById("skill-list");
  elements.skillProblems = document.getElementById("skill-problems");
  elements.skillDirs = document.getElementById("skill-dirs-input");
  elements.skillDirsHint = document.getElementById("skill-dirs-hint");
  elements.skillsWorkspaceHint = document.getElementById("skills-workspace-hint");

  elements.memoryEnabled = document.getElementById("memory-enabled");
  elements.memoryContent = document.getElementById("memory-content");
  elements.memoryWorkspaceHint = document.getElementById("memory-workspace-hint");
  elements.usageContent = document.getElementById("usage-content");

  elements.clearSearchKey = button("ghost-button small", "清除已保存的密钥");
  elements.clearSearchKey.addEventListener("click", () => {
    pendingClearSearchKey = true;
    elements.webSearchKeyHint.textContent = "保存后清除（之后要用对应厂商模型的密钥）";
  });
  elements.webSearchKeyField.append(elements.clearSearchKey);

  for (const tab of elements.tabs) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  }
  document.getElementById("settings-close").addEventListener("click", closeSettings);
  document.getElementById("settings-cancel").addEventListener("click", closeSettings);
  document.getElementById("settings-save").addEventListener("click", saveConfig);
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeSettings();
    }
  });

  elements.addModel.addEventListener("click", addModel);
  elements.modelForm.addEventListener("submit", (event) => event.preventDefault());
  document.getElementById("test-model").addEventListener("click", testModel);
  document.getElementById("make-default").addEventListener("click", makeDefault);
  document.getElementById("delete-model").addEventListener("click", deleteModel);

  elements.webSearchProvider.addEventListener("change", () => {
    paintWebSearchFields();
    elements.webSearchKeyHint.textContent = pendingClearSearchKey ? "保存后清除" : "留空表示不修改已保存的密钥";
  });
  elements.sandboxEnabled.addEventListener("change", paintSandboxFields);

  for (const tab of elements.tabs) {
    if (tab.dataset.tab === "models") {
      tab.classList.add("active");
    }
  }
}

export function isSettingsOpen() {
  return elements.modal && !elements.modal.classList.contains("hidden");
}

export async function openSettings(tab = "models") {
  if (!elements.modal) {
    return;
  }
  show(elements.modal, true);
  elements.message.textContent = "";
  elements.message.className = "settings-message";
  try {
    await loadConfig();
  } catch (error) {
    setMessage(`读取配置失败：${error.message}`, "error");
    return;
  }
  switchTab(tab);
}

export function closeSettings() {
  if (elements.modal) {
    show(elements.modal, false);
  }
}

function switchTab(name) {
  currentTab = name;
  for (const tab of elements.tabs) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  }
  for (const page of elements.pages) {
    page.classList.toggle("hidden", page.dataset.page !== name);
  }
  if (name === "skills") {
    loadSkills();
  } else if (name === "memory") {
    loadMemory();
  } else if (name === "usage") {
    renderUsage(elements.usageContent);
  }
}

function setMessage(text, kind = "") {
  elements.message.textContent = text;
  elements.message.className = `settings-message ${kind}`.trim();
}

// ---- 读取和填充 ----

async function loadConfig() {
  config = await request("/api/config");
  draft = {
    models: structuredClone(config.models || []),
    default_model: config.default_model,
  };
  selectedModelId = draft.default_model || draft.models[0]?.id || null;
  disabledSkills = new Set(config.disabled_skills || []);
  skillContentCache = new Map();
  memoryContentCache = new Map();
  memoryWorkspace = currentWorkspace();
  pendingClearSearchKey = false;

  fillModels();
  fillGeneral();

  if (currentTab === "skills") {
    loadSkills();
  } else if (currentTab === "memory") {
    loadMemory();
  } else if (currentTab === "usage") {
    renderUsage(elements.usageContent);
  }
}

function currentWorkspace() {
  const task = state.tasks.find((each) => each.id === state.currentTaskId);
  return task?.workspace || state.info?.default_workspace || "";
}

function fillModels() {
  elements.modelList.replaceChildren();
  for (const model of draft.models) {
    const item = el("button", "model-item");
    item.type = "button";
    item.classList.toggle("active", model.id === selectedModelId);
    item.append(el("span", "model-item-name", model.name || model.model || "未命名"));
    item.append(el("span", "model-item-meta", model.model || "（没填模型名）"));
    item.addEventListener("click", () => {
      syncModelForm(false);
      selectedModelId = model.id;
      renderModelList();
      renderModelForm();
    });
    elements.modelList.append(item);
  }
  const hasModels = draft.models.length > 0;
  show(elements.modelFormEmpty, !hasModels);
  show(elements.modelFormFields, hasModels);
}

function renderModelList() {
  fillModels();
}

function selectedModel() {
  return draft.models.find((model) => model.id === selectedModelId) || null;
}

function fillGeneral() {
  elements.userName.value = config.user_name || "";
  elements.defaultWorkspace.replaceChildren();
  for (const workspace of state.info?.workspaces || []) {
    const option = el("option", "", workspace.name);
    option.value = workspace.path;
    option.disabled = !workspace.exists;
    elements.defaultWorkspace.append(option);
  }
  elements.defaultWorkspace.value = config.default_workspace || "";
  elements.defaultWorkspaceHint.textContent = config.default_workspace
    ? `当前默认：${config.default_workspace}`
    : "没设默认工作区时，用工作区列表里的第一个";

  elements.defaultMode.replaceChildren();
  for (const mode of state.info?.modes || []) {
    const option = el("option", "", MODE_LABELS[mode] || mode);
    option.value = mode;
    elements.defaultMode.append(option);
  }
  elements.defaultMode.value = config.approval_mode;

  elements.maxSteps.value = config.max_steps;
  elements.autoCompact.checked = Boolean(config.auto_compact);
  elements.compactThreshold.value = String(config.compact_threshold_percent);

  elements.webSearchEnabled.checked = Boolean(config.web_search_enabled);
  elements.webSearchProvider.value = config.web_search_provider || "bing";
  elements.webSearchEngine.value = config.web_search_engine || "search_std";
  elements.webSearchKey.value = "";
  elements.webSearchKeyHint.textContent = config.web_search_key_hint || "留空表示不修改已保存的密钥";
  elements.webFetchEnabled.checked = Boolean(config.web_fetch_enabled);

  elements.computerUseEnabled.checked = Boolean(config.computer_use_enabled);
  elements.computerUseEnabled.disabled = !config.computer_use_available;
  elements.computerUseHint.textContent = config.computer_use_available
    ? "要给 PocketAgent 打开「屏幕录制」和「辅助功能」权限（系统设置 → 隐私与安全性）"
    : "目前只支持 macOS";

  elements.sandboxEnabled.checked = Boolean(config.sandbox_enabled);
  elements.sandboxNetwork.checked = Boolean(config.sandbox_network);
  elements.sandboxHint.textContent = config.sandbox_available
    ? "沙箱可用：bash 命令在沙箱里跑，写不到工作目录和临时目录以外的地方"
    : "这台机器上用不了沙箱（需要 sandbox-exec 或 bubblewrap），命令会直接跑";

  elements.allowOutsideWorkspace.checked = Boolean(config.allow_outside_workspace);
  elements.trustedDirs.value = (config.trusted_dirs || []).join("\n");
  elements.configPath.textContent = `配置文件：${config.config_path || ""}`;

  elements.skillsEnabled.checked = Boolean(config.skills_enabled);
  elements.skillDirs.value = (config.skill_dirs || []).join("\n");
  elements.skillDirsHint.textContent = "留空就用默认目录（工作区 .pocketagent/skills、~/.pocketagent/skills 等）";
  elements.skillsWorkspaceHint.textContent = `技能按工作区查找；当前看的是：${memoryWorkspace || "未选择工作区"}`;

  elements.memoryEnabled.checked = Boolean(config.memory_enabled);
  elements.memoryWorkspaceHint.textContent = `项目记忆按工作区存；当前看的是：${memoryWorkspace || "未选择工作区"}`;

  const busyMode = config.busy_input_mode || "interject";
  for (const radio of elements.modal.querySelectorAll('input[name="busy-input-mode"]')) {
    radio.checked = radio.value === busyMode;
  }
  paintWebSearchFields();
  paintSandboxFields();
}

function paintWebSearchFields() {
  const provider = elements.webSearchProvider.value;
  show(elements.webSearchKeyField, provider !== "bing");
  show(elements.webSearchEngineField, provider === "zhipu");
  elements.webSearchKeyLabel.textContent = provider === "deepseek" ? "DeepSeek 搜索 API Key" : "智谱 API Key";
}

function paintSandboxFields() {
  elements.sandboxNetwork.disabled = !elements.sandboxEnabled.checked;
}

// ---- 模型表单 ----

function renderModelForm() {
  const model = selectedModel();
  if (!model) {
    return;
  }
  const form = elements.modelForm;
  form.elements.name.value = model.name || "";
  form.elements.model.value = model.model || "";
  form.elements.base_url.value = model.base_url || "";
  form.elements.api_key.value = "";
  form.elements.api_key.placeholder = model.api_key_hint ? `已保存：${model.api_key_hint}` : "sk-...";
  elements.apiKeyHint.textContent = model.api_key_hint ? `已保存的密钥：${model.api_key_hint}（留空表示不改）` : "还没保存密钥";

  form.elements.reasoning_effort.replaceChildren();
  const defaultOption = el("option", "", "默认（不传）");
  defaultOption.value = "";
  form.elements.reasoning_effort.append(defaultOption);
  for (const choice of model.reasoning_choices || []) {
    const option = el("option", "", REASONING_LABELS[choice] || choice);
    option.value = choice;
    form.elements.reasoning_effort.append(option);
  }
  form.elements.reasoning_effort.value = model.reasoning_effort || "";

  form.elements.context_window_tokens.value = model.context_window_tokens || DEFAULT_CONTEXT_WINDOW;
  form.elements.temperature.value = model.temperature === null || model.temperature === undefined ? "" : model.temperature;
  form.elements.extra_body.value = model.extra_body && Object.keys(model.extra_body).length
    ? JSON.stringify(model.extra_body, null, 2)
    : "";
  elements.supportsImages.checked = Boolean(model.supports_images);
  elements.modelVisible.checked = model.visible !== false;
  elements.testResult.textContent = "";
  elements.testResult.className = "test-result";
}

function parseExtraBody(text) {
  const trimmed = text.trim();
  if (!trimmed) {
    return {};
  }
  const parsed = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("额外请求参数必须是 JSON 对象");
  }
  return parsed;
}

function readModelForm(strict) {
  const model = selectedModel();
  if (!model) {
    return null;
  }
  const form = elements.modelForm;
  let extraBody = model.extra_body || {};
  if (strict) {
    try {
      extraBody = parseExtraBody(form.elements.extra_body.value);
    } catch (error) {
      throw new Error(`额外请求参数不是合法 JSON：${error.message}`);
    }
  }
  const data = {
    ...model,
    name: form.elements.name.value.trim() || form.elements.model.value.trim(),
    model: form.elements.model.value.trim(),
    base_url: form.elements.base_url.value.trim() || null,
    reasoning_effort: form.elements.reasoning_effort.value || null,
    context_window_tokens: Number(form.elements.context_window_tokens.value) || model.context_window_tokens,
    temperature: form.elements.temperature.value === "" ? null : Number(form.elements.temperature.value),
    extra_body: extraBody,
    supports_images: elements.supportsImages.checked,
    visible: elements.modelVisible.checked,
  };
  const typedKey = form.elements.api_key.value.trim();
  if (typedKey) {
    data.api_key = typedKey;
  } else if (model.api_key) {
    data.api_key = model.api_key;
  }
  return data;
}

function syncModelForm(strict) {
  const model = selectedModel();
  if (!model) {
    return;
  }
  const data = readModelForm(strict);
  const index = draft.models.findIndex((each) => each.id === model.id);
  if (index >= 0) {
    draft.models[index] = data;
  }
  elements.modelList.children[index]?.querySelector(".model-item-name")?.replaceChildren(document.createTextNode(data.name));
  elements.modelList.children[index]?.querySelector(".model-item-meta")?.replaceChildren(document.createTextNode(data.model));
}

function addModel() {
  syncModelForm(false);
  const model = {
    id: `local-${Date.now().toString(36)}`,
    name: "新模型",
    model: "",
    base_url: "",
    reasoning_effort: null,
    temperature: null,
    context_window_tokens: DEFAULT_CONTEXT_WINDOW,
    extra_body: {},
    supports_images: false,
    reasoning_choices: [],
    visible: true,
  };
  draft.models.push(model);
  selectedModelId = model.id;
  renderModelList();
  renderModelForm();
  elements.modelForm.elements.model.focus();
}

async function testModel() {
  let data;
  try {
    data = readModelForm(true);
  } catch (error) {
    setMessage(error.message, "error");
    return;
  }
  if (!data?.model) {
    setMessage("先填模型名再测试", "error");
    return;
  }
  elements.testResult.textContent = "测试中…";
  elements.testResult.className = "test-result";
  try {
    const result = await sendJson("/api/config/test", "POST", data);
    if (result.ok) {
      elements.testResult.textContent = `连接成功（${result.seconds}s）：${result.reply || ""}`.slice(0, 120);
      elements.testResult.className = "test-result ok";
    } else {
      elements.testResult.textContent = result.error || "连接失败";
      elements.testResult.className = "test-result error";
    }
  } catch (error) {
    elements.testResult.textContent = `测试失败：${error.message}`;
    elements.testResult.className = "test-result error";
  }
}

function makeDefault() {
  if (!selectedModelId) {
    return;
  }
  syncModelForm(false);
  draft.default_model = selectedModelId;
  renderModelList();
  setMessage("已设为默认模型（点保存后生效）", "ok");
}

function deleteModel() {
  const model = selectedModel();
  if (!model) {
    return;
  }
  if (!window.confirm(`删除模型「${model.name || model.model}」？\n只是从配置里删掉，已经开着的任务会继续用它。`)) {
    return;
  }
  draft.models = draft.models.filter((each) => each.id !== model.id);
  if (draft.default_model === model.id) {
    draft.default_model = draft.models[0]?.id || null;
  }
  selectedModelId = draft.models[0]?.id || null;
  renderModelList();
  renderModelForm();
}

// ---- 技能 ----

async function loadSkills() {
  elements.skillList.replaceChildren(el("div", "muted", "加载中…"));
  show(elements.skillProblems, false);
  elements.skillsWorkspaceHint.textContent = `技能按工作区查找；当前看的是：${memoryWorkspace || "未选择工作区"}`;
  let overview;
  try {
    overview = await request(`/api/skills?workspace=${encodeURIComponent(memoryWorkspace || "")}`);
  } catch (error) {
    elements.skillList.replaceChildren(el("div", "error-text", `读取技能失败：${error.message}`));
    return;
  }
  elements.skillsEnabled.checked = Boolean(overview.enabled);
  const directories = (overview.directories || []).filter((directory) => directory.exists);
  elements.skillDirsHint.textContent = directories.length
    ? `正在找：${directories.map((directory) => `${directory.path}（${directory.source}）`).join("；")}`
    : "没有找到技能目录";

  elements.skillList.replaceChildren();
  if ((overview.skills || []).length === 0) {
    elements.skillList.append(el("div", "panel-empty", "这个工作区还没有技能"));
  }
  for (const skill of overview.skills || []) {
    elements.skillList.append(renderSkill(skill));
  }

  if ((overview.problems || []).length > 0) {
    elements.skillProblems.textContent = overview.problems.join("\n");
    show(elements.skillProblems, true);
  }
}

function renderSkill(skill) {
  const item = el("div", "skill-item");
  const head = el("div", "skill-head");
  const enabled = !disabledSkills.has(skill.name);
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = enabled;
  checkbox.title = "启用这个技能";
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) {
      disabledSkills.delete(skill.name);
    } else {
      disabledSkills.add(skill.name);
    }
  });
  head.append(checkbox, el("span", "skill-name", skill.name), el("span", "skill-source", skill.source));
  item.append(head);
  if (skill.description) {
    item.append(el("div", "skill-desc", skill.description));
  }
  if ((skill.warnings || []).length > 0) {
    item.append(el("div", "skill-desc", `注意：${skill.warnings.join("；")}`));
  }

  const actions = el("div", "skill-actions");
  const view = button("ghost-button small", "查看说明");
  const content = el("pre", "memory-body hidden");
  view.addEventListener("click", async () => {
    if (!content.classList.contains("hidden")) {
      content.classList.add("hidden");
      view.textContent = "查看说明";
      return;
    }
    view.textContent = "收起";
    content.classList.remove("hidden");
    if (!content.textContent) {
      content.textContent = "加载中…";
      try {
        let text = skillContentCache.get(skill.path);
        if (!text) {
          const result = await request(`/api/skills/content?path=${encodeURIComponent(skill.path)}`);
          text = result.content || "";
          skillContentCache.set(skill.path, text);
        }
        content.textContent = text;
      } catch (error) {
        content.textContent = `读取失败：${error.message}`;
      }
    }
  });
  actions.append(view);
  item.append(actions, content);
  return item;
}

// ---- 记忆 ----

async function loadMemory() {
  elements.memoryContent.replaceChildren(el("div", "muted", "加载中…"));
  elements.memoryWorkspaceHint.textContent = `项目记忆按工作区存；当前看的是：${memoryWorkspace || "未选择工作区"}`;
  if (!memoryWorkspace) {
    elements.memoryContent.replaceChildren(el("div", "panel-empty", "先选一个工作区"));
    return;
  }
  let overview;
  try {
    overview = await request(`/api/memory?workspace=${encodeURIComponent(memoryWorkspace)}`);
  } catch (error) {
    elements.memoryContent.replaceChildren(el("div", "error-text", `读取记忆失败：${error.message}`));
    return;
  }
  elements.memoryEnabled.checked = Boolean(overview.enabled);
  elements.memoryContent.replaceChildren();
  elements.memoryContent.append(el("div", "field-hint", `记忆目录：${overview.directory}`));

  if (overview.index) {
    const indexBlock = el("div", "memory-block");
    indexBlock.append(el("div", "memory-head", "索引 MEMORY.md"));
    const pre = el("pre", "memory-body", overview.index);
    indexBlock.append(pre);
    elements.memoryContent.append(indexBlock);
  }

  if ((overview.memories || []).length === 0) {
    elements.memoryContent.append(el("div", "panel-empty", "这个工作区还没有记忆文件"));
  }
  for (const memory of overview.memories || []) {
    elements.memoryContent.append(renderMemory(memory));
  }
}

function renderMemory(memory) {
  const block = el("div", "memory-block");
  const head = el("div", "memory-head");
  head.append(
    el("span", "memory-name", memory.name || memory.file),
    el("span", "memory-meta", `${memory.type || ""} · ${memory.file}`),
  );
  const actions = el("div", "skill-actions");
  const view = button("ghost-button small", "查看");
  const content = el("pre", "memory-body hidden");
  view.addEventListener("click", async () => {
    if (!content.classList.contains("hidden")) {
      content.classList.add("hidden");
      view.textContent = "查看";
      return;
    }
    view.textContent = "收起";
    content.classList.remove("hidden");
    if (!content.textContent) {
      content.textContent = "加载中…";
      try {
        let text = memoryContentCache.get(memory.file);
        if (!text) {
          const result = await request(
            `/api/memory/file?workspace=${encodeURIComponent(memoryWorkspace)}&file=${encodeURIComponent(memory.file)}`,
          );
          text = result.content || "";
          memoryContentCache.set(memory.file, text);
        }
        content.textContent = text;
      } catch (error) {
        content.textContent = `读取失败：${error.message}`;
      }
    }
  });
  const remove = button("ghost-button small danger-text", "删除");
  remove.addEventListener("click", async () => {
    if (!window.confirm(`删除记忆「${memory.name || memory.file}」？`)) {
      return;
    }
    try {
      await sendJson(
        `/api/memory/file?workspace=${encodeURIComponent(memoryWorkspace)}&file=${encodeURIComponent(memory.file)}`,
        "DELETE",
      );
      memoryContentCache.delete(memory.file);
      await loadMemory();
    } catch (error) {
      window.alert(`删除失败：${error.message}`);
    }
  });
  actions.append(view, remove);
  block.append(head, el("div", "memory-meta", memory.description || ""), actions, content);
  return block;
}

// ---- 保存 ----

async function saveConfig() {
  let models;
  try {
    syncModelForm(true);
    models = draft.models;
  } catch (error) {
    setMessage(error.message, "error");
    return;
  }
  if (models.some((model) => !model.model)) {
    setMessage("每个模型都要填模型名", "error");
    return;
  }

  const payload = {
    models,
    default_model: draft.default_model,
    approval_mode: elements.defaultMode.value,
    busy_input_mode: elements.modal.querySelector('input[name="busy-input-mode"]:checked')?.value || "interject",
    max_steps: Number(elements.maxSteps.value) || 50,
    auto_compact: elements.autoCompact.checked,
    compact_threshold_percent: Number(elements.compactThreshold.value) || 80,
    web_search_enabled: elements.webSearchEnabled.checked,
    web_search_provider: elements.webSearchProvider.value,
    web_search_engine: elements.webSearchEngine.value,
    web_fetch_enabled: elements.webFetchEnabled.checked,
    computer_use_enabled: elements.computerUseEnabled.checked,
    sandbox_enabled: elements.sandboxEnabled.checked,
    sandbox_network: elements.sandboxNetwork.checked,
    allow_outside_workspace: elements.allowOutsideWorkspace.checked,
    skills_enabled: elements.skillsEnabled.checked,
    skill_dirs: linesOf(elements.skillDirs.value),
    disabled_skills: [...disabledSkills],
    memory_enabled: elements.memoryEnabled.checked,
    trusted_dirs: linesOf(elements.trustedDirs.value),
    default_workspace: elements.defaultWorkspace.value || null,
    user_name: elements.userName.value.trim() || null,
  };
  const searchKey = elements.webSearchKey.value.trim();
  if (searchKey) {
    payload.web_search_api_key = searchKey;
  }
  if (pendingClearSearchKey) {
    payload.clear_web_search_api_key = true;
  }

  setMessage("保存中…");
  try {
    config = await sendJson("/api/config", "PUT", payload);
  } catch (error) {
    setMessage(`保存失败：${error.message}`, "error");
    return;
  }
  pendingClearSearchKey = false;
  invalidateSkillNames();
  setMessage("已保存", "ok");
  elements.webSearchKey.value = "";
  await callbacks.onSaved?.(config);
}

function linesOf(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}
