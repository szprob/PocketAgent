// 设置弹窗：模型 / 通用 / 技能 / 记忆 / 用量 五页。模型和通用改完点保存，写进 ~/.pocketagent/config.json。

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
    defaultWorkspace: byId("default-workspace-select"),
    defaultWorkspaceHint: byId("default-workspace-hint"),
    defaultMode: byId("default-mode-select"),
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
  // 用量页只看不改，不需要保存按钮
  show(elements.footer, tabName !== "usage");
  if (tabName === "usage") {
    renderUsage(elements.usageContent);
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
  renderDefaultWorkspaceOptions(config);
  elements.maxSteps.value = config.max_steps;
  elements.userName.value = config.user_name;
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
  elements.sandboxHint.textContent = sandboxHint(config.sandbox_available);
  elements.allowOutsideWorkspace.checked = config.allow_outside_workspace;
  elements.skillsEnabled.checked = config.skills_enabled;
  elements.memoryEnabled.checked = config.memory_enabled;
  elements.skillDirs.value = (config.skill_dirs || []).join("\n");
  elements.trustedDirs.value = (config.trusted_dirs || []).join("\n");
  draft.disabledSkills = new Set(config.disabled_skills || []);
  loadSkills();
  elements.configPath.textContent = `保存在 ${config.config_path}（文件权限 600，只有你自己能读）`;
  setMessage("");
  renderModelList();
  renderModelForm();
  show(elements.modal, true);
  selectTab(tabName || "models");
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
  if (event.target.name === "base_url") {
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
    default_workspace: elements.defaultWorkspace.value || null,
    busy_input_mode: busyInput ? busyInput.value : "interject",
    max_steps: elements.maxSteps.value,
    user_name: elements.userName.value,
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
    allow_outside_workspace: elements.allowOutsideWorkspace.checked,
    skills_enabled: elements.skillsEnabled.checked,
    skill_dirs: pathLinesFromInput(elements.skillDirs),
    disabled_skills: [...draft.disabledSkills],
    trusted_dirs: pathLinesFromInput(elements.trustedDirs),
    memory_enabled: elements.memoryEnabled.checked,
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
