"""运行配置。

优先级：命令行参数 > 配置文件 ~/.pocketagent/config.json > 环境变量 > 默认值。
配置文件可以在 web 界面的「设置」里改，也可以手动编辑，格式：

    {
      "models": [
        {"id": "a1b2c3", "name": "DS 4.1 Flash", "model": "deepseek-v4.1-flash",
         "base_url": "https://your-gateway.example.com/v1", "api_key": "sk-...",
         "reasoning_effort": "low", "context_window_tokens": 128000,
         "temperature": null, "extra_body": {}, "supports_images": true}
      ],
      "default_model": "a1b2c3",
      "approval_mode": "ask",
      "busy_input_mode": "interject",
      "max_steps": 50,
      "workspaces": ["/Users/me/projects/foo"],
      "trusted_dirs": ["/Users/me/data"],
      "ripgrep_path": null,
      "user_name": "me",
      "web_token": "..."
    }

一个模型都没配时，才读这些环境变量：
- POCKETAGENT_MODEL / POCKETAGENT_BASE_URL（或 OPENAI_BASE_URL）/ POCKETAGENT_API_KEY（或 OPENAI_API_KEY）
- POCKETAGENT_REASONING_EFFORT / POCKETAGENT_EXTRA_BODY（JSON）/ POCKETAGENT_CONTEXT_WINDOW
POCKETAGENT_HOME 决定配置、会话、trace 放在哪，默认 ~/.pocketagent。
"""

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pocketagent.memory import project_memory_dir
from pocketagent.permissions import ApprovalMode
from pocketagent.sandbox import available_sandbox
from pocketagent.tools.image import (
    DEFAULT_IMAGE_BASE_URL_BY_PROVIDER,
    DEFAULT_IMAGE_MODEL_BY_PROVIDER,
    IMAGE_GEN_PROVIDERS,
    ImageGenSettings,
)
from pocketagent.tools.web import WebSearchSettings

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_CONTEXT_WINDOW_TOKENS = 256_000
DEFAULT_MAX_STEPS = 50
CONFIG_FILE_NAME = "config.json"

# agent 正在干活时用户又发了一句话：插话（下一次调用模型时带上）还是排队（这一轮结束后再跑）
BUSY_INPUT_INTERJECT = "interject"
BUSY_INPUT_QUEUE = "queue"
BUSY_INPUT_MODES = [BUSY_INPUT_INTERJECT, BUSY_INPUT_QUEUE]

DEFAULT_COMPACT_THRESHOLD_PERCENT = 80

# 各家接口能选的思考档位（2026-10-26 逐个实测）。界面上另有「默认」= 不传这个参数，用接口自己的默认。
# 个别模型和所在接口不一样（比如 OpenRouter 上的 Grok 不能关思考），在模型配置的 reasoning_choices 里单独写。
REASONING_CHOICES_BY_PROVIDER = {
    # DeepSeek 官方：none 关掉思考；medium / xhigh 会被并到 high
    "deepseek": ["none", "low", "high", "max"],
    # 智谱：始终思考，只认 low / high / max，其他值直接 400
    "bigmodel": ["low", "high", "max"],
    # OpenRouter：走它自己的 reasoning.effort
    "openrouter": ["none", "low", "medium", "high"],
    # 其他 OpenAI 兼容网关（new-api 等），走顶层 reasoning_effort
    "openai": ["none", "low", "medium", "high", "max"],
}


def provider_kind(base_url: str | None) -> str:
    """按接口地址认是哪家，决定思考档位怎么传、能选哪些。"""
    host = urlparse(base_url or "").hostname or ""
    if "deepseek.com" in host:
        return "deepseek"
    if "bigmodel.cn" in host:
        return "bigmodel"
    if "openrouter.ai" in host:
        return "openrouter"
    return "openai"


class ConfigError(ValueError):
    pass


# ---- 读字段的小工具：配置文件和网页表单都经过这里，统一做类型转换和校验 ----


