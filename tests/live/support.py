"""真实 API 联调的公共设施：找可用模型、造 provider、搭临时 agent。

默认只在 `make test-live`（或环境变量 POCKETAGENT_LIVE=1）时运行，普通 `make test` 会把整套跳过。
模型按这个顺序找：

1. POCKETAGENT_LIVE_MODEL + POCKETAGENT_LIVE_BASE_URL（CI 里用环境变量注入）
2. POCKETAGENT_LIVE_MODEL（按名字去 ~/.pocketagent/config.json 里找）
3. POCKETAGENT_LIVE_PROFILE（按 id / 名称 / 模型名指定配置里的某个模型）
4. ~/.pocketagent/config.json 里第一个免费模型（模型名带 :free，或名称里写「免费」）

只自动挑免费模型，避免跑测试的时候偷偷花钱；想用付费模型就用上面几种方式显式指定。
"""

import os
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from pocketagent.config import AppConfig, ModelProfile, Settings, config_path, default_home_dir
from pocketagent.core.agent import Agent, create_agent
from pocketagent.llm.base import LLMProvider, StreamListener
from pocketagent.safety.permissions import AllowAllApprover, ApprovalMode
from pocketagent.tools.image import ImageGenSettings
from pocketagent.tools.web import WebSearchSettings
from tests.helpers import EventRecorder, TemporaryDirectories

LIVE_ENV = "POCKETAGENT_LIVE"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ZHIPU_IMAGE_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_FREE_IMAGE_MODEL = "cogview-3-flash"


@dataclass
class LiveModel:
    """联调用的一套模型配置。"""

    name: str
    model: str
    base_url: str | None
    api_key: str | None
    protocol: str = "openai"
    reasoning_effort: str | None = None
    extra_body: dict = field(default_factory=dict)
    supports_images: bool = False


@dataclass
class LiveHarness:
    agent: Agent
    provider: LLMProvider
    recorder: EventRecorder
    settings: Settings
    directories: TemporaryDirectories


class RecordingListener:
    """收集流式增量，测试里用来确认 provider 真的在边收边发。"""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.retries: list[tuple[int, Exception]] = []

    async def on_text(self, text: str) -> None:
        self.text_parts.append(text)

    async def on_reasoning(self, text: str) -> None:
        self.reasoning_parts.append(text)

    async def on_retry(self, attempt: int, error: Exception, wait_seconds: float) -> None:
        self.retries.append((attempt, error))


def live_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def load_app_config() -> AppConfig | None:
    try:
        return AppConfig.load(config_path(default_home_dir()))
    except Exception:
        return None


def _is_free_profile(profile: ModelProfile) -> bool:
    lowered_model = profile.model.lower()
    lowered_name = profile.name.lower()
    return ":free" in lowered_model or "free" in lowered_name or "免费" in profile.name


def _from_profile(profile: ModelProfile) -> LiveModel:
    return LiveModel(
        name=profile.name,
        model=profile.model,
        base_url=profile.base_url,
        api_key=profile.api_key,
        protocol=profile.protocol,
        reasoning_effort=profile.reasoning_effort,
        extra_body=dict(profile.extra_body),
        supports_images=profile.supports_images,
    )


def resolve_live_model() -> LiveModel | None:
    environment_model = os.environ.get("POCKETAGENT_LIVE_MODEL")
    environment_base_url = os.environ.get("POCKETAGENT_LIVE_BASE_URL")
    if environment_model and environment_base_url:
        return LiveModel(
            name="环境变量",
            model=environment_model,
            base_url=environment_base_url,
            api_key=os.environ.get("POCKETAGENT_LIVE_API_KEY") or "EMPTY",
            protocol=os.environ.get("POCKETAGENT_LIVE_PROTOCOL") or "openai",
            reasoning_effort=os.environ.get("POCKETAGENT_LIVE_REASONING_EFFORT") or None,
        )
    app_config = load_app_config()
    if app_config is None:
        return None
    requested = os.environ.get("POCKETAGENT_LIVE_PROFILE") or environment_model
    profile = app_config.find_model(requested) if requested else None
    if profile is None and not requested:
        profile = next((item for item in app_config.models if _is_free_profile(item)), None)
    if profile is None or not profile.api_key:
        return None
    return _from_profile(profile)


