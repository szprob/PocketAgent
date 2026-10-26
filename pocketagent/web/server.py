"""web 服务（aiohttp）：静态页面 + REST + 每个任务一条 WebSocket。

浏览器 → 服务（WebSocket，JSON）：
  {"type": "user_message", "text": ..., "attachment_ids": [...]}   空闲就开一轮；忙着就按设置插话或排队
  {"type": "approval", "request_id": ..., "answer": "allow_once" | "allow_always" | "deny"}
  {"type": "question_answer", "request_id": ..., "answers": [{"selected": [...], "other": "..."}, ...] | null（跳过）}
  {"type": "interrupt"} / {"type": "compact"} / {"type": "remove_queued", "index": n}
  {"type": "set_mode", "mode": ...} / {"type": "set_model", "model_id": ...}
服务 → 浏览器：
  snapshot（连上时的完整历史）、state（是否在跑、模型、审批模式、上下文占用、排队和插话）、
  event（agent 事件，见 events.py）、approval_request / approval_resolved、question_request / question_resolved、
  returned_inputs（中断时退回的输入）、error

REST（都在 /api 下）：
  info                                   工作区、模型（含可选思考档位、上下文长度）、审批模式、用户名
  models/{id}         PATCH              选模型面板里改思考档位、上下文长度（存进配置，马上生效）
  workspaces          POST / DELETE      加 / 移除工作区
  files / file / file/raw                工作区文件树 / 文件预览 / 原始文件（图片预览）
  browse                                 选工作区时浏览本机目录（只列目录）
  sessions            GET / POST         任务列表 / 新建任务（指定工作区、模型、审批模式）
  sessions/{id}       GET / PATCH / DELETE   快照 / 改标题 / 删除
  sessions/{id}/overview                 右侧栏概要（用量、缓存命中、最新回复），比快照轻
  sessions/{id}/attachments              POST 上传（multipart），GET .../{附件 id} 取文件
  sessions/{id}/changes                  GET 文件变更 + 检查点（影子 git），GET .../diff?path=，POST .../revert
  sessions/{id}/checkpoints/restore      POST 把整个工作区恢复到某个检查点
  usage / usage/balance                  本机 token 用量 / 各家账户余额
  config              GET / PUT          设置；config/test 测试模型连接
  automations         GET / POST         自动化列表 / 新建；automations/{id} PUT / DELETE；.../run 立即运行

页面里所有地址都是相对路径，挂在网关子路径下（如 /web-xxx/pocketagent/）也能用。
"""

import asyncio
import json
import secrets
import webbrowser
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

from pocketagent.automations import Automation
from pocketagent.billing import query_all_balances
from pocketagent.checkpoints import CheckpointError
from pocketagent.config import AppConfig, ConfigError, Settings
from pocketagent.permissions import WEB_APPROVAL_MODES
from pocketagent.web import files
from pocketagent.web.runtime import ProviderFactory, SessionBusyError, SessionManager, WebSession

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_COOKIE_NAME = "pocketagent_token"
DEFAULT_PORT = 8765
PORT_SEARCH_LIMIT = 20

MANAGER_KEY = web.AppKey("manager", SessionManager)
TOKEN_KEY = web.AppKey("token", str)
SCHEDULER_KEY = web.AppKey("scheduler", asyncio.Task)
WARM_UP_KEY = web.AppKey("warm_up", asyncio.Future)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False), content_type="application/json", status=status)


def _error_response(message: str, status: int = 400) -> web.Response:
    return _json_response({"error": message}, status=status)


def _manager(request: web.Request) -> SessionManager:
    return request.app[MANAGER_KEY]


# ---- 鉴权：和 Jupyter 一样，第一次用 ?token= 打开，之后靠 cookie ----


