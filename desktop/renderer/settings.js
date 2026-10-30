// 设置弹窗：模型、通用、工具、MCP、技能、记忆、用量。MCP 页写的是 ~/.pocketagent/mcp.json，其余写 config.json。
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
// 表单里现在填的是哪个模型：和 selectedModelId 对不上时（还没画、或换了模型）不能把表单写回去，
// 不然空表单 / 上一个模型的值会把当前模型的名字和模型名冲掉
let formModelId = null;
let disabledSkills = new Set();
let skillContentCache = new Map();
let memoryContentCache = new Map();
let memoryWorkspace = null;
let pendingClearSearchKey = false;
let currentTab = "general";
// MCP 页的草稿：服务器配置、当前选中、有没有和服务端同步过（和服务端存的 mcp.json 对应）
let mcpDraft = { servers: [], selected: null, loaded: false };
// 钩子页的草稿：每条钩子一个对象，随「保存」一起提交
let hookDraft = [];

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
  elements.appearance = document.getElementById("appearance-select");
  elements.fontSize = document.getElementById("font-size-select");
  elements.foldToolCalls = document.getElementById("fold-tool-calls-input");
  elements.defaultWorkspace = document.getElementById("default-workspace-select");
  elements.defaultWorkspaceHint = document.getElementById("default-workspace-hint");
  elements.defaultMode = document.getElementById("default-mode-select");
  elements.reviewModel = document.getElementById("review-model-select");
  elements.reviewReasoning = document.getElementById("review-reasoning-select");
  elements.subagentModel = document.getElementById("subagent-model-select");
  elements.subagentReasoning = document.getElementById("subagent-reasoning-select");
  elements.subagentExploreMinutes = document.getElementById("subagent-explore-minutes");
  elements.subagentGeneralMinutes = document.getElementById("subagent-general-minutes");
  elements.worktreeForeground = document.getElementById("worktree-foreground");
  elements.worktreeBackground = document.getElementById("worktree-background");
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
  elements.imageGenEnabled = document.getElementById("image-gen-enabled");
  elements.imageGenProvider = document.getElementById("image-gen-provider");
  elements.imageGenModel = document.getElementById("image-gen-model");
  elements.imageGenBaseUrl = document.getElementById("image-gen-base-url");
  elements.imageGenBaseUrlHint = document.getElementById("image-gen-base-url-hint");
  elements.imageGenKey = document.getElementById("image-gen-key");
  elements.imageGenKeyHint = document.getElementById("image-gen-key-hint");
  elements.imageSearchEnabled = document.getElementById("image-search-enabled");
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
  elements.mcpServerList = document.getElementById("mcp-server-list");
  elements.mcpForm = document.getElementById("mcp-form");
  elements.mcpFormEmpty = document.getElementById("mcp-form-empty");
  elements.mcpFormFields = document.getElementById("mcp-form-fields");
  elements.mcpResult = document.getElementById("mcp-result");
  elements.mcpStatus = document.getElementById("mcp-status");
  elements.mcpConfigPath = document.getElementById("mcp-config-path");
  elements.hookList = document.getElementById("hook-list");

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
  elements.modelForm.elements.protocol.addEventListener("change", paintModelReasoningOptions);
  document.getElementById("test-model").addEventListener("click", testModel);
  document.getElementById("make-default").addEventListener("click", makeDefault);
  document.getElementById("delete-model").addEventListener("click", deleteModel);

  elements.webSearchProvider.addEventListener("change", () => {
    paintWebSearchFields();
    elements.webSearchKeyHint.textContent = pendingClearSearchKey ? "保存后清除" : "留空表示不修改已保存的密钥";
  });
  elements.imageGenProvider.addEventListener("change", paintImageGenFields);
  elements.reviewModel.addEventListener("change", () =>
    renderAuxiliaryReasoningOptions(reviewSelects(), elements.reviewReasoning.value),
  );
  elements.subagentModel.addEventListener("change", () =>
    renderAuxiliaryReasoningOptions(subagentSelects(), elements.subagentReasoning.value),
  );
  elements.sandboxEnabled.addEventListener("change", paintSandboxFields);

  elements.mcpForm.addEventListener("input", onMcpFieldInput);
  elements.mcpForm.addEventListener("submit", (event) => event.preventDefault());
  elements.mcpForm.elements.transport.addEventListener("change", updateMcpTransportFields);
  document.getElementById("add-mcp-server").addEventListener("click", addMcpServer);
  document.getElementById("save-mcp").addEventListener("click", saveMcp);
  document.getElementById("reload-mcp").addEventListener("click", reloadMcp);
  document.getElementById("delete-mcp-server").addEventListener("click", deleteSelectedMcpServer);
  document.getElementById("add-hook").addEventListener("click", addHook);

  for (const tab of elements.tabs) {
    if (tab.dataset.tab === "models") {
      tab.classList.add("active");
    }
  }
}

