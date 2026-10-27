"""自动化（定时任务）的增删改和立即运行。"""


from aiohttp import web

from pocketagent.server.automations import Automation
from pocketagent.server.common import json_response, session_manager


async def list_automations(request: web.Request) -> web.Response:
    return json_response({"automations": session_manager(request).automation_summaries()})


async def create_automation(request: web.Request) -> web.Response:
    manager = session_manager(request)
    automation = Automation.from_form(await request.json())
    manager.automations.save(automation)
    return json_response({"automations": manager.automation_summaries()})


async def update_automation(request: web.Request) -> web.Response:
    manager = session_manager(request)
    existing = manager.automations.get(request.match_info["automation_id"])
    manager.automations.save(Automation.from_form(await request.json(), existing=existing))
    return json_response({"automations": manager.automation_summaries()})


async def delete_automation(request: web.Request) -> web.Response:
    manager = session_manager(request)
    manager.automations.delete(request.match_info["automation_id"])
    return json_response({"automations": manager.automation_summaries()})


async def run_automation_now(request: web.Request) -> web.Response:
    web_session = session_manager(request).run_automation(request.match_info["automation_id"])
    return json_response({"session_id": web_session.id})