LIVE_MODEL = resolve_live_model()
LIVE_DISABLED_REASON = f"真实 API 测试默认不跑：用 make test-live（{LIVE_ENV}=1）"
NO_MODEL_REASON = (
    "没找到可用的联调模型：设 POCKETAGENT_LIVE_MODEL/BASE_URL/API_KEY，"
    "或在 ~/.pocketagent/config.json 里配一个 :free 模型"
)
SKIP_WITHOUT_MODEL = LIVE_DISABLED_REASON if not live_enabled() else (None if LIVE_MODEL else NO_MODEL_REASON)


def build_live_provider(model: LiveModel) -> LLMProvider:
    if model.protocol == "anthropic":
        from pocketagent.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model.model,
            base_url=model.base_url,
            api_key=model.api_key,
            temperature=0,
            reasoning_effort=model.reasoning_effort,
            extra_body=model.extra_body,
            timeout_seconds=180,
        )
    from pocketagent.llm.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        model=model.model,
        base_url=model.base_url,
        api_key=model.api_key,
        temperature=0,
        reasoning_effort=model.reasoning_effort,
        extra_body=model.extra_body,
        timeout_seconds=180,
        # OpenRouter 的思考档位有自己的写法，其余网关用顶层 reasoning_effort
        reasoning_style="openrouter" if "openrouter.ai" in (model.base_url or "") else "top",
    )


def live_settings(workspace: Path, home_dir: Path) -> Settings:
    """联调用的设置：全自动、不开沙箱、关掉技能和记忆，避免临时环境里混进本机的东西。"""
    return Settings(
        model=LIVE_MODEL.model,
        base_url=LIVE_MODEL.base_url,
        api_key=LIVE_MODEL.api_key,
        protocol=LIVE_MODEL.protocol,
        supports_images=LIVE_MODEL.supports_images,
        workspace=workspace,
        home_dir=home_dir,
        approval_mode=ApprovalMode.FULL_AUTO,
        sandbox_enabled=False,
        skills_enabled=False,
        memory_enabled=False,
        image_search=False,
        web_search=WebSearchSettings(provider="bing"),
        max_steps=10,
    )


def build_live_agent(directories: TemporaryDirectories) -> LiveHarness:
    provider = build_live_provider(LIVE_MODEL)
    settings = live_settings(directories.workspace, directories.home_dir)
    agent = create_agent(settings, AllowAllApprover(), provider=provider)
    recorder = EventRecorder()
    agent.context.events.subscribe(recorder)
    return LiveHarness(
        agent=agent, provider=provider, recorder=recorder, settings=settings, directories=directories
    )


def resolve_image_settings() -> ImageGenSettings | None:
    """免费生图（智谱 CogView）要密钥：环境变量优先，否则用配置里智谱模型的密钥。"""
    api_key = os.environ.get("POCKETAGENT_LIVE_IMAGE_API_KEY")
    if not api_key:
        app_config = load_app_config()
        if app_config is not None:
            for profile in app_config.models:
                if "bigmodel.cn" in (profile.base_url or "") and profile.api_key:
                    api_key = profile.api_key
                    break
    if not api_key:
        return None
    return ImageGenSettings(
        provider="zhipu",
        base_url=os.environ.get("POCKETAGENT_LIVE_IMAGE_BASE_URL") or ZHIPU_IMAGE_BASE_URL,
        api_key=api_key,
        model=os.environ.get("POCKETAGENT_LIVE_IMAGE_MODEL") or DEFAULT_FREE_IMAGE_MODEL,
    )


def live_model_profile_dict(model: LiveModel) -> dict:
    """给 CLI 子进程写临时 config.json 用。"""
    return {
        "id": "live",
        "name": "Live",
        "model": model.model,
        "base_url": model.base_url,
        "api_key": model.api_key,
        "protocol": model.protocol,
        "reasoning_effort": model.reasoning_effort,
        "supports_images": model.supports_images,
    }


def skip_without_model(test_class):
    return unittest.skipIf(SKIP_WITHOUT_MODEL, SKIP_WITHOUT_MODEL or "")(test_class)


def skip_when_live_disabled(test_class):
    return unittest.skipUnless(live_enabled(), LIVE_DISABLED_REASON)(test_class)