def _optional_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    text = _optional_text(data, key)
    if text is None:
        raise ConfigError(f"{label}不能为空")
    return text


def _positive_int(data: dict[str, Any], key: str, default: int, label: str) -> int:
    value = data.get(key)
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{label}必须是整数：{value}")
    if number <= 0:
        raise ConfigError(f"{label}必须大于 0：{value}")
    return number


def _optional_positive_int(data: dict[str, Any], key: str, label: str) -> int | None:
    if data.get(key) in (None, ""):
        return None
    return _positive_int(data, key, 1, label)


def _optional_float(data: dict[str, Any], key: str, label: str) -> float | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{label}必须是数字：{value}")


def _json_object(data: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    """接受字典，或者网页文本框里填的 JSON 字符串。"""
    value = data.get(key)
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ConfigError(f"{label}不是合法 JSON：{error}")
    if not isinstance(value, dict):
        raise ConfigError(f"{label}必须是 JSON 对象（{{...}}）")
    return value


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _busy_input_mode(value: Any) -> str:
    mode = value or BUSY_INPUT_INTERJECT
    if mode not in BUSY_INPUT_MODES:
        raise ConfigError(f"运行中发消息的处理方式只能是 {' / '.join(BUSY_INPUT_MODES)}：{value}")
    return mode


def _boolean_with_default(data: dict[str, Any], key: str, default: bool) -> bool:
    if key not in data or data[key] is None:
        return default
    return _boolean(data, key)


def _text_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or []
    if isinstance(value, str):
        value = value.split(",")
    return [str(item).strip() for item in value if str(item).strip()]


def _compact_threshold_percent(data: dict[str, Any]) -> int:
    percent = _positive_int(data, "compact_threshold_percent", DEFAULT_COMPACT_THRESHOLD_PERCENT, "自动压缩阈值")
    if not 30 <= percent <= 95:
        raise ConfigError(f"自动压缩阈值要在 30%–95% 之间：{percent}")
    return percent


def _workspace_list(value: Any) -> list[str]:
    """去重、展开 ~、转成绝对路径；不检查目录是否存在（换电脑后目录可能暂时不在）。"""
    workspaces: list[str] = []
    for item in value or []:
        path = str(Path(str(item)).expanduser().resolve())
        if path not in workspaces:
            workspaces.append(path)
    return workspaces


def _workspace_path(value: Any) -> str | None:
    """单个目录：空着就是 None，否则和 _workspace_list 一样展开 ~、转绝对路径。"""
    text = str(value or "").strip()
    return str(Path(text).expanduser().resolve()) if text else None


def _name_list(value: Any) -> list[str]:
    names: list[str] = []
    for item in value or []:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return names


def _approval_mode(value: Any) -> ApprovalMode:
    try:
        return ApprovalMode(value or ApprovalMode.ASK.value)
    except ValueError:
        choices = " / ".join(mode.value for mode in ApprovalMode)
        raise ConfigError(f"审批模式只能是 {choices}：{value}")


def _key_hint(key: str | None) -> str:
    """给界面看的密钥提示，只露头尾。"""
    if not key:
        return ""
    if len(key) <= 10:
        return "已设置"
    return f"{key[:6]}…{key[-4:]}"


WEB_SEARCH_PROVIDERS = ["bing", "deepseek", "zhipu"]
# 搜索没单独配密钥时，用对应厂商模型的密钥
WEB_SEARCH_MODEL_KIND = {"deepseek": "deepseek", "zhipu": "bigmodel"}
WEB_SEARCH_ENGINES = ["search_std", "search_pro"]

# 生图没单独配密钥时，用对应厂商模型的密钥
IMAGE_GEN_MODEL_KIND = {"zhipu": "bigmodel", "openai": "openai"}


def _image_gen_provider(value: Any) -> str:
    provider = value or "zhipu"
    if provider not in IMAGE_GEN_PROVIDERS:
        raise ConfigError(f"生图服务只能是 {' / '.join(IMAGE_GEN_PROVIDERS)}：{value}")
    return provider


def _web_search_provider(value: Any) -> str:
    provider = value or "bing"
    if provider not in WEB_SEARCH_PROVIDERS:
        raise ConfigError(f"搜索服务只能是 {' / '.join(WEB_SEARCH_PROVIDERS)}：{value}")
    return provider


def _web_search_engine(value: Any) -> str:
    engine = value or "search_std"
    if engine not in WEB_SEARCH_ENGINES:
        raise ConfigError(f"搜索引擎只能是 {' / '.join(WEB_SEARCH_ENGINES)}：{value}")
    return engine


def new_model_id() -> str:
    return secrets.token_hex(4)


# ---- 模型配置 ----


@dataclass
class ModelProfile:
    """一个可选的模型：接口地址、密钥、思考档位、上下文长度都跟着模型走。"""

    id: str
    name: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    extra_body: dict[str, Any] = field(default_factory=dict)
    # 能不能看图：不能看的模型，用户上传的图片只以文件路径的形式告诉它
    supports_images: bool = False
    # 这个模型能选的思考档位；空就按接口地址用 REASONING_CHOICES_BY_PROVIDER 里的
    reasoning_choices: list[str] | None = None
    # 单次回复最多输出多少 token（max_tokens）；空着用接口默认
    max_output_tokens: int | None = None
    # 在会话的模型列表里显示；不显示的只在设置里，已经用着它的任务不受影响
    visible: bool = True

    @property
    def provider(self) -> str:
        return provider_kind(self.base_url)

    def available_reasoning_choices(self) -> list[str]:
        return self.reasoning_choices or REASONING_CHOICES_BY_PROVIDER[self.provider]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelProfile":
        model = _required_text(data, "model", "模型名")
        profile = cls(
            id=_optional_text(data, "id") or new_model_id(),
            name=_optional_text(data, "name") or model,
            model=model,
            base_url=_optional_text(data, "base_url"),
            api_key=_optional_text(data, "api_key"),
            reasoning_effort=_optional_text(data, "reasoning_effort"),
            temperature=_optional_float(data, "temperature", "温度"),
            context_window_tokens=_positive_int(
                data, "context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS, "上下文长度"
            ),
            extra_body=_json_object(data, "extra_body", "额外请求参数"),
            supports_images=_boolean(data, "supports_images"),
            reasoning_choices=_text_list(data, "reasoning_choices") or None,
            max_output_tokens=_optional_positive_int(data, "max_output_tokens", "最大输出 token"),
            visible=_boolean_with_default(data, "visible", True),
        )
        choices = profile.available_reasoning_choices()
        if profile.reasoning_effort and profile.reasoning_effort not in choices:
            raise ConfigError(
                f"「{profile.name}」的思考档位只能是 默认 / {' / '.join(choices)}，不能是 {profile.reasoning_effort}"
            )
        return profile

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def api_key_hint(self) -> str:
        return _key_hint(self.api_key)

    def public_dict(self) -> dict[str, Any]:
        """发给浏览器的版本：不带密钥本身。"""
        data = self.to_dict()
        data.pop("api_key")
        data["api_key_hint"] = self.api_key_hint()
        data["provider"] = self.provider
        # 单独配的（如 Grok 不能关思考）原样带回去，保存时不丢；available 是界面上实际可选的
        data["custom_reasoning_choices"] = self.reasoning_choices
        data["reasoning_choices"] = self.available_reasoning_choices()
        return data


@dataclass
class AppConfig:
    """配置文件的内容。"""

    models: list[ModelProfile] = field(default_factory=list)
    default_model: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.ASK
    busy_input_mode: str = BUSY_INPUT_INTERJECT
    max_steps: int = DEFAULT_MAX_STEPS
    # 上下文占用超过窗口的这个百分比就自动压缩
    auto_compact: bool = True
    compact_threshold_percent: int = DEFAULT_COMPACT_THRESHOLD_PERCENT
    # 联网：web_search 默认抓必应（免费），也可以换成智谱 Web Search（按次计费，密钥空着就用智谱模型的密钥）
    web_search_enabled: bool = True
    web_search_provider: str = "bing"
    web_search_engine: str = "search_std"
    web_search_api_key: str | None = None
    web_fetch_enabled: bool = True
    # 图片：generate_image 默认走智谱 CogView（cogview-3-flash 免费），密钥空着就用智谱模型的；
    # search_images 抓必应图片结果页，免费。模型 / 接口地址空着用 IMAGE 模块里的默认
    image_gen_enabled: bool = True
    image_gen_provider: str = "zhipu"
    image_gen_model: str | None = None
    image_gen_base_url: str | None = None
    image_gen_api_key: str | None = None
    image_search_enabled: bool = True
    # bash 沙箱：只能写工作目录和临时目录、读不到密钥；sandbox_network 决定沙箱里能不能联网
    sandbox_enabled: bool = True
    sandbox_network: bool = False
    # 技能：除了默认目录还去哪找；关掉的技能名
    skills_enabled: bool = True
    skill_dirs: list[str] = field(default_factory=list)
    disabled_skills: list[str] = field(default_factory=list)
    # 项目记忆：每个工作区一份跨会话的笔记
    memory_enabled: bool = True
    workspaces: list[str] = field(default_factory=list)
    # 免问目录：工作目录以外、读写都不用确认的目录（和密钥目录重叠的会被忽略，见 tools/base.py）
    trusted_dirs: list[str] = field(default_factory=list)
    # grep/glob 用的 ripgrep 可执行文件；空着就从 PATH 找，找不到回退纯 Python 实现
    ripgrep_path: str | None = None
    # 新任务默认用哪个工作区；空着就用列表里的第一个（启动目录会排第一）
    default_workspace: str | None = None
    user_name: str | None = None
    web_token: str | None = None

    def find_model(self, key: str | None) -> ModelProfile | None:
        """按 id、名称、模型名依次找。"""
        if not key:
            return None
        for attribute in ("id", "name", "model"):
            for profile in self.models:
                if getattr(profile, attribute) == key:
                    return profile
        return None

    def default_profile(self) -> ModelProfile | None:
        return self.find_model(self.default_model) or (self.models[0] if self.models else None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        models = [ModelProfile.from_dict(item) for item in data.get("models") or []]
        _check_unique([profile.id for profile in models], "模型 id")
        _check_unique([profile.name for profile in models], "模型名称")
        config = cls(
            models=models,
            default_model=_optional_text(data, "default_model"),
            approval_mode=_approval_mode(data.get("approval_mode")),
            busy_input_mode=_busy_input_mode(data.get("busy_input_mode")),
            max_steps=_positive_int(data, "max_steps", DEFAULT_MAX_STEPS, "单轮最多调用模型次数"),
            auto_compact=_boolean_with_default(data, "auto_compact", True),
            compact_threshold_percent=_compact_threshold_percent(data),
            web_search_enabled=_boolean_with_default(data, "web_search_enabled", True),
            web_search_provider=_web_search_provider(data.get("web_search_provider")),
            web_search_engine=_web_search_engine(data.get("web_search_engine")),
            web_search_api_key=_optional_text(data, "web_search_api_key"),
            web_fetch_enabled=_boolean_with_default(data, "web_fetch_enabled", True),
            image_gen_enabled=_boolean_with_default(data, "image_gen_enabled", True),
            image_gen_provider=_image_gen_provider(data.get("image_gen_provider")),
            image_gen_model=_optional_text(data, "image_gen_model"),
            image_gen_base_url=_optional_text(data, "image_gen_base_url"),
            image_gen_api_key=_optional_text(data, "image_gen_api_key"),
            image_search_enabled=_boolean_with_default(data, "image_search_enabled", True),
            sandbox_enabled=_boolean_with_default(data, "sandbox_enabled", True),
            sandbox_network=_boolean_with_default(data, "sandbox_network", False),
            skills_enabled=_boolean_with_default(data, "skills_enabled", True),
            skill_dirs=_workspace_list(data.get("skill_dirs")),
            disabled_skills=_name_list(data.get("disabled_skills")),
            memory_enabled=_boolean_with_default(data, "memory_enabled", True),
            workspaces=_workspace_list(data.get("workspaces")),
            trusted_dirs=_workspace_list(data.get("trusted_dirs")),
            ripgrep_path=_optional_text(data, "ripgrep_path"),
            default_workspace=_workspace_path(data.get("default_workspace")),
            user_name=_optional_text(data, "user_name"),
            web_token=_optional_text(data, "web_token"),
        )
        if config.default_model and config.find_model(config.default_model) is None:
            config.default_model = models[0].id if models else None
        # 默认工作区得在列表里；配的那个目录被移除了就当没配
        if config.default_workspace not in config.workspaces:
            config.default_workspace = None
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": [profile.to_dict() for profile in self.models],
            "default_model": self.default_model,
            "approval_mode": self.approval_mode.value,
            "busy_input_mode": self.busy_input_mode,
            "max_steps": self.max_steps,
            "auto_compact": self.auto_compact,
            "compact_threshold_percent": self.compact_threshold_percent,
            "web_search_enabled": self.web_search_enabled,
            "web_search_provider": self.web_search_provider,
            "web_search_engine": self.web_search_engine,
            "web_search_api_key": self.web_search_api_key,
            "web_fetch_enabled": self.web_fetch_enabled,
            "image_gen_enabled": self.image_gen_enabled,
            "image_gen_provider": self.image_gen_provider,
            "image_gen_model": self.image_gen_model,
            "image_gen_base_url": self.image_gen_base_url,
            "image_gen_api_key": self.image_gen_api_key,
            "image_search_enabled": self.image_search_enabled,
            "sandbox_enabled": self.sandbox_enabled,
            "sandbox_network": self.sandbox_network,
            "skills_enabled": self.skills_enabled,
            "skill_dirs": self.skill_dirs,
            "disabled_skills": self.disabled_skills,
            "memory_enabled": self.memory_enabled,
            "workspaces": self.workspaces,
            "trusted_dirs": self.trusted_dirs,
            "ripgrep_path": self.ripgrep_path,
            "default_workspace": self.default_workspace,
            "user_name": self.user_name,
            "web_token": self.web_token,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "models": [profile.public_dict() for profile in self.models],
            "default_model": self.default_profile().id if self.models else None,
            "approval_mode": self.approval_mode.value,
            "busy_input_mode": self.busy_input_mode,
            "max_steps": self.max_steps,
            "auto_compact": self.auto_compact,
            "compact_threshold_percent": self.compact_threshold_percent,
            "web_search_enabled": self.web_search_enabled,
            "web_search_provider": self.web_search_provider,
            "web_search_engine": self.web_search_engine,
            "web_search_key_hint": _key_hint(self.web_search_api_key),
            "web_search_key_source": self.web_search_key_source(),
            "web_fetch_enabled": self.web_fetch_enabled,
            "image_gen_enabled": self.image_gen_enabled,
            "image_gen_provider": self.image_gen_provider,
            "image_gen_model": self.image_gen_model or "",
            "image_gen_base_url": self.image_gen_base_url or "",
            "image_gen_key_hint": _key_hint(self.image_gen_api_key),
            "image_gen_key_source": self.image_gen_key_source(),
            "image_search_enabled": self.image_search_enabled,
            "sandbox_enabled": self.sandbox_enabled,
            "sandbox_network": self.sandbox_network,
            "sandbox_available": available_sandbox(),
            "skills_enabled": self.skills_enabled,
            "skill_dirs": self.skill_dirs,
            "disabled_skills": self.disabled_skills,
            "memory_enabled": self.memory_enabled,
            "workspaces": self.workspaces,
            "trusted_dirs": self.trusted_dirs,
            "default_workspace": self.resolved_default_workspace(),
            "user_name": self.display_user_name(),
            "reasoning_presets": REASONING_CHOICES_BY_PROVIDER,
        }

    def resolved_web_search_key(self, provider: str | None = None) -> str | None:
        """单独配的搜索密钥优先，没配就用对应厂商模型的密钥。"""
        if self.web_search_api_key:
            return self.web_search_api_key
        kind = WEB_SEARCH_MODEL_KIND.get(provider or self.web_search_provider)
        for profile in self.models:
            if profile.provider == kind and profile.api_key:
                return profile.api_key
        return None

    def web_search_key_source(self, provider: str | None = None) -> str:
        if self.web_search_api_key:
            return "单独配置的密钥"
        kind = WEB_SEARCH_MODEL_KIND.get(provider or self.web_search_provider)
        for profile in self.models:
            if profile.provider == kind and profile.api_key:
                return f"用模型「{profile.name}」的密钥"
        return ""

    def web_search_settings(self) -> "WebSearchSettings | None":
        if not self.web_search_enabled:
            return None
        if self.web_search_provider == "bing":
            return WebSearchSettings(provider="bing")
        # DeepSeek / 智谱都要密钥；没配就当搜索不可用
        key = self.resolved_web_search_key()
        if not key:
            return None
        return WebSearchSettings(provider=self.web_search_provider, api_key=key, engine=self.web_search_engine)

    def first_model_of_kind(self, kind: str) -> ModelProfile | None:
        for profile in self.models:
            if profile.provider == kind:
                return profile
        return None

    def resolved_image_gen_key(self) -> str | None:
        """单独配的生图密钥优先，没配就用对应厂商模型的密钥。"""
        if self.image_gen_api_key:
            return self.image_gen_api_key
        profile = self.first_model_of_kind(IMAGE_GEN_MODEL_KIND[self.image_gen_provider])
        return profile.api_key if profile is not None else None

    def image_gen_key_source(self) -> str:
        if self.image_gen_api_key:
            return "单独配置的密钥"
        profile = self.first_model_of_kind(IMAGE_GEN_MODEL_KIND[self.image_gen_provider])
        if profile is not None and profile.api_key:
            return f"用模型「{profile.name}」的密钥"
        return ""

    def image_gen_settings(self) -> "ImageGenSettings | None":
        """生图可用的配置；没开或者没有密钥就返回 None（工具列表里就没有 generate_image）。"""
        if not self.image_gen_enabled:
            return None
        key = self.resolved_image_gen_key()
        if not key:
            return None
        provider = self.image_gen_provider
        base_url = self.image_gen_base_url
        if provider == "openai" and not base_url:
            # OpenAI 兼容的接口地址空着时，用第一个同类型模型的网关地址（new-api 这类）
            profile = self.first_model_of_kind("openai")
            base_url = profile.base_url if profile is not None else None
        return ImageGenSettings(
            provider=provider,
            base_url=base_url or DEFAULT_IMAGE_BASE_URL_BY_PROVIDER[provider],
            api_key=key,
            model=self.image_gen_model or DEFAULT_IMAGE_MODEL_BY_PROVIDER[provider],
        )

    def display_user_name(self) -> str:
        return self.user_name or os.environ.get("USER") or "我"

    def add_workspace(self, workspace: Path) -> bool:
        """加进工作区列表，返回是否新加的。"""
        path = str(workspace.expanduser().resolve())
        if path in self.workspaces:
            return False
        self.workspaces.append(path)
        return True

    def resolved_default_workspace(self) -> str | None:
        """配的默认工作区；没配、或者那个目录已经不在列表里，就用第一个。"""
        if self.default_workspace in self.workspaces:
            return self.default_workspace
        return self.workspaces[0] if self.workspaces else None

    def set_default_workspace(self, workspace: Path | str) -> None:
        """把列表里的某个目录设成新任务的默认工作区。"""
        text = str(workspace).strip()
        if not text:
            raise ConfigError("没给工作区路径")
        path = str(Path(text).expanduser().resolve())
        if path not in self.workspaces:
            raise ConfigError(f"这个目录不在工作区列表里：{workspace}")
        self.default_workspace = path

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConfigError(f"{path} 不是合法 JSON：{error}")
        return cls.from_dict(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        # 里面有密钥，只给自己读写
        temporary.chmod(0o600)
        temporary.replace(path)


def _check_unique(values: list[str], label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise ConfigError(f"{label}重复：{value}")
        seen.add(value)


def config_path(home_dir: Path) -> Path:
    return home_dir / CONFIG_FILE_NAME


def default_home_dir() -> Path:
    configured = os.environ.get("POCKETAGENT_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".pocketagent"


# ---- 运行时设置 ----


@dataclass
class Settings:
    """一个 agent 运行时用的设置。web 端每个会话各有一份，切模型只改自己那份。"""

    model: str = DEFAULT_MODEL
    base_url: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    supports_images: bool = False
    # 思考档位怎么传：openrouter 用 reasoning.effort，其他用顶层 reasoning_effort
    reasoning_style: str = "top"
    max_output_tokens: int | None = None
    # 联网工具：web_search 为 None 表示没开（或没有密钥）
    web_search: WebSearchSettings | None = None
    web_fetch: bool = True
    # 图片工具：image_gen 为 None 表示没开（或没有密钥）
    image_gen: ImageGenSettings | None = None
    image_search: bool = True
    sandbox_enabled: bool = True
    sandbox_network: bool = False
    skills_enabled: bool = True
    skill_dirs: list[str] = field(default_factory=list)
    disabled_skills: list[str] = field(default_factory=list)
    trusted_dirs: list[str] = field(default_factory=list)
    ripgrep_path: str | None = None
    memory_enabled: bool = True
    # 当前用的是配置文件里哪个模型；为空表示来自环境变量
    model_profile_id: str | None = None
    model_profile_name: str = ""

    workspace: Path = field(default_factory=Path.cwd)
    # 临时任务：workspace 是 ~/.pocketagent/scratch 下自动建的目录，不是用户的某个项目
    ephemeral: bool = False
    approval_mode: ApprovalMode = ApprovalMode.ASK
    home_dir: Path = field(default_factory=default_home_dir)

    # 单轮最多调用模型多少次，防止死循环
    max_steps: int = DEFAULT_MAX_STEPS
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    # 估算的上下文占用超过窗口的这个比例就自动压缩；接口报上下文超长时不管开没开都会压缩后重试
    auto_compact: bool = True
    compact_threshold: float = DEFAULT_COMPACT_THRESHOLD_PERCENT / 100
    shell_timeout_seconds: int = 120
    max_tool_output_chars: int = 30_000

    @property
    def sessions_dir(self) -> Path:
        return self.home_dir / "sessions"

    @property
    def traces_dir(self) -> Path:
        return self.home_dir / "traces"

    @property
    def config_path(self) -> Path:
        return config_path(self.home_dir)

    @property
    def attachments_dir(self) -> Path:
        return self.home_dir / "attachments"

    @property
    def tool_outputs_root(self) -> Path:
        """工具输出太长时，完整内容落在这里（每个任务一个子目录）。"""
        return self.home_dir / "tool_outputs"

    @property
    def shadow_dir(self) -> Path:
        """影子 git 仓库放这里，一个工作区一个。"""
        return self.home_dir / "shadow"

    @property
    def memory_dir(self) -> Path:
        """这个工作区的项目记忆目录。"""
        return project_memory_dir(self.home_dir, self.workspace)

    @property
    def usage_log_path(self) -> Path:
        return self.home_dir / "usage.jsonl"

    def display_model(self) -> str:
        if self.model_profile_name and self.model_profile_name != self.model:
            return f"{self.model_profile_name}（{self.model}）"
        return self.model

    def use_profile(self, profile: ModelProfile) -> None:
        self.model = profile.model
        self.base_url = profile.base_url
        self.api_key = profile.api_key
        self.reasoning_effort = profile.reasoning_effort
        self.temperature = profile.temperature
        self.context_window_tokens = profile.context_window_tokens
        self.extra_body = dict(profile.extra_body)
        self.supports_images = profile.supports_images
        self.reasoning_style = "openrouter" if profile.provider == "openrouter" else "top"
        self.max_output_tokens = profile.max_output_tokens
        self.model_profile_id = profile.id
        self.model_profile_name = profile.name

    def apply_compaction(self, app_config: "AppConfig") -> None:
        self.auto_compact = app_config.auto_compact
        self.compact_threshold = app_config.compact_threshold_percent / 100

    def apply_app_config(self, app_config: "AppConfig") -> None:
        """配置文件里和模型无关的那些：步数上限、自动压缩、联网、沙箱、技能、记忆。"""
        self.max_steps = app_config.max_steps
        self.apply_compaction(app_config)
        self.web_search = app_config.web_search_settings()
        self.web_fetch = app_config.web_fetch_enabled
        self.image_gen = app_config.image_gen_settings()
        self.image_search = app_config.image_search_enabled
        self.sandbox_enabled = app_config.sandbox_enabled
        self.sandbox_network = app_config.sandbox_network
        self.skills_enabled = app_config.skills_enabled
        self.skill_dirs = list(app_config.skill_dirs)
        self.disabled_skills = list(app_config.disabled_skills)
        self.trusted_dirs = list(app_config.trusted_dirs)
        self.ripgrep_path = app_config.ripgrep_path
        self.memory_enabled = app_config.memory_enabled

    def apply_environment(self) -> None:
        environment = os.environ
        self.model = environment.get("POCKETAGENT_MODEL", self.model)
        self.base_url = environment.get("POCKETAGENT_BASE_URL") or environment.get("OPENAI_BASE_URL")
        self.api_key = environment.get("POCKETAGENT_API_KEY") or environment.get("OPENAI_API_KEY")
        self.reasoning_effort = environment.get("POCKETAGENT_REASONING_EFFORT") or None
        self.extra_body = _json_object(environment, "POCKETAGENT_EXTRA_BODY", "POCKETAGENT_EXTRA_BODY")
        self.context_window_tokens = _positive_int(
            environment, "POCKETAGENT_CONTEXT_WINDOW", self.context_window_tokens, "POCKETAGENT_CONTEXT_WINDOW"
        )

    @classmethod
    def load(
        cls,
        model: str | None = None,
        workspace: Path | None = None,
        approval_mode: ApprovalMode | None = None,
        ephemeral: bool = False,
    ) -> "Settings":
        """按优先级合成设置。model 可以是配置里的模型 id / 名称 / 模型名，都不匹配就当成模型名直接用。
        ephemeral 表示这是临时任务：真正的 workspace 由调用方拿到 home_dir 之后建好再设进来。"""
        home_dir = default_home_dir()
        app_config = AppConfig.load(config_path(home_dir))
        settings = cls(home_dir=home_dir, approval_mode=app_config.approval_mode, ephemeral=ephemeral)
        settings.apply_app_config(app_config)
        settings.apply_environment()
        profile = app_config.find_model(model) or app_config.default_profile()
        if profile is not None:
            settings.use_profile(profile)
        if model and app_config.find_model(model) is None:
            settings.model = model
        if workspace is not None:
            settings.workspace = workspace
        if approval_mode is not None:
            settings.approval_mode = approval_mode
        return settings
