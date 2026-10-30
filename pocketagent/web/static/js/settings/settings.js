// 设置弹窗：模型 / 通用 / 工具 / MCP / 技能 / 记忆 / 用量。模型和通用改完点保存，写进 ~/.pocketagent/config.json；
// MCP 页有自己的「保存并重连」，写的是 ~/.pocketagent/mcp.json。

import { requestJson, sendJson } from "../core/api.js";
import { button, el, show } from "../ui/dom.js";
import { renderMarkdown } from "../ui/markdown.js";
import { renderMemoryPage } from "./memory.js";
import { reasoningLabel } from "../task/modelpicker.js";
import { MODE_LABELS, reloadInfo, state } from "../core/state.js";
import { renderUsage } from "./usage.js";

const MODEL_FORM_FIELDS = [
  "name",
  "model",
  "protocol",
  "base_url",
  "api_key",
  "reasoning_effort",
  "context_window_tokens",
  "temperature",
  "extra_body",
];
const DEFAULT_CONTEXT_WINDOW = 256000;

const elements = {};
// 设置面板里正在编辑、还没保存的配置
const draft = {
  models: [],
  defaultModel: null,
  selectedId: null,
  // 各家接口能选的思考档位（后端实测的那张表）
  reasoningPresets: {},
  // 技能页里关掉的技能名
  disabledSkills: new Set(),
  // 服务端给的模型列表（带各自能选的思考档位），AI 审批的下拉框用
  savedModels: [],
  // MCP 页：服务器草稿、当前选中的下标、有没有和服务端同步过
  mcpServers: [],
  selectedMcp: null,
  mcpLoaded: false,
  // 钩子页：每条钩子一个草稿，随「保存」一起提交
  hooks: [],
};

export function initSettings() {
  const byId = (id) => document.getElementById(id);
  Object.assign(elements, {
    modal: byId("settings-modal"),
    tabs: [...document.querySelectorAll(".settings-tab")],
    pages: [...document.querySelectorAll(".settings-page")],
    footer: byId("settings-footer"),
    message: byId("settings-message"),
    save: byId("settings-save"),
    modelList: byId("model-list"),
    modelForm: byId("model-form"),
    modelFormEmpty: byId("model-form-empty"),
    modelFormFields: byId("model-form-fields"),
    apiKeyHint: byId("api-key-hint"),
    supportsImages: byId("supports-images"),
    modelVisible: byId("model-visible"),
    testResult: byId("test-result"),
    testButton: byId("test-model"),
    makeDefaultButton: byId("make-default"),
    userName: byId("user-name-input"),
    appearance: byId("appearance-select"),
    fontSize: byId("font-size-select"),
    foldToolCalls: byId("fold-tool-calls-input"),
    defaultWorkspace: byId("default-workspace-select"),
    defaultWorkspaceHint: byId("default-workspace-hint"),
    defaultMode: byId("default-mode-select"),
    reviewModel: byId("review-model-select"),
    reviewReasoning: byId("review-reasoning-select"),
    subagentModel: byId("subagent-model-select"),
    subagentReasoning: byId("subagent-reasoning-select"),
    subagentExploreMinutes: byId("subagent-explore-minutes"),
    subagentGeneralMinutes: byId("subagent-general-minutes"),
    worktreeForeground: byId("worktree-foreground"),
    worktreeBackground: byId("worktree-background"),
    maxSteps: byId("max-steps-input"),
    busyModeInputs: [...document.querySelectorAll('input[name="busy-input-mode"]')],
    autoCompact: byId("auto-compact-input"),
    compactThreshold: byId("compact-threshold-select"),
    webSearchEnabled: byId("web-search-enabled"),
    webSearchProvider: byId("web-search-provider"),
    webSearchEngine: byId("web-search-engine"),
    webSearchEngineField: byId("web-search-engine-field"),
    webSearchKey: byId("web-search-key"),
    webSearchKeyField: byId("web-search-key-field"),
    webSearchKeyLabel: byId("web-search-key-label"),
    webSearchKeyHint: byId("web-search-key-hint"),
    webFetchEnabled: byId("web-fetch-enabled"),
    imageGenEnabled: byId("image-gen-enabled"),
    imageGenProvider: byId("image-gen-provider"),
    imageGenModel: byId("image-gen-model"),
    imageGenBaseUrlField: byId("image-gen-base-url-field"),
    imageGenBaseUrl: byId("image-gen-base-url"),
    imageGenBaseUrlHint: byId("image-gen-base-url-hint"),
    imageGenKey: byId("image-gen-key"),
    imageGenKeyHint: byId("image-gen-key-hint"),
    imageSearchEnabled: byId("image-search-enabled"),
    computerUseEnabled: byId("computer-use-enabled"),
    computerUseHint: byId("computer-use-hint"),
    sandboxEnabled: byId("sandbox-enabled"),
    sandboxNetwork: byId("sandbox-network"),
    sandboxNetworkAllowlist: byId("sandbox-network-allowlist"),
    sandboxHint: byId("sandbox-hint"),
    allowOutsideWorkspace: byId("allow-outside-workspace"),
    skillsEnabled: byId("skills-enabled"),
    skillList: byId("skill-list"),
    skillProblems: byId("skill-problems"),
    memoryEnabled: byId("memory-enabled"),
    memoryContent: byId("memory-content"),
    skillDirs: byId("skill-dirs-input"),
    skillDirsHint: byId("skill-dirs-hint"),
    trustedDirs: byId("trusted-dirs-input"),
    configPath: byId("config-path"),
    usageContent: byId("usage-content"),
    mcpServerList: byId("mcp-server-list"),
    mcpForm: byId("mcp-form"),
    mcpFormEmpty: byId("mcp-form-empty"),
    mcpFormFields: byId("mcp-form-fields"),
    mcpResult: byId("mcp-result"),
    mcpStatus: byId("mcp-status"),
    mcpConfigPath: byId("mcp-config-path"),
    hookList: byId("hook-list"),
  });

  for (const tab of elements.tabs) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  }
  byId("settings-close").addEventListener("click", closeSettings);
  byId("settings-cancel").addEventListener("click", closeSettings);
  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) {
      closeSettings();
    }
  });
  elements.save.addEventListener("click", saveSettings);
  elements.modelForm.addEventListener("input", onModelFieldInput);
  elements.modelForm.addEventListener("submit", (event) => event.preventDefault());
  byId("add-model").addEventListener("click", addModel);
  byId("delete-model").addEventListener("click", deleteSelectedModel);
  elements.makeDefaultButton.addEventListener("click", makeSelectedDefault);
  elements.testButton.addEventListener("click", testSelectedModel);
  elements.webSearchProvider.addEventListener("change", () => updateWebSearchFields());
  elements.imageGenProvider.addEventListener("change", () => updateImageGenFields());
  elements.reviewModel.addEventListener("change", () =>
    renderAuxiliaryReasoningOptions(reviewSelects(), elements.reviewReasoning.value)
  );
  elements.subagentModel.addEventListener("change", () =>
    renderAuxiliaryReasoningOptions(subagentSelects(), elements.subagentReasoning.value)
  );
  elements.mcpForm.addEventListener("input", onMcpFieldInput);
  elements.mcpForm.addEventListener("submit", (event) => event.preventDefault());
  elements.mcpForm.elements.namedItem("transport").addEventListener("change", updateMcpTransportFields);
  byId("add-mcp-server").addEventListener("click", addMcpServer);
  byId("delete-mcp-server").addEventListener("click", deleteSelectedMcpServer);
  byId("save-mcp").addEventListener("click", saveMcp);
  byId("reload-mcp").addEventListener("click", reloadMcp);
  byId("add-hook").addEventListener("click", addHook);
}

