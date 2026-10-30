"""任务：列表、新建、快照、改名、删除、附件、文件变更和检查点。"""

from pathlib import Path
from typing import Any

from aiohttp import web

from pocketagent.server import files
from pocketagent.server.common import error_response, find_session, json_response, session_manager
from pocketagent.server.session import SessionBusyError, WebSession


async def list_sessions(request: web.Request) -> web.Response:
    return json_response({"sessions": session_manager(request).list_tasks()})


async def create_session(request: web.Request) -> web.Response:
    payload = await request.json() if request.can_read_body else {}
    manager = session_manager(request)
    # ephemeral：不指定工作区，服务端在 ~/.pocketagent/scratch 下建一个临时目录
    ephemeral = bool(payload.get("ephemeral"))
    workspace = None
    if not ephemeral:
        workspace = Path(payload.get("workspace") or manager.default_workspace()).expanduser()
    web_session = manager.create(
        workspace,
        model_id=payload.get("model_id"),
        mode_name=payload.get("mode"),
        ephemeral=ephemeral,
        agent_mode=payload.get("agent_mode"),
    )
    return json_response({"id": web_session.id, "workspace": web_session.session.workspace})


async def get_overview(request: web.Request) -> web.Response:
    try:
        return json_response(session_manager(request).task_overview(request.match_info["session_id"]))
    except KeyError:
        raise web.HTTPNotFound(text="任务不存在")


async def get_session(request: web.Request) -> web.Response:
    web_session = find_session(request)
    snapshot = web_session.snapshot_message()
    snapshot["usage"] = session_manager(request).usage_log.session_total(web_session.id)
    return json_response(snapshot)


async def update_session(request: web.Request) -> web.Response:
    payload = await request.json()
    web_session = find_session(request)
    if "title" in payload:
        await web_session.set_title(str(payload["title"]))
    return json_response({"title": web_session.session.display_title()})


async def rewind_session(request: web.Request) -> web.Response:
    """删掉这句话和之后的全部对话；返回新快照，并且推给其它标签页 / 桌面窗口。"""
    web_session = find_session(request)
    payload = await request.json()
    message = web_session.rewind_to(str(payload.get("message_id") or ""))
    snapshot = web_session.snapshot_message()
    await web_session.broadcast(snapshot)
    return json_response({"message": message, "snapshot": snapshot})


async def delete_session(request: web.Request) -> web.Response:
    await session_manager(request).delete_task(request.match_info["session_id"])
    return json_response({"ok": True})


async def upload_attachments(request: web.Request) -> web.Response:
    web_session = find_session(request)
    reader = await request.multipart()
    saved = []
    async for part in reader:
        if part.filename is None:
            continue
        data = await part.read(decode=False)
        try:
            saved.append(web_session.attachments.save(part.filename, bytes(data)).to_dict())
        except ValueError as error:
            return error_response(str(error))
    return json_response({"attachments": saved})


async def get_attachment(request: web.Request) -> web.StreamResponse:
    web_session = find_session(request)
    attachment = web_session.attachments.get(request.match_info["attachment_id"])
    return web.FileResponse(attachment.path, headers={"Content-Type": attachment.mime})


async def get_session_file(request: web.Request) -> web.StreamResponse:
    # 按会话的工作区取图：临时任务的临时目录没登记成工作区，也能在聊天里显示
    web_session = find_session(request)
    path = files.session_file_path(web_session.session.workspace, request.query.get("path", ""))
    return web.FileResponse(path)


async def _changes_payload(web_session: WebSession) -> dict[str, Any]:
    checkpoints = web_session.checkpoints
    changes = await checkpoints.changes()
    return {
        "enabled": checkpoints.enabled,
        "disabled_reason": checkpoints.disabled_reason,
        "changes": [change.to_dict() for change in changes],
        "checkpoints": [checkpoint.to_dict() for checkpoint in checkpoints.all_checkpoints()],
    }


async def list_changes(request: web.Request) -> web.Response:
    return json_response(await _changes_payload(find_session(request)))


async def list_turn_changes(request: web.Request) -> web.Response:
    web_session = find_session(request)
    return json_response({"changes": [change.to_dict() for change in await web_session.checkpoints.turn_changes()]})


async def get_change_diff(request: web.Request) -> web.Response:
    return json_response({"diff": await find_session(request).checkpoints.diff(request.query.get("path", ""))})


async def revert_change(request: web.Request) -> web.Response:
    web_session = find_session(request)
    if web_session.running:
        raise SessionBusyError("任务还在运行，等它停下再撤销")
    payload = await request.json()
    await web_session.checkpoints.revert_file(str(payload.get("path", "")))
    web_session.agent.session_store.save(web_session.session)
    return json_response(await _changes_payload(web_session))


async def restore_checkpoint(request: web.Request) -> web.Response:
    web_session = find_session(request)
    if web_session.running:
        raise SessionBusyError("任务还在运行，等它停下再恢复")
    payload = await request.json()
    restored = await web_session.checkpoints.restore(str(payload.get("commit", "")))
    web_session.agent.session_store.save(web_session.session)
    return json_response({"restored": restored, **(await _changes_payload(web_session))})
