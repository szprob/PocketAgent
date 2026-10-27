"""各个路由共用的小工具：取 SessionManager、找任务、回 JSON。"""

import json
from typing import Any

from aiohttp import web

from pocketagent.server.manager import SessionManager
from pocketagent.server.session import WebSession

MANAGER_KEY = web.AppKey("manager", SessionManager)


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False), content_type="application/json", status=status)


def error_response(message: str, status: int = 400) -> web.Response:
    return json_response({"error": message}, status=status)


def session_manager(request: web.Request) -> SessionManager:
    return request.app[MANAGER_KEY]


def find_session(request: web.Request) -> WebSession:
    try:
        return session_manager(request).get(request.match_info["session_id"])
    except KeyError:
        raise web.HTTPNotFound(text="任务不存在")