@web.middleware
async def token_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    expected_token = request.app[TOKEN_KEY]
    if not expected_token:
        return await handler(request)
    query_token = request.query.get("token", "")
    supplied_token = query_token or request.cookies.get(TOKEN_COOKIE_NAME, "")
    if not secrets.compare_digest(supplied_token, expected_token):
        return web.Response(status=401, text="需要 token：用启动时打印的带 ?token= 的地址打开")
    response = await handler(request)
    if query_token and not isinstance(response, web.WebSocketResponse):
        response.set_cookie(TOKEN_COOKIE_NAME, expected_token, httponly=True, samesite="Strict", path="/")
    return response


@web.middleware
async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """校验类错误统一转成 400 / 404 / 409 的 JSON。"""
    try:
        return await handler(request)
    except ConfigError as error:
        return _error_response(str(error))
    except SessionBusyError as error:
        return _error_response(str(error), status=409)
    except KeyError as error:
        return _error_response(f"找不到：{error}", status=404)
    except CheckpointError as error:
        return _error_response(str(error))


# ---- 页面和基本信息 ----


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def get_info(request: web.Request) -> web.Response:
    manager = _manager(request)
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
    return _json_response(
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
            "auto_compact": app_config.auto_compact,
            "compact_threshold_percent": app_config.compact_threshold_percent,
            "web_search_enabled": app_config.web_search_settings() is not None,
        }
    )


async def update_model(request: web.Request) -> web.Response:
    profile = await _manager(request).update_model_options(request.match_info["model_id"], await request.json())
    return _json_response(profile.public_dict())


# ---- 文件浏览 ----


async def list_files(request: web.Request) -> web.Response:
    manager = _manager(request)
    entries = files.list_workspace_dir(
        manager.app_config.workspaces, request.query.get("workspace", ""), request.query.get("path", "")
    )
    return _json_response({"entries": entries})


async def read_file(request: web.Request) -> web.Response:
    manager = _manager(request)
    preview = files.read_workspace_file(
        manager.app_config.workspaces, request.query.get("workspace", ""), request.query.get("path", "")
    )
    return _json_response(preview)


async def raw_file(request: web.Request) -> web.StreamResponse:
    manager = _manager(request)
    path = files.workspace_file_path(
        manager.app_config.workspaces, request.query.get("workspace", ""), request.query.get("path", "")
    )
    return web.FileResponse(path)


async def browse_directories(request: web.Request) -> web.Response:
    return _json_response(files.browse_directories(request.query.get("path")))


# ---- 工作区 ----


async def add_workspace(request: web.Request) -> web.Response:
    payload = await request.json()
    _manager(request).add_workspace(str(payload.get("path", "")))
    return _json_response({"workspaces": _manager(request).workspace_list()})


async def remove_workspace(request: web.Request) -> web.Response:
    payload = await request.json()
    _manager(request).remove_workspace(str(payload.get("path", "")))
    return _json_response({"workspaces": _manager(request).workspace_list()})


# ---- 任务 ----


async def list_sessions(request: web.Request) -> web.Response:
    return _json_response({"sessions": _manager(request).list_tasks()})


async def create_session(request: web.Request) -> web.Response:
    payload = await request.json() if request.can_read_body else {}
    manager = _manager(request)
    workspace = Path(payload.get("workspace") or manager.default_workspace()).expanduser()
    web_session = manager.create(workspace, model_id=payload.get("model_id"), mode_name=payload.get("mode"))
    return _json_response({"id": web_session.id})


def _find_session(request: web.Request) -> WebSession:
    try:
        return _manager(request).get(request.match_info["session_id"])
    except KeyError:
        raise web.HTTPNotFound(text="任务不存在")


async def get_overview(request: web.Request) -> web.Response:
    try:
        return _json_response(_manager(request).task_overview(request.match_info["session_id"]))
    except KeyError:
        raise web.HTTPNotFound(text="任务不存在")


async def get_session(request: web.Request) -> web.Response:
    web_session = _find_session(request)
    snapshot = web_session.snapshot_message()
    snapshot["usage"] = _manager(request).usage_log.session_total(web_session.id)
    return _json_response(snapshot)


async def update_session(request: web.Request) -> web.Response:
    payload = await request.json()
    web_session = _find_session(request)
    if "title" in payload:
        await web_session.set_title(str(payload["title"]))
    return _json_response({"title": web_session.session.display_title()})


