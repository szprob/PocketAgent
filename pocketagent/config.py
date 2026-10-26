"""运行配置。

优先级：命令行参数 > 配置文件 ~/.pocketagent/config.json > 环境变量 > 默认值。
配置文件可以在 web 界面的「设置」里改，也可以手动编辑，格式：

    {
      "models": [
        {"id": "a1b2c3", "name": "DS 4.1 Flash", "model": "deepseek-v4.1-flash",
         "base_url": "https://your-gateway.example.com/v1", "api_key": "sk-...",
         "reasoning_effort": "low", "context_window_tokens": 128000,
         "temperature": null, "extra_body": {}}
      ],
      "default_model": "a1b2c3",
      "approval_mode": "ask",
      "max_steps": 50,
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

from pocketagent.permissions import ApprovalMode

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_MAX_STEPS = 50
CONFIG_FILE_NAME = "config.json"


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


def _approval_mode(value: Any) -> ApprovalMode:
    try:
        return ApprovalMode(value or ApprovalMode.ASK.value)
    except ValueError:
        choices = " / ".join(mode.value for mode in ApprovalMode)
        raise ConfigError(f"审批模式只能是 {choices}：{value}")


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelProfile":
        model = _required_text(data, "model", "模型名")
        return cls(
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
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def api_key_hint(self) -> str:
        """给界面看的密钥提示，只露头尾。"""
        if not self.api_key:
            return ""
        if len(self.api_key) <= 10:
            return "已设置"
        return f"{self.api_key[:6]}…{self.api_key[-4:]}"

    def public_dict(self) -> dict[str, Any]:
        """发给浏览器的版本：不带密钥本身。"""
        data = self.to_dict()
        data.pop("api_key")
        data["api_key_hint"] = self.api_key_hint()
        return data


@dataclass
class AppConfig:
    """配置文件的内容。"""

    models: list[ModelProfile] = field(default_factory=list)
    default_model: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.ASK
    max_steps: int = DEFAULT_MAX_STEPS
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
            max_steps=_positive_int(data, "max_steps", DEFAULT_MAX_STEPS, "单轮最多调用模型次数"),
            web_token=_optional_text(data, "web_token"),
        )
        if config.default_model and config.find_model(config.default_model) is None:
            config.default_model = models[0].id if models else None
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": [profile.to_dict() for profile in self.models],
            "default_model": self.default_model,
            "approval_mode": self.approval_mode.value,
            "max_steps": self.max_steps,
            "web_token": self.web_token,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "models": [profile.public_dict() for profile in self.models],
            "default_model": self.default_profile().id if self.models else None,
            "approval_mode": self.approval_mode.value,
            "max_steps": self.max_steps,
        }

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
    # 当前用的是配置文件里哪个模型；为空表示来自环境变量
    model_profile_id: str | None = None
    model_profile_name: str = ""

    workspace: Path = field(default_factory=Path.cwd)
    approval_mode: ApprovalMode = ApprovalMode.ASK
    home_dir: Path = field(default_factory=default_home_dir)

    # 单轮最多调用模型多少次，防止死循环
    max_steps: int = DEFAULT_MAX_STEPS
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    # 估算的上下文占用超过窗口的这个比例就压缩
    compact_threshold: float = 0.8
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
        self.model_profile_id = profile.id
        self.model_profile_name = profile.name

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
    ) -> "Settings":
        """按优先级合成设置。model 可以是配置里的模型 id / 名称 / 模型名，都不匹配就当成模型名直接用。"""
        home_dir = default_home_dir()
        app_config = AppConfig.load(config_path(home_dir))
        settings = cls(home_dir=home_dir, approval_mode=app_config.approval_mode, max_steps=app_config.max_steps)
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
