"""自动化的增删改、立即运行，以及外部 POST 进来的那个入口。"""

import json

from aiohttp import web

from pocketagent.server.automations import Automation
from pocketagent.server.common import error_response, json_response, session_manager
from pocketagent.server.triggers.engine import verify_webhook
from pocketagent.server.triggers.model import WebhookSource

SIGNATURE_HEADER = "X-PocketAgent-Signature"
DELIVERY_HEADER = "X-PocketAgent-Delivery"


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


async def automation_hook(request: web.Request) -> web.Response:
    """外部系统的入口：带 HMAC 签名 POST 过来，验过了才开任务。

    这条路由不走 token 中间件（签名的密钥是每条自动化自己的，外面的人不该拿到主 token），
    所以这里的验签是唯一的门，不能省。
    """
    manager = session_manager(request)
    automation_id = request.match_info["automation_id"]
    automation = manager.automations.get(automation_id)
    source = automation.trigger.source
    if not isinstance(source, WebhookSource):
        return error_response("这条自动化不是外部触发的")
    body = await request.read()
    if not verify_webhook(source.secret, body, request.headers.get(SIGNATURE_HEADER, "")):
        return error_response("签名不对", status=401)
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return error_response("正文不是合法的 JSON")
    if not isinstance(payload, dict):
        return error_response("正文要是一个 JSON 对象")
    outcome = await manager.triggers.fire_webhook(
        automation_id,
        payload,
        request.headers.get(DELIVERY_HEADER, ""),
    )
    return json_response(outcome.to_dict())