async def delete_session(request: web.Request) -> web.Response:
    await _manager(request).delete_task(request.match_info["session_id"])
    return _json_response({"ok": True})


# ---- 附件 ----


async def upload_attachments(request: web.Request) -> web.Response:
    web_session = _find_session(request)
    reader = await request.multipart()
    saved = []
    async for part in reader:
        if part.filename is None:
            continue
        data = await part.read(decode=False)
        try:
            saved.append(web_session.attachments.save(part.filename, bytes(data)).to_dict())
        except ValueError as error:
            return _error_response(str(error))
    return _json_response({"attachments": saved})


async def get_attachment(request: web.Request) -> web.StreamResponse:
    web_session = _find_session(request)
    attachment = web_session.attachments.get(request.match_info["attachment_id"])
    return web.FileResponse(attachment.path, headers={"Content-Type": attachment.mime})


# ---- 文件变更 ----


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
    return _json_response(await _changes_payload(_find_session(request)))


async def get_change_diff(request: web.Request) -> web.Response:
    return _json_response({"diff": await _find_session(request).checkpoints.diff(request.query.get("path", ""))})


async def revert_change(request: web.Request) -> web.Response:
    web_session = _find_session(request)
    if web_session.running:
        raise SessionBusyError("任务还在运行，等它停下再撤销")
    payload = await request.json()
    await web_session.checkpoints.revert_file(str(payload.get("path", "")))
    web_session.agent.session_store.save(web_session.session)
    return _json_response(await _changes_payload(web_session))


async def restore_checkpoint(request: web.Request) -> web.Response:
    web_session = _find_session(request)
    if web_session.running:
        raise SessionBusyError("任务还在运行，等它停下再恢复")
    payload = await request.json()
    restored = await web_session.checkpoints.restore(str(payload.get("commit", "")))
    web_session.agent.session_store.save(web_session.session)
    return _json_response({"restored": restored, **(await _changes_payload(web_session))})


# ---- 用量和余额 ----


async def get_usage(request: web.Request) -> web.Response:
    return _json_response(_manager(request).usage_log.summarize())


async def get_balances(request: web.Request) -> web.Response:
    return _json_response({"balances": await query_all_balances(_manager(request).app_config.models)})


# ---- 设置 ----


def _config_payload(manager: SessionManager) -> dict[str, Any]:
    return {**manager.app_config.public_dict(), "config_path": str(manager.base_settings.config_path)}


async def list_skills(request: web.Request) -> web.Response:
    return _json_response(_manager(request).skills_overview(request.query.get("workspace") or None))


async def get_skill_content(request: web.Request) -> web.Response:
    return _json_response(_manager(request).skill_file_content(request.query.get("path", "")))


async def get_memory(request: web.Request) -> web.Response:
    return _json_response(_manager(request).memory_overview(request.query.get("workspace", "")))


async def get_memory_file(request: web.Request) -> web.Response:
    manager = _manager(request)
    return _json_response(manager.read_memory(request.query.get("workspace", ""), request.query.get("file", "")))


async def delete_memory_file(request: web.Request) -> web.Response:
    manager = _manager(request)
    return _json_response(manager.delete_memory(request.query.get("workspace", ""), request.query.get("file", "")))


async def get_config(request: web.Request) -> web.Response:
    return _json_response(_config_payload(_manager(request)))


async def put_config(request: web.Request) -> web.Response:
    manager = _manager(request)
    new_config = manager.config_from_form(await request.json())
    await manager.apply_config(new_config)
    return _json_response(_config_payload(manager))


async def test_config(request: web.Request) -> web.Response:
    try:
        result = await _manager(request).test_model(await request.json())
    except ConfigError as error:
        return _json_response({"ok": False, "error": str(error)})
    return _json_response(result)


# ---- 自动化 ----


async def list_automations(request: web.Request) -> web.Response:
    return _json_response({"automations": _manager(request).automation_summaries()})