export function isSettingsOpen() {
  return elements.modal && !elements.modal.classList.contains("hidden");
}

export async function openSettings(tab = "general") {
  // 每次打开都重新拉 MCP（外部改过 mcp.json、或者刚断开重连过，状态要新）
  mcpDraft.loaded = false;
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
  // 一个模型都还没配时直接停在「模型」页，第一次打开就是来加模型的
  switchTab(tab === "general" && draft.models.length === 0 ? "models" : tab);
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
  // MCP 页有自己的「保存并重连」，底部的全局保存按钮在这儿没有意义，先禁掉
  const saveButton = document.getElementById("settings-save");
  if (saveButton) {
    saveButton.disabled = name === "mcp";
  }
  if (name === "skills") {
    loadSkills();
  } else if (name === "memory") {
    loadMemory();
  } else if (name === "usage") {
    renderUsage(elements.usageContent);
  } else if (name === "mcp" && !mcpDraft.loaded) {
    loadMcp();
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
  hookDraft = (config.hooks || []).map(toDraftHook);
  skillContentCache = new Map();
  memoryContentCache = new Map();
  memoryWorkspace = currentWorkspace();
  pendingClearSearchKey = false;

  fillModels();
  renderModelForm();
  fillGeneral();
  renderHookList();

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
  elements.appearance.value = config.appearance || "system";
  elements.fontSize.value = config.font_size || "medium";
  elements.foldToolCalls.checked = config.fold_tool_calls !== false;
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
  renderAuxiliaryModelOptions(reviewSelects(), config.review_model, config.review_reasoning);
  renderAuxiliaryModelOptions(subagentSelects(), config.subagent_model, config.subagent_reasoning);
  elements.subagentExploreMinutes.value = config.subagent_explore_minutes;
  elements.subagentGeneralMinutes.value = config.subagent_general_minutes;
  elements.worktreeForeground.checked = Boolean(config.worktree_foreground);
  elements.worktreeBackground.checked = config.worktree_background !== false;

  elements.maxSteps.value = config.max_steps;
  elements.autoCompact.checked = Boolean(config.auto_compact);
  elements.compactThreshold.value = String(config.compact_threshold_percent);

  elements.webSearchEnabled.checked = Boolean(config.web_search_enabled);
  elements.webSearchProvider.value = config.web_search_provider || "bing";
  elements.webSearchEngine.value = config.web_search_engine || "search_std";
  elements.webSearchKey.value = "";
  elements.webSearchKeyHint.textContent = config.web_search_key_hint || "留空表示不修改已保存的密钥";
  elements.webFetchEnabled.checked = Boolean(config.web_fetch_enabled);

  elements.imageGenEnabled.checked = Boolean(config.image_gen_enabled);
  elements.imageGenProvider.value = config.image_gen_provider || "zhipu";
  elements.imageGenModel.value = config.image_gen_model || "";
  elements.imageGenBaseUrl.value = config.image_gen_base_url || "";
  elements.imageGenKey.value = "";
  elements.imageGenKeyHint.textContent = config.image_gen_key_hint
    ? `已单独配置 ${config.image_gen_key_hint}，留空表示不改`
    : config.image_gen_key_source || "没有可用的密钥：填一个，或者在模型里加一个智谱模型";
  elements.imageSearchEnabled.checked = Boolean(config.image_search_enabled);
  paintImageGenFields();

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

// 智谱用官方地址；OpenAI 兼容的可以走网关，没填就用第一个 OpenAI 兼容模型的地址（和 web 端一样）
// ---- AI 审批、子 agent 用的模型和思考档位（和 web 端 settings/settings.js 同一套） ----
// 「最低」= 这个模型能选的最低档（能关思考就关），「跟模型一样」= 用模型配置里的档位；选了具体模型时还能挑它支持的档位
const AUXILIARY_REASONING_FIXED = [
  { value: "lowest", label: "最低（能关思考就关）" },
  { value: "inherit", label: "跟模型配置一样" },
];

function renderAuxiliaryModelOptions(selects, selectedModel, selectedReasoning) {
  selects.model.replaceChildren(new Option("跟任务的模型一样", ""));
  for (const model of config.models || []) {
    selects.model.append(new Option(model.name, model.id));
  }
  selects.model.value = selectedModel || "";
  renderAuxiliaryReasoningOptions(selects, selectedReasoning);
}

function renderAuxiliaryReasoningOptions(selects, selectedReasoning) {
  const model = (config.models || []).find((each) => each.id === selects.model.value);
  const options = [...AUXILIARY_REASONING_FIXED];
  for (const choice of model?.reasoning_choices || []) {
    options.push({ value: choice, label: `${REASONING_LABELS[choice] || choice}（${choice}）` });
  }
  selects.reasoning.replaceChildren(...options.map((option) => new Option(option.label, option.value)));
  // 原来选的档位新模型不支持，就回到这一项的默认档
  const stillAvailable = options.some((option) => option.value === selectedReasoning);
  selects.reasoning.value = stillAvailable ? selectedReasoning : selects.fallbackReasoning;
}

function reviewSelects() {
  return { model: elements.reviewModel, reasoning: elements.reviewReasoning, fallbackReasoning: "lowest" };
}

function subagentSelects() {
  return { model: elements.subagentModel, reasoning: elements.subagentReasoning, fallbackReasoning: "inherit" };
}

function paintImageGenFields() {
  const usesOpenAI = elements.imageGenProvider.value === "openai";
  elements.imageGenModel.placeholder = usesOpenAI ? "留空用默认：gpt-image-1" : "留空用默认：cogview-3-flash";
  elements.imageGenBaseUrlHint.textContent = usesOpenAI
    ? "没填就用第一个 OpenAI 兼容模型的接口地址"
    : "智谱固定用 open.bigmodel.cn/api/paas/v4，不用填";
}

function paintSandboxFields() {
  elements.sandboxNetwork.disabled = !elements.sandboxEnabled.checked;
}

// ---- 模型表单 ----

function paintModelReasoningOptions() {
  const form = elements.modelForm;
  const model = selectedModel();
  // Anthropic 的思考块要原样带回历史，后端暂不提供思考档位
  const choices = form.elements.protocol.value === "anthropic" ? [] : model?.reasoning_choices || [];
  const select = form.elements.reasoning_effort;
  select.replaceChildren();
  const defaultOption = el("option", "", "默认（不传）");
  defaultOption.value = "";
  select.append(defaultOption);
  for (const choice of choices) {
    const option = el("option", "", REASONING_LABELS[choice] || choice);
    option.value = choice;
    select.append(option);
  }
  const selected = model?.reasoning_effort || "";
  select.value = choices.includes(selected) ? selected : "";
}

function renderModelForm() {
  const model = selectedModel();
  formModelId = model?.id || null;
  if (!model) {
    return;
  }
  const form = elements.modelForm;
  form.elements.name.value = model.name || "";
  form.elements.model.value = model.model || "";
  form.elements.protocol.value = model.protocol || "openai";
  form.elements.base_url.value = model.base_url || "";
  form.elements.api_key.value = "";
  form.elements.api_key.placeholder = model.api_key_hint ? `已保存：${model.api_key_hint}` : "sk-...";
  elements.apiKeyHint.textContent = model.api_key_hint ? `已保存的密钥：${model.api_key_hint}（留空表示不改）` : "还没保存密钥";

  paintModelReasoningOptions();

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
  if (!model || model.id !== formModelId) {
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
    protocol: form.elements.protocol.value || "openai",
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
  const data = readModelForm(strict);
  if (!data) {
    return;
  }
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
    protocol: "openai",
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
    review_model: elements.reviewModel.value || null,
    review_reasoning: elements.reviewReasoning.value,
    subagent_model: elements.subagentModel.value || null,
    subagent_reasoning: elements.subagentReasoning.value,
    subagent_explore_minutes: Number(elements.subagentExploreMinutes.value) || 10,
    subagent_general_minutes: Number(elements.subagentGeneralMinutes.value) || 40,
    worktree_foreground: elements.worktreeForeground.checked,
    worktree_background: elements.worktreeBackground.checked,
    busy_input_mode: elements.modal.querySelector('input[name="busy-input-mode"]:checked')?.value || "interject",
    max_steps: Number(elements.maxSteps.value) || 50,
    auto_compact: elements.autoCompact.checked,
    compact_threshold_percent: Number(elements.compactThreshold.value) || 80,
    web_search_enabled: elements.webSearchEnabled.checked,
    web_search_provider: elements.webSearchProvider.value,
    web_search_engine: elements.webSearchEngine.value,
    web_fetch_enabled: elements.webFetchEnabled.checked,
    image_gen_enabled: elements.imageGenEnabled.checked,
    image_gen_provider: elements.imageGenProvider.value,
    image_gen_model: elements.imageGenModel.value,
    image_gen_base_url: elements.imageGenBaseUrl.value,
    // 密钥留空表示不改（服务端 config_from_form 沿用已保存的）
    image_gen_api_key: elements.imageGenKey.value.trim(),
    image_search_enabled: elements.imageSearchEnabled.checked,
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
    appearance: elements.appearance.value,
    font_size: elements.fontSize.value,
    fold_tool_calls: elements.foldToolCalls.checked,
    hooks: hookPayload(),
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

// ---- MCP ----

// 服务端给的 overview → 界面草稿。env / headers 的值不回显，只列键名（提交时值为空 = 沿用原来的）
function toDraftMcp(server) {
  return {
    name: server.name || "",
    transport: server.transport === "http" ? "http" : "stdio",
    command: server.command || "",
    args: (server.args || []).join("\n"),
    env: Object.keys(server.env || {})
      .map((key) => `${key}=`)
      .join("\n"),
    url: server.url || "",
    headers: Object.keys(server.headers || {})
      .map((key) => `${key}: `)
      .join("\n"),
    timeout_seconds: String(server.timeout_seconds || 120),
    enabled: server.enabled !== false,
    connected: Boolean(server.connected),
    tool_count: server.tool_count || 0,
    tools: server.tools || [],
    problem: server.problem || "",
  };
}

function selectedMcpServer() {
  if (mcpDraft.selected === null) {
    return null;
  }
  return mcpDraft.servers[mcpDraft.selected] || null;
}

function renderMcpList() {
  const items = mcpDraft.servers.map((server, index) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "model-item";
    if (index === mcpDraft.selected) {
      item.classList.add("active");
    }
    let metaText = "未启用";
    if (server.enabled !== false) {
      metaText = server.connected ? `已连接 · ${server.tool_count} 个工具` : "未连接";
    }
    item.append(el("span", "model-item-name", server.name || "（未命名）"), el("span", "model-item-model", metaText));
    item.addEventListener("click", () => {
      mcpDraft.selected = index;
      renderMcpList();
      renderMcpForm();
    });
    return item;
  });
  elements.mcpServerList.replaceChildren(...items);
}

function renderMcpForm() {
  const server = selectedMcpServer();
  show(elements.mcpFormEmpty, !server);
  show(elements.mcpFormFields, Boolean(server));
  if (!server) {
    elements.mcpStatus.textContent = "";
    return;
  }
  const form = elements.mcpForm.elements;
  form.name.value = server.name;
  form.transport.value = server.transport;
  form.command.value = server.command;
  form.args.value = server.args;
  form.env.value = server.env;
  form.url.value = server.url;
  form.headers.value = server.headers;
  form.timeout_seconds.value = server.timeout_seconds;
  form.enabled.checked = server.enabled;
  updateMcpTransportFields();
  renderMcpStatus(server);
}

function updateMcpTransportFields() {
  const transport = elements.mcpForm.elements.transport.value;
  for (const field of elements.mcpForm.querySelectorAll("[data-mcp-stdio]")) {
    show(field, transport === "stdio");
  }
  for (const field of elements.mcpForm.querySelectorAll("[data-mcp-http]")) {
    show(field, transport === "http");
  }
}

function renderMcpStatus(server) {
  const parts = [];
  if (server.enabled === false) {
    parts.push("已停用");
  } else if (server.connected) {
    parts.push(`已连接，${server.tool_count} 个工具`);
    if (server.tools.length > 0) {
      parts.push(`工具：${server.tools.join("、")}`);
    }
  } else {
    parts.push("未连接");
  }
  if (server.problem) {
    parts.push(`问题：${server.problem}`);
  }
  elements.mcpStatus.textContent = parts.join(" · ");
}

function onMcpFieldInput(event) {
  const server = selectedMcpServer();
  if (!server) {
    return;
  }
  const target = event.target;
  switch (target.name) {
    case "name":
      server.name = target.value;
      renderMcpList();
      break;
    case "transport":
      server.transport = target.value;
      updateMcpTransportFields();
      break;
    case "command":
      server.command = target.value;
      break;
    case "args":
      server.args = target.value;
      break;
    case "env":
      server.env = target.value;
      break;
    case "url":
      server.url = target.value;
      break;
    case "headers":
      server.headers = target.value;
      break;
    case "timeout_seconds":
      server.timeout_seconds = target.value;
      break;
    case "enabled":
      server.enabled = target.checked;
      renderMcpList();
      renderMcpStatus(server);
      break;
    default:
      break;
  }
}

function addMcpServer() {
  mcpDraft.servers.push({
    name: `mcp-${mcpDraft.servers.length + 1}`,
    transport: "stdio",
    command: "",
    args: "",
    env: "",
    url: "",
    headers: "",
    timeout_seconds: "120",
    enabled: true,
    connected: false,
    tool_count: 0,
    tools: [],
    problem: "",
  });
  mcpDraft.selected = mcpDraft.servers.length - 1;
  renderMcpList();
  renderMcpForm();
  setMcpResult("");
}

function deleteSelectedMcpServer() {
  if (mcpDraft.selected === null) {
    return;
  }
  mcpDraft.servers.splice(mcpDraft.selected, 1);
  mcpDraft.selected = mcpDraft.servers.length
    ? Math.min(mcpDraft.selected, mcpDraft.servers.length - 1)
    : null;
  renderMcpList();
  renderMcpForm();
  setMcpResult("已删除，点「保存并重连」后生效", "ok");
}

async function loadMcp(force = false) {
  if (mcpDraft.loaded && !force) {
    return;
  }
  try {
    applyMcpOverview(await request("/api/mcp"));
  } catch (error) {
    setMcpResult(`读取 MCP 配置失败：${error.message}`, "error");
  }
}

function applyMcpOverview(overview) {
  mcpDraft.servers = (overview.servers || []).map(toDraftMcp);
  mcpDraft.loaded = true;
  if (mcpDraft.servers.length === 0) {
    mcpDraft.selected = null;
  } else if (mcpDraft.selected === null || mcpDraft.selected >= mcpDraft.servers.length) {
    mcpDraft.selected = 0;
  }
  elements.mcpConfigPath.textContent = overview.config_path ? `配置文件 ${overview.config_path}` : "";
  const problems = overview.problems || [];
  setMcpResult(problems.join("；"), problems.length > 0 ? "error" : "");
  renderMcpList();
  renderMcpForm();
}

function parseKeyValueLines(text, separator) {
  const result = {};
  for (const line of linesOf(text)) {
    const index = line.indexOf(separator);
    if (index <= 0) {
      continue;
    }
    const key = line.slice(0, index).trim();
    const value = line.slice(index + separator.length).trim();
    if (key) {
      result[key] = value;
    }
  }
  return result;
}

function mcpPayload() {
  return {
    servers: mcpDraft.servers.map((server) => ({
      name: server.name.trim(),
      transport: server.transport,
      command: server.command.trim(),
      args: linesOf(server.args),
      env: parseKeyValueLines(server.env, "="),
      url: server.url.trim(),
      headers: parseKeyValueLines(server.headers, ":"),
      timeout_seconds: Number(server.timeout_seconds) || 120,
      enabled: server.enabled,
    })),
  };
}

async function saveMcp() {
  const saveButton = document.getElementById("save-mcp");
  saveButton.disabled = true;
  setMcpResult("保存中…", "ok");
  try {
    const result = await sendJson("/api/mcp", "PUT", mcpPayload());
    applyMcpOverview(result.overview);
    if ((result.overview.problems || []).length === 0) {
      setMcpResult("已保存，配置已生效", "ok");
    }
  } catch (error) {
    setMcpResult(error.message, "error");
  } finally {
    saveButton.disabled = false;
  }
}

async function reloadMcp() {
  const reloadButton = document.getElementById("reload-mcp");
  reloadButton.disabled = true;
  setMcpResult("重新连接中…", "ok");
  try {
    const result = await sendJson("/api/mcp/reload", "POST", {});
    applyMcpOverview(result.overview);
    if ((result.overview.problems || []).length === 0) {
      setMcpResult("已重新连接", "ok");
    }
  } catch (error) {
    setMcpResult(error.message, "error");
  } finally {
    reloadButton.disabled = false;
  }
}

function setMcpResult(text, kind = "") {
  elements.mcpResult.textContent = text || "";
  elements.mcpResult.classList.toggle("ok", kind === "ok");
  elements.mcpResult.classList.toggle("error", kind === "error");
}

// ---- 钩子 ----

// 和后端 safety/hooks.py 的 HOOK_EVENTS 一一对应（按发生顺序排）
const HOOK_EVENTS = [
  { value: "session_start", label: "任务开始（第一轮之前）" },
  { value: "user_prompt_submit", label: "用户发来一句话（这一轮开始前）" },
  { value: "pre_tool_use", label: "工具调用之前（可以拦住）" },
  { value: "post_tool_use", label: "工具调用之后（可以补充提示）" },
  { value: "subagent_stop", label: "子 agent 交回结论之前" },
  { value: "pre_compact", label: "上下文压缩之前" },
  { value: "notification", label: "要你审批 / 回答之前" },
  { value: "turn_end", label: "一轮结束（答完 / 被拒绝 / 出错）" },
  { value: "session_end", label: "任务结束（删任务 / 退出）" },
];
// 只有这两个时机按工具名匹配
const TOOL_HOOK_EVENTS = ["pre_tool_use", "post_tool_use"];

function toDraftHook(hook) {
  return {
    event: hook.event || "pre_tool_use",
    matcher: hook.matcher || "",
    command: hook.command || "",
    timeout_seconds: String(hook.timeout_seconds || 30),
    enabled: hook.enabled !== false,
  };
}

function renderHookList() {
  if (!elements.hookList) {
    return;
  }
  if (hookDraft.length === 0) {
    elements.hookList.replaceChildren(
      el("div", "muted", "还没有钩子。比如加一个「工具调用之前」的钩子，工具名填 bash，命令写你自己的检查脚本。"),
    );
    return;
  }
  elements.hookList.replaceChildren(...hookDraft.map(renderHookItem));
}

function renderHookItem(hook, index) {
  const item = el("div", "hook-item");

  const head = el("div", "hook-head");
  const eventSelect = document.createElement("select");
  for (const choice of HOOK_EVENTS) {
    eventSelect.appendChild(new Option(choice.label, choice.value));
  }
  eventSelect.value = hook.event;
  eventSelect.addEventListener("change", () => {
    hook.event = eventSelect.value;
    // 换时机后工具名可能变得没意义，重画一遍把输入框禁掉
    renderHookList();
  });

  const matcher = document.createElement("input");
  matcher.type = "text";
  matcher.className = "hook-matcher";
  matcher.placeholder = "工具名，如 bash、edit_*、mcp__*；留空 = 全部";
  matcher.value = hook.matcher;
  matcher.disabled = !TOOL_HOOK_EVENTS.includes(hook.event);
  matcher.addEventListener("input", () => {
    hook.matcher = matcher.value;
  });

  const timeout = document.createElement("input");
  timeout.type = "number";
  timeout.min = "1";
  timeout.step = "1";
  timeout.className = "hook-timeout";
  timeout.value = hook.timeout_seconds;
  timeout.title = "超时秒数：超过就结束这个脚本，当它出错处理";
  timeout.addEventListener("input", () => {
    hook.timeout_seconds = timeout.value;
  });

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "checkbox-field hook-enabled";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = hook.enabled;
  enabled.addEventListener("change", () => {
    hook.enabled = enabled.checked;
  });
  enabledLabel.append(enabled, el("span", "", "启用"));

  const remove = button("ghost-button small danger-text", "删除");
  remove.addEventListener("click", () => {
    hookDraft.splice(index, 1);
    renderHookList();
  });

  head.append(
    eventSelect,
    matcher,
    el("span", "hook-unit", "超时"),
    timeout,
    el("span", "hook-unit", "秒"),
    enabledLabel,
    remove,
  );

  const command = document.createElement("textarea");
  command.rows = 2;
  command.className = "hook-command";
  command.placeholder = "要跑的 shell 命令，如 ~/bin/guard.sh，或者 npx prettier --write .";
  command.value = hook.command;
  command.addEventListener("input", () => {
    hook.command = command.value;
  });

  item.append(head, command);
  return item;
}

function addHook() {
  hookDraft.push({
    event: "pre_tool_use",
    matcher: "",
    command: "",
    timeout_seconds: "30",
    enabled: true,
  });
  renderHookList();
}

function hookPayload() {
  // 命令空着的行是加了还没填，保存时丢掉，免得后端报「命令不能为空」
  return hookDraft
    .filter((hook) => hook.command.trim())
    .map((hook) => ({
      event: hook.event,
      matcher: hook.matcher.trim(),
      command: hook.command.trim(),
      timeout_seconds: Number(hook.timeout_seconds) || 30,
      enabled: hook.enabled,
    }));
}
