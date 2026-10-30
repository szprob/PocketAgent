"""组装和启动后端：中间件（token 鉴权、错误转 JSON）、路由、web 前端的静态页面、定时任务调度。

接口协议见 pocketagent/server/__init__.py。
"""


import asyncio
import secrets
import webbrowser
from typing import Awaitable, Callable

from aiohttp import web

from pocketagent.config import AppConfig, ConfigError, Settings
from pocketagent.safety.checkpoints import CheckpointError
from pocketagent.server.common import MANAGER_KEY, error_response
from pocketagent.server.manager import SessionManager
from pocketagent.server.routes import add_routes
from pocketagent.server.session import ProviderFactory, RewindError, SessionBusyError
from pocketagent.web import STATIC_DIR

TOKEN_COOKIE_NAME = "pocketagent_token"
DEFAULT_PORT = 8765
PORT_SEARCH_LIMIT = 20

TOKEN_KEY = web.AppKey("token", str)
SCHEDULER_KEY = web.AppKey("scheduler", asyncio.Task)
WARM_UP_KEY = web.AppKey("warm_up", asyncio.Future)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ---- 鉴权：和 Jupyter 一样，第一次用 ?token= 打开，之后靠 cookie ----


def _is_webhook_path(path: str) -> bool:
    """外部触发自己验签（密钥在每条自动化上），不能用主 token 拦。"""
    return path.startswith("/api/triggers/") and path.endswith("/hook")


@web.middleware
async def token_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    expected_token = request.app[TOKEN_KEY]
    if not expected_token:
        return await handler(request)
    if _is_webhook_path(request.path):
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
        return error_response(str(error))
    except SessionBusyError as error:
        return error_response(str(error), status=409)
    except RewindError as error:
        return error_response(str(error), status=409)
    except KeyError as error:
        return error_response(f"找不到：{error}", status=404)
    except CheckpointError as error:
        return error_response(str(error))


# ---- web 前端的页面 ----


@web.middleware
async def static_no_cache_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """static 里的 js/css 改了要能马上生效：不声明 no-cache 时浏览器会按启发式缓存继续用旧的。"""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


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
    app = web.Application(
        middlewares=[token_middleware, error_middleware, static_no_cache_middleware],
        client_max_size=25 * 1024 * 1024,
    )
    app[TOKEN_KEY] = token
    app[MANAGER_KEY] = SessionManager(settings, app_config, provider_factory)
    add_routes(app.router)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC_DIR)
    if run_scheduler:
        app.on_startup.append(_start_scheduler)
        app.on_startup.append(_warm_up)
        app.on_cleanup.append(_stop_scheduler)
    app.on_shutdown.append(_shutdown_sessions)
    return app


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
        default_workspace = app[MANAGER_KEY].default_workspace()
        print(f"默认工作区 {default_workspace} · 模型 {settings.display_model()} · Ctrl-C 停止服务", flush=True)
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
    explicit_workspace: bool = False,
) -> None:
    """explicit_workspace：用户用 -C 指定了工作区，就把它加进工作区列表并排第一（见 _register_launch_workspace）。"""
    if not require_token and not _is_loopback(host):
        raise SystemExit("监听非本机地址时必须带 token：这个服务能在你的机器上执行任意命令")
    app_config = AppConfig.load(settings.config_path)
    _register_launch_workspace(app_config, settings, explicit_workspace)
    resolved_token = _resolve_token(settings, app_config, token, require_token)
    app = create_app(settings, resolved_token, app_config=app_config, run_scheduler=True)
    try:
        asyncio.run(_serve(app, settings, host, port, open_browser))
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


def _register_launch_workspace(app_config: AppConfig, settings: Settings, explicit: bool) -> None:
    """-C 指定的目录排在工作区列表最前面；没指定时只在列表为空（第一次用）时把启动目录加进去。

    以前不管有没有 -C 都把启动目录加回来：桌面端总在家目录启动后端，用户在界面里移除的工作区
    每次打开又冒出来。配置里已经指定了默认工作区就尊重配置，不覆盖。
    """
    if not explicit and app_config.workspaces:
        return
    workspace = str(settings.workspace.resolve())
    workspaces = [path for path in app_config.workspaces if path != workspace]
    app_config.workspaces = [workspace] + workspaces
    if app_config.default_workspace is None:
        app_config.default_workspace = workspace
    app_config.save(settings.config_path)
