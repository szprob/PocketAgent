"""工作区：文件树、文件预览、选目录、增删工作区。"""


from aiohttp import web

from pocketagent.server import files
from pocketagent.server.common import json_response, session_manager


async def list_files(request: web.Request) -> web.Response:
    manager = session_manager(request)
    entries = files.list_workspace_dir(
        manager.file_workspaces(), request.query.get("workspace", ""), request.query.get("path", "")
    )
    return json_response({"entries": entries})


async def read_file(request: web.Request) -> web.Response:
    manager = session_manager(request)
    preview = files.read_workspace_file(
        manager.file_workspaces(), request.query.get("workspace", ""), request.query.get("path", "")
    )
    return json_response(preview)


async def raw_file(request: web.Request) -> web.StreamResponse:
    manager = session_manager(request)
    path = files.workspace_file_path(
        manager.file_workspaces(), request.query.get("workspace", ""), request.query.get("path", "")
    )
    return web.FileResponse(path)


async def browse_directories(request: web.Request) -> web.Response:
    return json_response(files.browse_directories(request.query.get("path")))


async def add_workspace(request: web.Request) -> web.Response:
    payload = await request.json()
    session_manager(request).add_workspace(str(payload.get("path", "")))
    return json_response({"workspaces": session_manager(request).workspace_list()})


async def remove_workspace(request: web.Request) -> web.Response:
    payload = await request.json()
    session_manager(request).remove_workspace(str(payload.get("path", "")))
    return json_response({"workspaces": session_manager(request).workspace_list()})


async def set_default_workspace(request: web.Request) -> web.Response:
    payload = await request.json()
    manager = session_manager(request)
    manager.set_default_workspace(str(payload.get("path", "")))
    return json_response(
        {
            "workspaces": manager.workspace_list(),
            "default_workspace": str(manager.default_workspace()),
        }
    )
