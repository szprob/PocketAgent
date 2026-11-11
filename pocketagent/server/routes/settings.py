"""服务信息、模型、设置、用量、技能、记忆、云同步。"""

import asyncio
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from pocketagent.account.billing import query_all_balances
from pocketagent.config import FONT_SCALES, ConfigError
from pocketagent.interaction.commands import command_specs_for
from pocketagent.safety.permissions import WEB_APPROVAL_MODES
from pocketagent.server.common import json_response, session_manager
from pocketagent.server.manager import SessionManager
from pocketagent.sync.backend import SyncError
from pocketagent.sync.config import (
    BackendSettings,
    SyncConfig,
    SyncConfigError,
    default_local_directory,
    make_device_id,
    sync_state_dir,
)
from pocketagent.sync.engine import SyncEngine, SyncState, create_backend
from pocketagent.tools.mcp import McpError


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
            "protocol": profile.protocol,
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


async def get_mcp(request: web.Request) -> web.Response:
    return json_response(session_manager(request).mcp_overview())


async def put_mcp(request: web.Request) -> web.Response:
    try:
        overview = await session_manager(request).save_mcp_config(await request.json())
    except McpError as error:
        return json_response({"ok": False, "error": str(error)}, status=400)
    return json_response({"ok": True, "overview": overview})


async def reload_mcp(request: web.Request) -> web.Response:
    overview = await session_manager(request).reload_mcp()
    return json_response({"ok": True, "overview": overview})


# ---- 云同步 ----


def _sync_home(request: web.Request) -> Path:
    return session_manager(request).base_settings.home_dir


def _password_source(backend: BackendSettings) -> str:
    if os.environ.get(backend.password_env):
        return "env"
    return "file" if backend.password else "none"


def _sync_payload(home_dir: Path) -> dict[str, Any]:
    """给设置页看的同步配置：密码只回「有没有」，不回内容。"""
    payload: dict[str, Any] = {
        "configured": False,
        "device_id": None,
        "backend": {"kind": "webdav", "url": "", "username": ""},
        "has_password": False,
        "password_source": "none",
        "includes": ["sessions", "projects"],
        "local_files": 0,
        "default_local_directory": str(default_local_directory()),
        "last_synced_at": None,
    }
    try:
        config = SyncConfig.load(home_dir)
    except SyncConfigError as error:
        payload["error"] = str(error)
        return payload
    state = SyncState.load(sync_state_dir(home_dir) / "state.json")
    payload.update(
        {
            "configured": True,
            "device_id": config.device_id,
            "backend": {
                "kind": config.backend.kind,
                "url": config.backend.url,
                "username": config.backend.username,
                "branch": config.backend.branch,
            },
            "has_password": bool(config.backend.resolved_password()),
            "password_source": _password_source(config.backend),
            "includes": config.includes,
            "local_files": len(state.files),
            "last_synced_at": max((item.synced_at for item in state.files.values()), default=None),
        }
    )
    return payload


def _sync_config_from_form(home_dir: Path, data: dict[str, Any]) -> SyncConfig:
    """表单 → SyncConfig。密码留空表示沿用已经存下的那个。"""
    existing: SyncConfig | None = None
    try:
        existing = SyncConfig.load(home_dir)
    except SyncConfigError:
        existing = None
    kind = str(data.get("kind") or (existing.backend.kind if existing is not None else "webdav"))
    password = str(data.get("password") or "")
    if not password and existing is not None and existing.backend.kind == kind:
        # 换后端（WebDAV ↔ GitHub）时不能沿用上一个后端的密码
        password = existing.backend.password
    includes = data.get("includes")
    if includes is None:
        includes = existing.includes if existing is not None else ["sessions", "projects"]
    if not includes:
        raise SyncConfigError("同步范围至少要选一项")
    url = str(data.get("url") or "").strip()
    if kind == "local" and not url:
        # 本地目录有默认值：装了坚果云就用它的同步文件夹，其次 iCloud
        url = str(default_local_directory())
    return SyncConfig.from_dict(
        {
            "device_id": existing.device_id if existing is not None else make_device_id(),
            "backend": {
                "kind": kind,
                "url": url,
                "username": str(data.get("username") or ""),
                "password": password,
                "password_env": (
                    existing.backend.password_env if existing is not None else "POCKETAGENT_SYNC_PASSWORD"
                ),
                "verify_tls": existing.backend.verify_tls if existing is not None else True,
                "branch": str(data.get("branch") or (existing.backend.branch if existing is not None else "main")),
            },
            "includes": includes,
            "max_file_bytes": existing.max_file_bytes if existing is not None else None,
            "remote_prefix": existing.remote_prefix if existing is not None else "data",
        }
    )


async def get_sync(request: web.Request) -> web.Response:
    return json_response(_sync_payload(_sync_home(request)))


async def put_sync(request: web.Request) -> web.Response:
    home_dir = _sync_home(request)
    try:
        config = _sync_config_from_form(home_dir, await request.json())
    except SyncConfigError as error:
        return json_response({"ok": False, "error": str(error)}, status=400)
    if config.backend.kind == "local":
        # 指向 iCloud 这类目录时目录可能还不存在，和命令行 init 一样先建出来
        Path(config.backend.url).expanduser().mkdir(parents=True, exist_ok=True)
    session_manager(request).save_sync_config(config)
    return json_response({"ok": True, "sync": _sync_payload(home_dir)})


async def test_sync(request: web.Request) -> web.Response:
    home_dir = _sync_home(request)
    try:
        data = await request.json()
    except (ValueError, TypeError):
        data = {}
    try:
        config = _sync_config_from_form(home_dir, data)
        backend = create_backend(config.backend)
        await asyncio.to_thread(backend.ping)
    except (SyncConfigError, SyncError) as error:
        return json_response({"ok": False, "error": str(error)})
    return json_response({"ok": True})


async def run_sync(request: web.Request) -> web.Response:
    home_dir = _sync_home(request)
    try:
        config = SyncConfig.load(home_dir)
    except SyncConfigError as error:
        return json_response({"ok": False, "error": str(error)}, status=400)
    try:
        report = await asyncio.to_thread(lambda: SyncEngine(home_dir, config).run())
    except SyncError as error:
        return json_response({"ok": False, "error": str(error)}, status=400)
    payload = report.to_dict()
    payload["ok"] = not report.errors
    if report.errors:
        payload["error"] = "；".join(report.errors)
    return json_response(payload)
