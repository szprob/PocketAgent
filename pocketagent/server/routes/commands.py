"""斜杠命令（和命令行共用 interaction/commands.py 那一套）。"""

import copy
from pathlib import Path

from aiohttp import web

from pocketagent.interaction.commands import CommandContext, CommandResult, handle_command_line
from pocketagent.server.common import json_response, session_manager
from pocketagent.server.manager import SessionManager
from pocketagent.server.session import SessionBusyError, WebSession


async def run_command(request: web.Request) -> web.Response:
    """斜杠命令。带 session_id 就在那个任务里执行（能用 /compact、/undo 这些）；不带就在新任务页执行。

    换模式 / 换模型 / 压缩这些动作在这里落地到任务上；没有任务时把动作原样回给前端
    （新任务页改的是输入框上那两个选择器）。
    """
    payload = await request.json() if request.can_read_body else {}
    text = str(payload.get("text", ""))
    manager = session_manager(request)
    session_id = str(payload.get("session_id") or "")
    web_session = manager.get(session_id) if session_id else None
    settings = manager.base_settings
    if web_session is not None:
        settings = web_session.settings
    elif payload.get("workspace"):
        # 新任务页：按用户选的工作区找技能、看记忆
        settings = copy.deepcopy(manager.base_settings)
        settings.workspace = Path(str(payload["workspace"])).expanduser()
        settings.apply_app_config(manager.app_config)
    context = CommandContext(
        settings=settings,
        agent=web_session.agent if web_session is not None else None,
        models=manager.app_config.models,
        surface="web",
    )
    result = await handle_command_line(text, context)
    if result is None:
        # 技能命令（/技能名）或普通消息：不是这里的事
        return json_response({"handled": False})
    await _apply_command_action(manager, web_session, result)
    return json_response(
        {
            "handled": True,
            "text": result.text,
            "kind": result.kind,
            "action": result.action,
            "argument": result.argument,
        }
    )


async def _apply_command_action(
    manager: SessionManager, web_session: WebSession | None, result: CommandResult
) -> None:
    """把命令里的界面动作落到这个任务上；没有任务时（新任务页）动作留给前端自己做。"""
    if web_session is None:
        return
    if result.action == "switch_mode":
        await web_session.set_mode(result.argument)
    elif result.action == "switch_model":
        await manager.switch_model(web_session, result.argument)
    elif result.action == "compact":
        try:
            await web_session.start_compaction()
        except SessionBusyError as error:
            result.text = str(error)
            result.kind = "warning"
