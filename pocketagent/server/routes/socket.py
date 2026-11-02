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
            payload = _parse_client_message(message.data)
            if payload is None:
                await web_session.send(client, {"type": "error", "message": "消息不是合法的 JSON 对象"})
                continue
            if web_session.deleted:
                # 任务可能已被另一个标签页删掉，这条连接不能再发一条话把它写回磁盘
                await web_session.send(client, {"type": "error", "message": "任务已经删除"})
                break
            await _handle_client_message(manager, web_session, client, payload)
    finally:
        web_session.clients.discard(client)
    return client


def _parse_client_message(raw: str) -> dict[str, Any] | None:
    """一条客户端消息：不是 JSON、或者不是对象，就返回 None（调用方回一条 error，连接留着）。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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
            # 一项的卡片回 answer，合并成一张的卡片回 answers（每项一个）
            web_session.answer_approval(message["request_id"], message.get("answer"), message.get("answers"))
        elif message_type == "question_answer":
            web_session.answer_questions(message["request_id"], message.get("answers"))
        elif message_type == "plan_answer":
            web_session.answer_plan(
                message["request_id"], bool(message.get("approve")), str(message.get("feedback") or "")
            )
        elif message_type == "interrupt":
            await web_session.interrupt()
        elif message_type == "compact":
            await web_session.start_compaction()
        elif message_type == "remove_queued":
            web_session.remove_queued(int(message["index"]))
            await web_session.broadcast_state()
        elif message_type == "set_mode":
            await web_session.set_mode(message["mode"])
        elif message_type == "set_agent_mode":
            await web_session.set_agent_mode(str(message.get("mode") or "agent"))
        elif message_type == "set_model":
            await manager.switch_model(web_session, message["model_id"])
        else:
            await web_session.send(client, {"type": "error", "message": f"不认识的消息类型：{message_type}"})
    except (SessionBusyError, ValueError, KeyError) as error:
        await web_session.send(client, {"type": "error", "message": str(error)})
