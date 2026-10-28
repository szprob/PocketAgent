"""服务信息、模型、设置、用量、技能、记忆。"""

from typing import Any

from aiohttp import web

from pocketagent.account.billing import query_all_balances
from pocketagent.config import FONT_SCALES, ConfigError
from pocketagent.interaction.commands import command_specs_for
from pocketagent.safety.permissions import WEB_APPROVAL_MODES
from pocketagent.server.common import json_response, session_manager
from pocketagent.server.manager import SessionManager


async def get_info(request: web.Request) -> web.Response:
    manager = session_manager(request)
    app_config = manager.app_config
    default_profile = app_config.default_profile()
    models = [
        {
            "id": profile.id,
            "name": profile.name,
            "model": profile.model,
            "provider": profile.provider,
            "supports_images": profile.supports_images,
            "reasoning_effort": profile.reasoning_effort,
            "reasoning_choices": profile.available_reasoning_choices(),
            "context_window_tokens": profile.context_window_tokens,
            "visible": profile.visible,
        }
        for profile in app_config.models
    ]
    return json_response(
        {
            "user_name": app_config.display_user_name(),
            "workspaces": manager.workspace_list(),
            "default_workspace": str(manager.default_workspace()),
            "models": models,
            "default_model": default_profile.id if default_profile else None,
            # 一个模型都没配时，显示从环境变量读到的模型名
            "fallback_model": manager.base_settings.model,
            "default_mode": app_config.approval_mode.value,
            "modes": [mode.value for mode in WEB_APPROVAL_MODES],
            "busy_input_mode": app_config.busy_input_mode,
            # 外观和字号：前端一启动就要套上，所以放在 info 里
            "appearance": app_config.appearance,
            "font_scale": FONT_SCALES[app_config.font_size],
            # 对话里把工具调用收进折叠（默认开）；关掉就平铺
            "fold_tool_calls": app_config.fold_tool_calls,
            "auto_compact": app_config.auto_compact,
            "compact_threshold_percent": app_config.compact_threshold_percent,
            "web_search_enabled": app_config.web_search_settings() is not None,
            "commands": [
                {"name": spec.name, "summary": spec.summary, "usage": spec.usage, "aliases": list(spec.aliases)}
                for spec in command_specs_for("web")
            ],
        }
    )


async def update_model(request: web.Request) -> web.Response:
    profile = await session_manager(request).update_model_options(request.match_info["model_id"], await request.json())
    return json_response(profile.public_dict())


async def get_usage(request: web.Request) -> web.Response:
    return json_response(session_manager(request).usage_log.summarize())


async def get_balances(request: web.Request) -> web.Response:
    return json_response({"balances": await query_all_balances(session_manager(request).app_config.models)})


def _config_payload(manager: SessionManager) -> dict[str, Any]:
    return {**manager.app_config.public_dict(), "config_path": str(manager.base_settings.config_path)}


async def list_skills(request: web.Request) -> web.Response:
    return json_response(session_manager(request).skills_overview(request.query.get("workspace") or None))


async def get_skill_content(request: web.Request) -> web.Response:
    return json_response(session_manager(request).skill_file_content(request.query.get("path", "")))


async def get_memory(request: web.Request) -> web.Response:
    return json_response(session_manager(request).memory_overview(request.query.get("workspace", "")))


async def get_memory_file(request: web.Request) -> web.Response:
    manager = session_manager(request)
    return json_response(manager.read_memory(request.query.get("workspace", ""), request.query.get("file", "")))


async def delete_memory_file(request: web.Request) -> web.Response:
    manager = session_manager(request)
    return json_response(manager.delete_memory(request.query.get("workspace", ""), request.query.get("file", "")))


async def get_config(request: web.Request) -> web.Response:
    return json_response(_config_payload(session_manager(request)))


async def put_config(request: web.Request) -> web.Response:
    manager = session_manager(request)
    new_config = manager.config_from_form(await request.json())
    await manager.apply_config(new_config)
    return json_response(_config_payload(manager))


async def test_config(request: web.Request) -> web.Response:
    try:
        result = await session_manager(request).test_model(await request.json())
    except ConfigError as error:
        return json_response({"ok": False, "error": str(error)})
    return json_response(result)