async def create_automation(request: web.Request) -> web.Response:
    manager = _manager(request)
    automation = Automation.from_form(await request.json())
    manager.automations.save(automation)
    return _json_response({"automations": manager.automation_summaries()})


async def update_automation(request: web.Request) -> web.Response:
    manager = _manager(request)
    existing = manager.automations.get(request.match_info["automation_id"])
    manager.automations.save(Automation.from_form(await request.json(), existing=existing))
    return _json_response({"automations": manager.automation_summaries()})


async def delete_automation(request: web.Request) -> web.Response:
    manager = _manager(request)
    manager.automations.delete(request.match_info["automation_id"])
    return _json_response({"automations": manager.automation_summaries()})


async def run_automation_now(request: web.Request) -> web.Response:
    web_session = _manager(request).run_automation(request.match_info["automation_id"])
    return _json_response({"session_id": web_session.id})


# ---- WebSocket ----


async def session_socket(request: web.Request) -> web.StreamResponse:
    web_session = _find_session(request)
    client = web.WebSocketResponse(heartbeat=30)
    await client.prepare(request)
    web_session.clients.add(client)
    await web_session.send(client, web_session.snapshot_message())
    manager = _manager(request)
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


# ---- 组装和启动 ----


def create_app(
    settings: Settings,
    token: str,
    provider_factory: ProviderFactory | None = None,
    app_config: AppConfig | None = None,
    run_scheduler: bool = False,
) -> web.Application:
    """token 传空字符串表示不鉴权。provider_factory 留空就用默认的 OpenAI 兼容接口，测试里传假模型。
    app_config 留空就读 settings.home_dir 下的配置文件。run_scheduler 打开定时任务调度（测试里关着）。"""
    if app_config is None:
        app_config = AppConfig.load(settings.config_path)
    app = web.Application(middlewares=[token_middleware, error_middleware], client_max_size=25 * 1024 * 1024)
    app[TOKEN_KEY] = token
    app[MANAGER_KEY] = SessionManager(settings, app_config, provider_factory)
    _add_routes(app.router)
    if run_scheduler:
        app.on_startup.append(_start_scheduler)
        app.on_startup.append(_warm_up)
        app.on_cleanup.append(_stop_scheduler)
    app.on_shutdown.append(_shutdown_sessions)
    return app


def _add_routes(router: web.UrlDispatcher) -> None:
    router.add_get("/", index)
    router.add_get("/api/info", get_info)
    router.add_patch("/api/models/{model_id}", update_model)
    router.add_get("/api/files", list_files)
    router.add_get("/api/file", read_file)
    router.add_get("/api/file/raw", raw_file)
    router.add_get("/api/browse", browse_directories)
    router.add_post("/api/workspaces", add_workspace)
    router.add_delete("/api/workspaces", remove_workspace)
    router.add_get("/api/sessions", list_sessions)
    router.add_post("/api/sessions", create_session)
    router.add_get("/api/sessions/{session_id}", get_session)
    router.add_get("/api/sessions/{session_id}/overview", get_overview)
    router.add_patch("/api/sessions/{session_id}", update_session)
    router.add_delete("/api/sessions/{session_id}", delete_session)
    router.add_post("/api/sessions/{session_id}/attachments", upload_attachments)
    router.add_get("/api/sessions/{session_id}/attachments/{attachment_id}", get_attachment)
    router.add_get("/api/sessions/{session_id}/changes", list_changes)
    router.add_get("/api/sessions/{session_id}/changes/diff", get_change_diff)
    router.add_post("/api/sessions/{session_id}/changes/revert", revert_change)
    router.add_post("/api/sessions/{session_id}/checkpoints/restore", restore_checkpoint)
    router.add_get("/api/usage", get_usage)
    router.add_get("/api/usage/balance", get_balances)
    router.add_get("/api/skills", list_skills)
    router.add_get("/api/skills/content", get_skill_content)
    router.add_get("/api/memory", get_memory)
    router.add_get("/api/memory/file", get_memory_file)
    router.add_delete("/api/memory/file", delete_memory_file)
    router.add_get("/api/config", get_config)
    router.add_put("/api/config", put_config)
    router.add_post("/api/config/test", test_config)
    router.add_get("/api/automations", list_automations)
    router.add_post("/api/automations", create_automation)
    router.add_put("/api/automations/{automation_id}", update_automation)
    router.add_delete("/api/automations/{automation_id}", delete_automation)
    router.add_post("/api/automations/{automation_id}/run", run_automation_now)
    router.add_get("/ws/{session_id}", session_socket)
    router.add_static("/static/", STATIC_DIR)


