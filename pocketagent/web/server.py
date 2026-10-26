"""web 服务（aiohttp）：静态页面 + REST + 每个会话一条 WebSocket。

浏览器 → 服务（WebSocket，JSON）：
  {"type": "user_message", "text": ...}
  {"type": "approval", "request_id": ..., "answer": "allow_once" | "allow_always" | "deny"}
  {"type": "interrupt"} / {"type": "compact"}
  {"type": "set_mode", "mode": ...} / {"type": "set_model", "model_id": ...}
服务 → 浏览器：
  snapshot（连上时的完整历史）、state（是否在跑、审批模式、当前模型）、event（agent 事件，见 events.py）、
  approval_request / approval_resolved、error

REST：
  GET  /api/info                     工作目录、可选模型、审批模式
  GET  /api/sessions                 这个目录的历史会话
  POST /api/sessions                 新建会话
  GET  /api/sessions/{id}            会话快照（同 WebSocket 的 snapshot）
  GET  /api/config                   配置（密钥只给提示，不给原文）
  PUT  /api/config                   保存配置，立刻对所有会话生效
  POST /api/config/test              用表单里的模型配置真的调一次模型

页面里所有地址都是相对路径，挂在网关子路径下（如 /web-xxx/pocketagent/）也能用。
"""

import asyncio
import json
import secrets
import webbrowser
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

from pocketagent.config import AppConfig, ConfigError, Settings
from pocketagent.permissions import ApprovalMode
from pocketagent.web.runtime import ProviderFactory, SessionBusyError, SessionManager, WebSession

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_COOKIE_NAME = "pocketagent_token"
DEFAULT_PORT = 8765
PORT_SEARCH_LIMIT = 20

MANAGER_KEY = web.AppKey("manager", SessionManager)
TOKEN_KEY = web.AppKey("token", str)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False), content_type="application/json", status=status)


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


# ---- 页面和 REST ----


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def get_info(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    app_config = manager.app_config
    default_profile = app_config.default_profile()
    return _json_response(
        {
            "workspace": str(manager.base_settings.workspace),
            "models": [{"id": profile.id, "name": profile.name, "model": profile.model} for profile in app_config.models],
            "default_model": default_profile.id if default_profile else None,
            # 一个模型都没配时，显示从环境变量读到的模型名
            "fallback_model": manager.base_settings.model,
            "default_mode": manager.base_settings.approval_mode.value,
            "modes": [mode.value for mode in ApprovalMode],
        }
    )


async def list_sessions(request: web.Request) -> web.Response:
    return _json_response({"sessions": request.app[MANAGER_KEY].list_saved()})


async def create_session(request: web.Request) -> web.Response:
    web_session = request.app[MANAGER_KEY].create()
    return _json_response({"id": web_session.id})


def _find_session(request: web.Request) -> WebSession:
    try:
        return request.app[MANAGER_KEY].get(request.match_info["session_id"])
    except KeyError:
        raise web.HTTPNotFound(text="会话不存在")


async def get_session(request: web.Request) -> web.Response:
    return _json_response(_find_session(request).snapshot_message())


def _config_payload(manager: SessionManager) -> dict[str, Any]:
    return {**manager.app_config.public_dict(), "config_path": str(manager.base_settings.config_path)}


async def get_config(request: web.Request) -> web.Response:
    return _json_response(_config_payload(request.app[MANAGER_KEY]))


async def put_config(request: web.Request) -> web.Response:
    manager = request.app[MANAGER_KEY]
    try:
        new_config = manager.config_from_form(await request.json())
    except ConfigError as error:
        return _json_response({"error": str(error)}, status=400)
    await manager.apply_config(new_config)
    return _json_response(_config_payload(manager))


async def test_config(request: web.Request) -> web.Response:
    try:
        result = await request.app[MANAGER_KEY].test_model(await request.json())
    except ConfigError as error:
        return _json_response({"ok": False, "error": str(error)})
    return _json_response(result)


# ---- WebSocket ----


async def session_socket(request: web.Request) -> web.StreamResponse:
    web_session = _find_session(request)
    client = web.WebSocketResponse(heartbeat=30)
    await client.prepare(request)
    web_session.clients.add(client)
    await web_session.send(client, web_session.snapshot_message())
    manager = request.app[MANAGER_KEY]
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
            if text:
                web_session.start_turn(text)
        elif message_type == "approval":
            web_session.answer_approval(message["request_id"], message["answer"])
        elif message_type == "interrupt":
            web_session.interrupt()
        elif message_type == "compact":
            web_session.start_compaction()
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
) -> web.Application:
    """token 传空字符串表示不鉴权。provider_factory 留空就用默认的 OpenAI 兼容接口，测试里传假模型。
    app_config 留空就读 settings.home_dir 下的配置文件。"""
    if app_config is None:
        app_config = AppConfig.load(settings.config_path)
    app = web.Application(middlewares=[token_middleware])
    app[TOKEN_KEY] = token
    app[MANAGER_KEY] = SessionManager(settings, app_config, provider_factory)
    app.router.add_get("/", index)
    app.router.add_get("/api/info", get_info)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_post("/api/config/test", test_config)
    app.router.add_get("/ws/{session_id}", session_socket)
    app.router.add_static("/static/", STATIC_DIR)
    app.on_shutdown.append(_shutdown_sessions)
    return app


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
    """指定了端口就只试这一个；没指定就从 8765 往后找空闲的，方便同时给几个项目各开一个。"""
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
        print(f"工作目录 {settings.workspace} · 模型 {settings.display_model()} · Ctrl-C 停止服务", flush=True)
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
    if not require_token and not _is_loopback(host):
        raise SystemExit("监听非本机地址时必须带 token：这个服务能在你的机器上执行任意命令")
    app_config = AppConfig.load(settings.config_path)
    resolved_token = _resolve_token(settings, app_config, token, require_token)
    app = create_app(settings, resolved_token, app_config=app_config)
    try:
        asyncio.run(_serve(app, settings, host, port, open_browser))
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