// 智谱的档位只在选智谱时才有意义；必应不用密钥，DeepSeek / 智谱要
function updateWebSearchFields() {
  const provider = elements.webSearchProvider.value;
  const usesZhipu = provider === "zhipu";
  const usesKey = provider !== "bing";
  show(elements.webSearchEngineField, usesZhipu);
  show(elements.webSearchKeyField, usesKey);
  elements.webSearchKeyLabel.textContent = usesZhipu ? "智谱搜索 API Key" : "DeepSeek 搜索 API Key";
  elements.webSearchKey.placeholder = usesZhipu ? "留空就用智谱模型的密钥" : "留空就用 DeepSeek 模型的密钥";
}

// 智谱用官方地址；OpenAI 兼容的可以走网关，没填就用第一个 OpenAI 兼容模型的地址
// ---- AI 审批、子 agent 用的模型和思考档位 ----
// 「最低」= 这个模型能选的最低档（能关思考就关），「跟模型一样」= 用模型配置里的档位；
// 选了具体模型时还能挑它支持的具体档位。和桌面端 settings.js 同一套
const AUXILIARY_REASONING_FIXED = [
  { value: "lowest", label: "最低（能关思考就关）" },
  { value: "inherit", label: "跟模型配置一样" },
];

function renderAuxiliaryModelOptions(selects, selectedModel, selectedReasoning) {
  selects.model.replaceChildren(new Option("跟任务的模型一样", ""));
  for (const model of draft.savedModels) {
    selects.model.appendChild(new Option(model.name, model.id));
  }
  selects.model.value = selectedModel || "";
  renderAuxiliaryReasoningOptions(selects, selectedReasoning);
}

