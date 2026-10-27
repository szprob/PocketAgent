"""每个任务一条 WebSocket：连上先发快照，之后推事件；收用户消息、审批、回答、中断等。"""

import json
from typing import Any

from aiohttp import WSMsgType, web

from pocketagent.server.common import find_session, session_manager
from pocketagent.server.manager import SessionManager
from pocketagent.server.session import SessionBusyError, WebSession


async def session_socket(request: web.Request) -> web.StreamResponse:
    web_session = find_session(request)
    client = web.WebSocketResponse(heartbeat=30)
    await client.prepare(request)
    web_session.clients.add(client)
    await web_session.send(client, web_session.snapshot_message())
    manager = session_manager(request)
    try:
        async for message in client:
            if message.type != WSMsgType.TEXT:
                continue
            await _handle_client_message(manager, web_session, client, json.loads(message.data))
    finally:
        web_session.clients.discard(client)
    return client


async def _handle_client_message(
    manager: SessionManager, web_session: WebSession, client: web.WebSocketResponse, message: dict[str, Any]
) -> None:
    message_type = message.get("type")
    try:
        if message_type == "user_message":
            text = str(message.get("text", "")).strip()
            attachment_ids = [str(item) for item in message.get("attachment_ids") or []]
            if text or attachment_ids:
                user_message = web_session.build_message(text, attachment_ids)
                await web_session.submit(user_message, manager.app_config.busy_input_mode)
        elif message_type == "approval":
            web_session.answer_approval(message["request_id"], message["answer"])
        elif message_type == "question_answer":
            web_session.answer_questions(message["request_id"], message.get("answers"))
        elif message_type == "interrupt":
            await web_session.interrupt()
        elif message_type == "compact":
            await web_session.start_compaction()
        elif message_type == "remove_queued":
            web_session.remove_queued(int(message["index"]))
            await web_session.broadcast_state()
        elif message_type == "set_mode":
            await web_session.set_mode(message["mode"])
        elif message_type == "set_model":
            await manager.switch_model(web_session, message["model_id"])
        else:
            await web_session.send(client, {"type": "error", "message": f"不认识的消息类型：{message_type}"})
    except (SessionBusyError, ValueError, KeyError) as error:
        await web_session.send(client, {"type": "error", "message": str(error)})