def _import_model_sdk() -> None:
    import openai  # noqa: F401  （只为提前加载）


async def _warm_up(app: web.Application) -> None:
    """openai SDK 第一次 import 要 0.5 秒以上；放在第一个任务里做，用户敲第一个回车就会卡一下。
    服务一起来就在线程里先 import 好，不挡启动。"""
    app[WARM_UP_KEY] = asyncio.get_running_loop().run_in_executor(None, _import_model_sdk)


async def _start_scheduler(app: web.Application) -> None:
    app[SCHEDULER_KEY] = asyncio.create_task(app[MANAGER_KEY].run_scheduler())


async def _stop_scheduler(app: web.Application) -> None:
    app[SCHEDULER_KEY].cancel()


async def _shutdown_sessions(app: web.Application) -> None:
    await app[MANAGER_KEY].shutdown()


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _resolve_token(settings: Settings, app_config: AppConfig, token: str | None, require_token: bool) -> str:
    """优先用传进来的；没有就用配置文件里存的；还没有就生成一个存进去，重启后浏览器 cookie 还能用。"""
    if not require_token:
        return ""
    if token:
        return token
    if not app_config.web_token:
        app_config.web_token = secrets.token_urlsafe(16)
        app_config.save(settings.config_path)
    return app_config.web_token


async def _start_site(runner: web.AppRunner, host: str, requested_port: int | None) -> int:
    """指定了端口就只试这一个；没指定就从 8765 往后找空闲的，方便同时开几个。"""
    if requested_port:
        candidate_ports = [requested_port]
    else:
        candidate_ports = list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_SEARCH_LIMIT))
    for port in candidate_ports:
        try:
            await web.TCPSite(runner, host, port).start()
            return port
        except OSError:
            continue
    raise SystemExit(f"端口被占用（试了 {candidate_ports[0]}–{candidate_ports[-1]}），用 --port 换一个")


async def _serve(app: web.Application, settings: Settings, host: str, port: int | None, open_browser: bool) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        bound_port = await _start_site(runner, host, port)
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{display_host}:{bound_port}/"
        if app[TOKEN_KEY]:
            url += f"?token={app[TOKEN_KEY]}"
        # flush：后台运行时 stdout 是块缓冲，不 flush 日志里看不到地址
        print(f"PocketAgent web 界面：{url}", flush=True)
        print(f"默认工作区 {settings.workspace} · 模型 {settings.display_model()} · Ctrl-C 停止服务", flush=True)
        if open_browser:
            webbrowser.open(url)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def run_web(
    settings: Settings,
    host: str,
    port: int | None,
    token: str | None,
    require_token: bool = True,
    open_browser: bool = True,
) -> None:
    """settings.workspace（-C 或当前目录）会加进工作区列表，并作为新任务的默认工作区。"""
    if not require_token and not _is_loopback(host):
        raise SystemExit("监听非本机地址时必须带 token：这个服务能在你的机器上执行任意命令")
    app_config = AppConfig.load(settings.config_path)
    _put_workspace_first(app_config, settings)
    resolved_token = _resolve_token(settings, app_config, token, require_token)
    app = create_app(settings, resolved_token, app_config=app_config, run_scheduler=True)
    try:
        asyncio.run(_serve(app, settings, host, port, open_browser))
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


def _put_workspace_first(app_config: AppConfig, settings: Settings) -> None:
    workspace = str(settings.workspace.resolve())
    workspaces = [path for path in app_config.workspaces if path != workspace]
    app_config.workspaces = [workspace] + workspaces
    app_config.save(settings.config_path)