function renderAuxiliaryReasoningOptions(selects, selectedReasoning) {
  const model = draft.savedModels.find((each) => each.id === selects.model.value);
  const options = [...AUXILIARY_REASONING_FIXED];
  for (const choice of model?.reasoning_choices || []) {
    options.push({ value: choice, label: `${reasoningLabel(choice)}（${choice}）` });
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

function updateImageGenFields() {
  const provider = elements.imageGenProvider.value;
  const usesOpenAI = provider === "openai";
  elements.imageGenModel.placeholder = usesOpenAI ? "留空用默认：gpt-image-1" : "留空用默认：cogview-3-flash";
  elements.imageGenBaseUrlHint.textContent = usesOpenAI
    ? "没填就用第一个 OpenAI 兼容模型的接口地址"
    : "智谱固定用 open.bigmodel.cn/api/paas/v4，不用填";
}

export function isSettingsOpen() {
  return !elements.modal.classList.contains("hidden");
}

export function closeSettings() {
  show(elements.modal, false);
}

function selectTab(tabName) {
  for (const tab of elements.tabs) {
    tab.classList.toggle("active", tab.dataset.tab === tabName);
  }
  for (const page of elements.pages) {
    show(page, page.dataset.page === tabName);
  }
  // 用量页只看不改，MCP 页有自己的一套按钮，都不需要底部的全局保存
  show(elements.footer, tabName !== "usage" && tabName !== "mcp");
  if (tabName === "usage") {
    renderUsage(elements.usageContent);
  }
  if (tabName === "mcp" && !draft.mcpLoaded) {
    loadMcp();
  }
  if (tabName === "memory") {
    renderMemoryPage(elements.memoryContent);
  }
}

// ---- 打开 ----

function textOrEmpty(value) {
  return value === null || value === undefined ? "" : String(value);
}

function formatJsonObject(value) {
  return value && Object.keys(value).length > 0 ? JSON.stringify(value) : "";
}

function toDraftModel(model) {
  return {
    id: model.id,
    name: textOrEmpty(model.name),
    model: textOrEmpty(model.model),
    protocol: model.protocol || "openai",
    base_url: textOrEmpty(model.base_url),
    api_key: "",
    api_key_hint: model.api_key_hint || "",
    clear_api_key: false,
    reasoning_effort: textOrEmpty(model.reasoning_effort),
    context_window_tokens: textOrEmpty(model.context_window_tokens),
    temperature: textOrEmpty(model.temperature),
    extra_body: formatJsonObject(model.extra_body),
    supports_images: Boolean(model.supports_images),
    visible: model.visible !== false,
    // 单独配过的思考档位（比如 Grok 不能关），保存时原样带回去
    custom_reasoning_choices: model.custom_reasoning_choices || null,
  };
}

// 和后端 config.provider_kind 同一套规则
function providerOf(baseUrl) {
  let host = "";
  try {
    host = new URL(baseUrl).hostname;
  } catch (notUrl) {
    return "openai";
  }
  if (host.includes("deepseek.com")) {
    return "deepseek";
  }
  if (host.includes("bigmodel.cn")) {
    return "bigmodel";
  }
  if (host.includes("openrouter.ai")) {
    return "openrouter";
  }
  return "openai";
}

function reasoningChoicesFor(model) {
  // Anthropic 的思考块要原样带回历史，后端暂不提供思考档位
  if (model.protocol === "anthropic") {
    return [];
  }
  return model.custom_reasoning_choices || draft.reasoningPresets[providerOf(model.base_url)] || [];
}

function renderReasoningOptions(model) {
  const select = elements.modelForm.elements.namedItem("reasoning_effort");
  const choices = reasoningChoicesFor(model);
  // 换了接口地址后原来的档位可能不支持了，回到默认
  if (model.reasoning_effort && !choices.includes(model.reasoning_effort)) {
    model.reasoning_effort = "";
  }
  select.replaceChildren();
  for (const value of [""].concat(choices)) {
    const option = el("option", "", value ? `${reasoningLabel(value)}（${value}）` : "默认（不传）");
    option.value = value;
    select.appendChild(option);
  }
  select.value = model.reasoning_effort;
}

function toPayloadModel(model) {
  const payload = {
    id: model.id,
    clear_api_key: model.clear_api_key,
    supports_images: model.supports_images,
    visible: model.visible,
    reasoning_choices: model.custom_reasoning_choices,
  };
  for (const field of MODEL_FORM_FIELDS) {
    payload[field] = model[field];
  }
  return payload;
}

// 默认工作区只能从已经加进来的工作区里挑；想加新的去左侧栏点 +
function renderDefaultWorkspaceOptions(config) {
  const select = elements.defaultWorkspace;
  select.replaceChildren();
  for (const workspace of state.info.workspaces) {
    const option = el("option", "", workspace.name);
    option.value = workspace.path;
    option.title = workspace.path;
    select.appendChild(option);
  }
  select.value = config.default_workspace || "";
  select.disabled = state.info.workspaces.length === 0;
  elements.defaultWorkspaceHint.textContent = state.info.workspaces.length
    ? "左侧栏工作区那一行的星标也能改"
    : "还没有工作区，先在左侧栏点 + 加一个目录";
}

export async function openSettings(tabName) {
  // 每次打开都重新拉 MCP（外部改过 mcp.json、或者刚断开重连过，状态要新）
  draft.mcpLoaded = false;
  const config = await requestJson("api/config");
  draft.models = config.models.map(toDraftModel);
  draft.defaultModel = config.default_model;
  draft.reasoningPresets = config.reasoning_presets;
  draft.selectedId = config.default_model || draft.models[0]?.id || null;

  elements.defaultMode.replaceChildren();
  for (const mode of state.info.modes) {
    const option = el("option", "", MODE_LABELS[mode] || mode);
    option.value = mode;
    elements.defaultMode.appendChild(option);
  }
  elements.defaultMode.value = config.approval_mode;
  draft.savedModels = config.models;
  renderAuxiliaryModelOptions(reviewSelects(), config.review_model, config.review_reasoning);
  renderAuxiliaryModelOptions(subagentSelects(), config.subagent_model, config.subagent_reasoning);
  elements.subagentExploreMinutes.value = config.subagent_explore_minutes;
  elements.subagentGeneralMinutes.value = config.subagent_general_minutes;
  elements.worktreeForeground.checked = Boolean(config.worktree_foreground);
  elements.worktreeBackground.checked = config.worktree_background !== false;
  renderDefaultWorkspaceOptions(config);
  elements.maxSteps.value = config.max_steps;
  elements.userName.value = config.user_name;
  elements.appearance.value = config.appearance || "system";
  elements.fontSize.value = config.font_size || "medium";
  elements.foldToolCalls.checked = config.fold_tool_calls !== false;
  for (const input of elements.busyModeInputs) {
    input.checked = input.value === config.busy_input_mode;
  }
  elements.autoCompact.checked = config.auto_compact;
  elements.compactThreshold.value = String(config.compact_threshold_percent);
  elements.webSearchEnabled.checked = config.web_search_enabled;
  elements.webSearchProvider.value = config.web_search_provider || "bing";
  elements.webSearchEngine.value = config.web_search_engine;
  elements.webSearchKey.value = "";
  elements.webSearchKeyHint.textContent = config.web_search_key_hint
    ? `已单独配置 ${config.web_search_key_hint}，留空表示不改`
    : config.web_search_key_source || "没有可用的密钥：填一个，或者在模型里加一个智谱模型";
  updateWebSearchFields();
  elements.webFetchEnabled.checked = config.web_fetch_enabled;
  elements.imageGenEnabled.checked = config.image_gen_enabled;
  elements.imageGenProvider.value = config.image_gen_provider || "zhipu";
  elements.imageGenModel.value = config.image_gen_model || "";
  elements.imageGenBaseUrl.value = config.image_gen_base_url || "";
  elements.imageGenKey.value = "";
  elements.imageGenKeyHint.textContent = config.image_gen_key_hint
    ? `已单独配置 ${config.image_gen_key_hint}，留空表示不改`
    : config.image_gen_key_source || "没有可用的密钥：填一个，或者在模型里加一个智谱模型";
  elements.imageSearchEnabled.checked = config.image_search_enabled;
  updateImageGenFields();
  elements.computerUseEnabled.checked = config.computer_use_enabled;
  elements.computerUseEnabled.disabled = !config.computer_use_available;
  elements.computerUseHint.textContent = config.computer_use_available
    ? "要给运行 PocketAgent 的应用打开「屏幕录制」和「辅助功能」权限（系统设置 → 隐私与安全性）。"
    : "目前只支持 macOS。";
  elements.sandboxEnabled.checked = config.sandbox_enabled;
  elements.sandboxNetwork.checked = config.sandbox_network;
  elements.sandboxNetworkAllowlist.value = (config.sandbox_network_allowlist || []).join("\n");
  elements.sandboxHint.textContent = sandboxHint(config.sandbox_available);
  elements.allowOutsideWorkspace.checked = config.allow_outside_workspace;
  elements.skillsEnabled.checked = config.skills_enabled;
  elements.memoryEnabled.checked = config.memory_enabled;
  elements.skillDirs.value = (config.skill_dirs || []).join("\n");
  elements.trustedDirs.value = (config.trusted_dirs || []).join("\n");
  draft.disabledSkills = new Set(config.disabled_skills || []);
  draft.hooks = (config.hooks || []).map(toDraftHook);
  renderHookList();
  loadSkills();
  elements.configPath.textContent = `保存在 ${config.config_path}（文件权限 600，只有你自己能读）`;
  setMessage("");
  renderModelList();
  renderModelForm();
  show(elements.modal, true);
  // 一个模型都还没配时直接停在「模型」页，第一次打开就是来加模型的
  const wantsGeneral = !tabName || tabName === "general";
  selectTab(wantsGeneral && draft.models.length === 0 ? "models" : tabName || "general");
}

function sandboxHint(kind) {
  if (kind === "seatbelt") {
    return "本机用 macOS 自带的 sandbox-exec。被拦下的命令，模型可以申请在沙箱外执行，那一次会问你。";
  }
  if (kind === "bubblewrap") {
    return "本机用 bubblewrap（bwrap）。被拦下的命令，模型可以申请在沙箱外执行，那一次会问你。";
  }
  return "这台机器没有可用的沙箱（macOS 要 sandbox-exec，Linux 要装 bubblewrap），命令会直接执行。";
}

// ---- 技能 ----

function currentWorkspace() {
  // 打开着某个任务就看那个任务的工作区，否则看默认工作区（项目级技能跟着工作区走）
  const task = state.tasks.find((each) => each.id === state.currentTaskId);
  return task?.workspace || state.info?.default_workspace || "";
}

async function loadSkills() {
  elements.skillList.replaceChildren(el("div", "muted", "正在找技能…"));
  const workspace = currentWorkspace();
  const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  let overview;
  try {
    overview = await requestJson(`api/skills${query}`);
  } catch (error) {
    elements.skillList.replaceChildren(el("div", "muted", `读不到技能：${error.message}`));
    return;
  }
  renderSkills(overview.skills);
  renderSkillProblems(overview.problems || []);
  const found = overview.directories.filter((directory) => directory.exists).map((directory) => directory.path);
  const missing = overview.directories.filter((directory) => !directory.exists).map((directory) => directory.path);
  elements.skillDirsHint.textContent =
    `在这些目录里找到了技能：${found.join("、") || "（没有）"}。` +
    (missing.length ? `还会看（目前不存在）：${missing.join("、")}。` : "") +
    (workspace ? `项目级技能看的是工作区 ${workspace}。` : "");
}

function renderSkills(skills) {
  if (skills.length === 0) {
    elements.skillList.replaceChildren(
      el("div", "muted", "还没有技能。把带 SKILL.md 的目录放进 ~/.pocketagent/skills、~/.agents/skills 或 ~/.claude/skills 就行。")
    );
    return;
  }
  elements.skillList.replaceChildren(...skills.map(renderSkillItem));
}

function renderSkillItem(skill) {
  const wrapper = el("div", "skill-item");
  const row = el("div", "skill-row");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = !draft.disabledSkills.has(skill.name);
  toggle.addEventListener("change", () => {
    if (toggle.checked) {
      draft.disabledSkills.delete(skill.name);
    } else {
      draft.disabledSkills.add(skill.name);
    }
  });
  toggle.title = "启用 / 停用这个技能";
  const text = el("div", "skill-text");
  const title = el("div", "skill-title");
  title.append(el("strong", "", skill.name), el("span", "skill-source", skill.source));
  text.append(title, el("div", "skill-description", skill.description || "（没写描述）"), el("div", "skill-path", skill.path));
  for (const warning of skill.warnings || []) {
    text.appendChild(el("div", "skill-warning", `不合规范：${warning}`));
  }
  const view = button("ghost-button small", "查看");
  const body = el("div", "skill-body markdown-view hidden");
  view.addEventListener("click", async () => {
    const opening = body.classList.contains("hidden");
    show(body, opening);
    view.textContent = opening ? "收起" : "查看";
    if (opening && !body.dataset.loaded) {
      body.textContent = "加载中…";
      try {
        const { content } = await requestJson(`api/skills/content?path=${encodeURIComponent(skill.path)}`);
        body.innerHTML = renderMarkdown(content);
        body.dataset.loaded = "1";
      } catch (error) {
        body.textContent = `读不了：${error.message}`;
      }
    }
  });
  row.append(toggle, text, view);
  wrapper.append(row, body);
  return wrapper;
}

function renderSkillProblems(problems) {
  show(elements.skillProblems, problems.length > 0);
  elements.skillProblems.replaceChildren(
    el("div", "skill-problems-title", `有 ${problems.length} 个技能没加载上或被同名的覆盖了：`),
    ...problems.map((problem) => el("div", "skill-problem", problem))
  );
}

function pathLinesFromInput(input) {
  return input.value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

// ---- 模型列表和表单 ----

function selectedModel() {
  return draft.models.find((model) => model.id === draft.selectedId) || null;
}

function setMessage(text, isOk) {
  elements.message.textContent = text;
  elements.message.classList.toggle("ok", Boolean(isOk));
}

function setTestResult(text, kind) {
  elements.testResult.textContent = text;
  elements.testResult.className = `test-result ${kind || ""}`;
}

function renderModelList() {
  elements.modelList.replaceChildren();
  for (const model of draft.models) {
    const item = el("button", "model-item");
    item.type = "button";
    item.classList.toggle("active", model.id === draft.selectedId);
    const nameLine = el("span", "model-item-name", model.name || model.model || "（未命名）");
    if (model.id === draft.defaultModel) {
      nameLine.appendChild(el("span", "default-badge", "默认"));
    }
    if (model.supports_images) {
      nameLine.appendChild(el("span", "vision-badge", "图"));
    }
    if (!model.visible) {
      nameLine.appendChild(el("span", "hidden-badge", "不在会话里显示"));
    }
    item.classList.toggle("hidden-model", !model.visible);
    item.append(nameLine, el("span", "model-item-model", model.model || "未填模型名"));
    item.addEventListener("click", () => {
      draft.selectedId = model.id;
      renderModelList();
      renderModelForm();
    });
    elements.modelList.appendChild(item);
  }
}

function renderApiKeyHint(model) {
  const hint = elements.apiKeyHint;
  hint.replaceChildren();
  if (model.api_key) {
    hint.append("保存后使用新填的密钥");
    return;
  }
  if (model.clear_api_key) {
    hint.append("保存后会清除已保存的密钥");
    return;
  }
  if (!model.api_key_hint) {
    hint.append("未设置");
    return;
  }
  hint.append(`已保存 ${model.api_key_hint}，留空表示不改`);
  const clearButton = button("link-button", "清除");
  clearButton.addEventListener("click", () => {
    model.clear_api_key = true;
    model.api_key = "";
    renderModelForm();
  });
  hint.appendChild(clearButton);
}

function renderModelForm() {
  const model = selectedModel();
  show(elements.modelFormEmpty, model === null);
  show(elements.modelFormFields, model !== null);
  setTestResult("");
  if (!model) {
    return;
  }
  for (const field of MODEL_FORM_FIELDS) {
    if (field !== "reasoning_effort") {
      elements.modelForm.elements.namedItem(field).value = model[field];
    }
  }
  renderReasoningOptions(model);
  elements.supportsImages.checked = model.supports_images;
  elements.modelVisible.checked = model.visible;
  renderApiKeyHint(model);
  elements.makeDefaultButton.disabled = model.id === draft.defaultModel;
}

function onModelFieldInput(event) {
  const model = selectedModel();
  if (!model) {
    return;
  }
  if (event.target === elements.modelVisible) {
    model.visible = elements.modelVisible.checked;
    renderModelList();
    return;
  }
  if (event.target === elements.supportsImages) {
    model.supports_images = elements.supportsImages.checked;
    renderModelList();
    return;
  }
  if (!MODEL_FORM_FIELDS.includes(event.target.name)) {
    return;
  }
  model[event.target.name] = event.target.value;
  if (event.target.name === "api_key") {
    if (event.target.value) {
      model.clear_api_key = false;
    }
    renderApiKeyHint(model);
  }
  if (event.target.name === "name" || event.target.name === "model") {
    renderModelList();
  }
  if (event.target.name === "base_url" || event.target.name === "protocol") {
    renderReasoningOptions(model);
  }
}

function randomModelId() {
  return Math.random().toString(16).slice(2, 10).padEnd(8, "0");
}

function addModel() {
  const model = toDraftModel({ id: randomModelId(), context_window_tokens: DEFAULT_CONTEXT_WINDOW });
  draft.models.push(model);
  draft.selectedId = model.id;
  if (!draft.defaultModel) {
    draft.defaultModel = model.id;
  }
  renderModelList();
  renderModelForm();
  elements.modelForm.elements.namedItem("name").focus();
}

function deleteSelectedModel() {
  const model = selectedModel();
  if (!model || !window.confirm(`删除模型「${model.name || model.model}」？保存后生效。`)) {
    return;
  }
  draft.models = draft.models.filter((each) => each.id !== model.id);
  const firstId = draft.models[0]?.id || null;
  if (draft.defaultModel === model.id) {
    draft.defaultModel = firstId;
  }
  draft.selectedId = firstId;
  renderModelList();
  renderModelForm();
}

function makeSelectedDefault() {
  draft.defaultModel = draft.selectedId;
  renderModelList();
  renderModelForm();
}

async function testSelectedModel() {
  const model = selectedModel();
  if (!model) {
    return;
  }
  setTestResult("测试中…");
  elements.testButton.disabled = true;
  try {
    const result = await sendJson("api/config/test", "POST", toPayloadModel(model));
    if (result.ok) {
      setTestResult(`✓ ${result.seconds} 秒，回复：${result.reply || "（空）"}`, "ok");
    } else {
      setTestResult(`✗ ${result.error}`, "error");
    }
  } catch (error) {
    setTestResult(`✗ ${error.message}`, "error");
  } finally {
    elements.testButton.disabled = false;
  }
}

// ---- 保存 ----

async function saveSettings() {
  const busyInput = elements.busyModeInputs.find((input) => input.checked);
  const payload = {
    models: draft.models.map(toPayloadModel),
    default_model: draft.defaultModel,
    approval_mode: elements.defaultMode.value,
    review_model: elements.reviewModel.value || null,
    review_reasoning: elements.reviewReasoning.value,
    subagent_model: elements.subagentModel.value || null,
    subagent_reasoning: elements.subagentReasoning.value,
    subagent_explore_minutes: elements.subagentExploreMinutes.value,
    subagent_general_minutes: elements.subagentGeneralMinutes.value,
    worktree_foreground: elements.worktreeForeground.checked,
    worktree_background: elements.worktreeBackground.checked,
    default_workspace: elements.defaultWorkspace.value || null,
    busy_input_mode: busyInput ? busyInput.value : "interject",
    max_steps: elements.maxSteps.value,
    user_name: elements.userName.value,
    appearance: elements.appearance.value,
    font_size: elements.fontSize.value,
    fold_tool_calls: elements.foldToolCalls.checked,
    auto_compact: elements.autoCompact.checked,
    compact_threshold_percent: elements.compactThreshold.value,
    web_search_enabled: elements.webSearchEnabled.checked,
    web_search_provider: elements.webSearchProvider.value,
    web_search_engine: elements.webSearchEngine.value,
    web_search_api_key: elements.webSearchKey.value,
    web_fetch_enabled: elements.webFetchEnabled.checked,
    image_gen_enabled: elements.imageGenEnabled.checked,
    image_gen_provider: elements.imageGenProvider.value,
    image_gen_model: elements.imageGenModel.value,
    image_gen_base_url: elements.imageGenBaseUrl.value,
    image_gen_api_key: elements.imageGenKey.value,
    image_search_enabled: elements.imageSearchEnabled.checked,
    computer_use_enabled: elements.computerUseEnabled.checked,
    sandbox_enabled: elements.sandboxEnabled.checked,
    sandbox_network: elements.sandboxNetwork.checked,
    sandbox_network_allowlist: pathLinesFromInput(elements.sandboxNetworkAllowlist),
    allow_outside_workspace: elements.allowOutsideWorkspace.checked,
    skills_enabled: elements.skillsEnabled.checked,
    skill_dirs: pathLinesFromInput(elements.skillDirs),
    disabled_skills: [...draft.disabledSkills],
    trusted_dirs: pathLinesFromInput(elements.trustedDirs),
    memory_enabled: elements.memoryEnabled.checked,
    hooks: hookPayload(),
  };
  elements.save.disabled = true;
  setMessage("保存中…", true);
  try {
    await sendJson("api/config", "PUT", payload);
  } catch (error) {
    setMessage(error.message);
    return;
  } finally {
    elements.save.disabled = false;
  }
  closeSettings();
  await reloadInfo();
}

// ---- MCP ----

// 服务端给的 overview → 界面草稿。env / headers 的值不回显，只列键名（提交时值为空 = 沿用原来的）
function toDraftMcp(server) {
  return {
    name: textOrEmpty(server.name),
    transport: server.transport === "http" ? "http" : "stdio",
    command: textOrEmpty(server.command),
    args: (server.args || []).join("\n"),
    env: Object.keys(server.env || {})
      .map((key) => `${key}=`)
      .join("\n"),
    url: textOrEmpty(server.url),
    headers: Object.keys(server.headers || {})
      .map((key) => `${key}: `)
      .join("\n"),
    timeout_seconds: textOrEmpty(server.timeout_seconds || 120),
    enabled: server.enabled !== false,
    connected: Boolean(server.connected),
    tool_count: server.tool_count || 0,
    tools: server.tools || [],
    problem: server.problem || "",
  };
}

function selectedMcpServer() {
  if (draft.selectedMcp === null) {
    return null;
  }
  return draft.mcpServers[draft.selectedMcp] || null;
}

function renderMcpList() {
  const items = draft.mcpServers.map((server, index) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "model-item";
    if (index === draft.selectedMcp) {
      item.classList.add("active");
    }
    let metaText = "未启用";
    if (server.enabled !== false) {
      metaText = server.connected ? `已连接 · ${server.tool_count} 个工具` : "未连接";
    }
    item.append(el("span", "model-item-name", server.name || "（未命名）"), el("span", "model-item-model", metaText));
    item.addEventListener("click", () => {
      draft.selectedMcp = index;
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
  form.namedItem("name").value = server.name;
  form.namedItem("transport").value = server.transport;
  form.namedItem("command").value = server.command;
  form.namedItem("args").value = server.args;
  form.namedItem("env").value = server.env;
  form.namedItem("url").value = server.url;
  form.namedItem("headers").value = server.headers;
  form.namedItem("timeout_seconds").value = server.timeout_seconds;
  form.namedItem("enabled").checked = server.enabled;
  updateMcpTransportFields();
  renderMcpStatus(server);
}

function updateMcpTransportFields() {
  const transport = elements.mcpForm.elements.namedItem("transport").value;
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
  draft.mcpServers.push({
    name: `mcp-${draft.mcpServers.length + 1}`,
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
  draft.selectedMcp = draft.mcpServers.length - 1;
  renderMcpList();
  renderMcpForm();
  setMcpResult("");
}

function deleteSelectedMcpServer() {
  if (draft.selectedMcp === null) {
    return;
  }
  draft.mcpServers.splice(draft.selectedMcp, 1);
  draft.selectedMcp = draft.mcpServers.length
    ? Math.min(draft.selectedMcp, draft.mcpServers.length - 1)
    : null;
  renderMcpList();
  renderMcpForm();
  setMcpResult("已删除，点「保存并重连」后生效", "ok");
}

async function loadMcp(force = false) {
  if (draft.mcpLoaded && !force) {
    return;
  }
  try {
    applyMcpOverview(await requestJson("api/mcp"));
  } catch (error) {
    setMcpResult(`读取 MCP 配置失败：${error.message}`, "error");
  }
}

function applyMcpOverview(overview) {
  draft.mcpServers = (overview.servers || []).map(toDraftMcp);
  draft.mcpLoaded = true;
  if (draft.mcpServers.length === 0) {
    draft.selectedMcp = null;
  } else if (draft.selectedMcp === null || draft.selectedMcp >= draft.mcpServers.length) {
    draft.selectedMcp = 0;
  }
  elements.mcpConfigPath.textContent = overview.config_path ? `配置文件 ${overview.config_path}` : "";
  const problems = overview.problems || [];
  setMcpResult(problems.join("；"), problems.length > 0 ? "error" : "");
  renderMcpList();
  renderMcpForm();
}

function parseLines(text) {
  return String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseKeyValueLines(text, separator) {
  const result = {};
  for (const line of parseLines(text)) {
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
    servers: draft.mcpServers.map((server) => ({
      name: server.name.trim(),
      transport: server.transport,
      command: server.command.trim(),
      args: parseLines(server.args),
      env: parseKeyValueLines(server.env, "="),
      url: server.url.trim(),
      headers: parseKeyValueLines(server.headers, ":"),
      timeout_seconds: Number(server.timeout_seconds) || 120,
      enabled: server.enabled,
    })),
  };
}

async function saveMcp() {
  const button = document.getElementById("save-mcp");
  button.disabled = true;
  setMcpResult("保存中…", "ok");
  try {
    const result = await sendJson("api/mcp", "PUT", mcpPayload());
    applyMcpOverview(result.overview);
    if ((result.overview.problems || []).length === 0) {
      setMcpResult("已保存，配置已生效", "ok");
    }
  } catch (error) {
    setMcpResult(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function reloadMcp() {
  const button = document.getElementById("reload-mcp");
  button.disabled = true;
  setMcpResult("重新连接中…", "ok");
  try {
    const result = await sendJson("api/mcp/reload", "POST", {});
    applyMcpOverview(result.overview);
    if ((result.overview.problems || []).length === 0) {
      setMcpResult("已重新连接", "ok");
    }
  } catch (error) {
    setMcpResult(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function setMcpResult(text, kind) {
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
    matcher: textOrEmpty(hook.matcher),
    command: textOrEmpty(hook.command),
    timeout_seconds: textOrEmpty(hook.timeout_seconds || 30),
    enabled: hook.enabled !== false,
  };
}

function renderHookList() {
  if (draft.hooks.length === 0) {
    elements.hookList.replaceChildren(
      el("div", "muted", "还没有钩子。比如加一个「工具调用之前」的钩子，工具名填 bash，命令写你自己的检查脚本。")
    );
    return;
  }
  elements.hookList.replaceChildren(...draft.hooks.map(renderHookItem));
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
    draft.hooks.splice(index, 1);
    renderHookList();
  });

  head.append(eventSelect, matcher, el("span", "hook-unit", "超时"), timeout, el("span", "hook-unit", "秒"),
    enabledLabel, remove);

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
  draft.hooks.push({
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
  return draft.hooks
    .filter((hook) => hook.command.trim())
    .map((hook) => ({
      event: hook.event,
      matcher: hook.matcher.trim(),
      command: hook.command.trim(),
      timeout_seconds: Number(hook.timeout_seconds) || 30,
      enabled: hook.enabled,
    }));
}
