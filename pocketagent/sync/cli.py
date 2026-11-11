"""pocketagent sync 子命令：init / run / status / conflicts。

一次同步只做一次远端列目录，然后按文件决定动作，输出「上传几个、下载几个、合并几个」。
配置和底稿都在 ~/.pocketagent 下，不碰仓库目录。
"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from pocketagent.config import config_path, default_home_dir
from pocketagent.sync.backend import SyncError
from pocketagent.sync.config import (
    BACKEND_KINDS,
    DEFAULT_INCLUDES,
    DEFAULT_MAX_FILE_BYTES,
    INCLUDE_CHOICES,
    BackendSettings,
    SyncConfig,
    SyncConfigError,
    default_local_directory,
    make_device_id,
    sync_config_path,
    sync_state_dir,
)
from pocketagent.sync.engine import SyncEngine, create_backend

EXIT_OK = 0
EXIT_ERROR = 1

RISKY_INCLUDES = {
    "config": "config.json 里有模型 API Key，会原样传到你自己的云盘（web_token 会去掉）",
    "automations": "自动化会在两台机器上各跑一遍，可能重复触发",
    "mcp": "mcp.json 里可能有第三方服务的密钥",
    "hooks": "钩子脚本会在另一台机器上被执行",
}


def main(argv: list[str], home_dir: Path | None = None) -> int:
    # console.main 把整个命令行传进来（第一个词是 sync），这里剥掉
    arguments = list(argv[1:]) if argv and argv[0] == "sync" else list(argv)
    command = arguments[0] if arguments else "run"
    rest = arguments[1:]
    home = Path(home_dir) if home_dir else default_home_dir()
    if command in ("-h", "--help"):
        _top_parser().print_help()
        return EXIT_OK
    if command == "init":
        return run_init_command(rest, home)
    if command == "status":
        return run_status_command(rest, home)
    if command == "conflicts":
        return run_conflicts_command(rest, home)
    if command == "run":
        return run_sync_command(rest, home)
    print(f"不认识的子命令 {command}；可选 init / run / status / conflicts", file=sys.stderr)
    return EXIT_ERROR


def _top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocketagent sync",
        description="把会话和项目记忆同步到自己的云端（GitHub 私有仓库 / 坚果云 WebDAV / 同步目录）",
    )
    parser.add_argument("command", nargs="?", choices=["init", "run", "status", "conflicts"], default="run")
    parser.add_argument("options", nargs="*", help="子命令自己的参数，用 pocketagent sync <子命令> -h 看")
    return parser


def _init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocketagent sync init",
        description="配置同步。默认 GitHub 私有仓库（--url owner/repo，数据放仓库的 pocketagent/ 目录）；"
        "坚果云 --backend webdav，密码用「第三方应用密码」",
    )
    parser.add_argument(
        "--backend",
        choices=BACKEND_KINDS,
        default="github",
        help="github（私有仓库，默认）/ webdav（坚果云等）/ local（已在同步的目录）",
    )
    parser.add_argument("--url", help="github 的 owner/repo（数据放仓库的 pocketagent/ 目录）、webdav 仓库地址，或 local 目录")
    parser.add_argument("--username", default="", help="webdav 用户名（坚果云是登录邮箱）")
    parser.add_argument("--password", default="", help="webdav 密码；不写就现场输入，或从环境变量读")
    parser.add_argument("--password-env", default="POCKETAGENT_SYNC_PASSWORD", help="从哪个环境变量读密码")
    parser.add_argument("--branch", default="main", help="GitHub 仓库的分支，默认 main")
    parser.add_argument(
        "--include",
        default=None,
        help=f"要同步的数据，逗号分隔：{','.join(INCLUDE_CHOICES)}；默认 {','.join(DEFAULT_INCLUDES)}",
    )
    parser.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_BYTES // 1024 // 1024, help="单个文件上限（MB）")
    parser.add_argument("--no-check", action="store_true", help="保存后不立即连一次远端")
    return parser


def run_init_command(argv: list[str], home_dir: Path) -> int:
    arguments = _init_parser().parse_args(argv)
    url = arguments.url
    if not url and arguments.backend == "local":
        url = str(default_local_directory())
    if not url:
        default_url = "https://dav.jianguoyun.com/dav/pocketagent" if arguments.backend == "webdav" else ""
        prompt = "仓库地址" if arguments.backend != "github" else "GitHub 仓库（owner/repo）"
        url = input(f"{prompt}{'（直接回车用 ' + default_url + '）' if default_url else ''}：").strip() or default_url
    if not url:
        print("地址不能为空", file=sys.stderr)
        return EXIT_ERROR

    existing = None
    try:
        existing = SyncConfig.load(home_dir)
    except SyncConfigError:
        existing = None
    include_text = arguments.include or ",".join(existing.includes if existing else DEFAULT_INCLUDES)
    includes = [item.strip() for item in include_text.split(",") if item.strip()]
    unknown = [item for item in includes if item not in INCLUDE_CHOICES]
    if unknown:
        print(f"不认识的同步项：{', '.join(unknown)}；可选 {', '.join(INCLUDE_CHOICES)}", file=sys.stderr)
        return EXIT_ERROR

    password = arguments.password
    if not password and existing is not None:
        # 重跑 init 只是想改范围时不把已经存下的密码抹掉
        password = existing.backend.password
    if arguments.backend in ("webdav", "github") and not password:
        # 已经能从环境变量读到就不用问
        if not os.environ.get(arguments.password_env):
            label = "GitHub Personal Access Token" if arguments.backend == "github" else "WebDAV 密码"
            hint = "" if arguments.backend == "github" else "（坚果云要第三方应用密码）"
            password = getpass.getpass(f"{label}{hint}，输入时不显示：")

    if arguments.backend == "local":
        # 指向 iCloud 这类目录时目录可能还不存在，先建出来省得第一次检查就报错
        Path(url).expanduser().mkdir(parents=True, exist_ok=True)

    settings = BackendSettings(
        kind=arguments.backend,
        url=url,
        username=arguments.username,
        password=password,
        password_env=arguments.password_env,
        branch=arguments.branch,
    )
    config = SyncConfig(
        device_id=existing.device_id if existing else make_device_id(),
        backend=settings,
        includes=list(dict.fromkeys(includes)),
        max_file_bytes=max(1, arguments.max_file_mb) * 1024 * 1024,
    )
    config.save(home_dir)
    print(f"同步设置已写入 {config_path(home_dir)}（按设备 {config.device_id} 分开）")
    print(f"本机身份和密码在 {sync_config_path(home_dir)}（权限 0600，不参与同步）")
    print(f"设备标识：{config.device_id}")
    print(f"同步范围：{', '.join(config.includes)}")
    for include in config.includes:
        if include in RISKY_INCLUDES:
            print(f"  注意：{RISKY_INCLUDES[include]}")
    if arguments.backend == "webdav":
        print("提示：坚果云免费版每月 1GB 上传 / 3GB 下载；会话和项目记忆通常只有十几 MB")
    if arguments.backend == "github":
        print("提示：数据放在仓库的 pocketagent/ 目录下；仓库要已有初始提交，Token 需要 Contents 读写权限")

    if arguments.no_check:
        return EXIT_OK
    try:
        engine = SyncEngine(home_dir, config)
        engine.backend.ping()
        report = engine.status()
    except SyncError as error:
        print(f"配置存下了，但连远端失败：{error}", file=sys.stderr)
        print("检查地址、账号和「第三方应用密码」后重跑 pocketagent sync status", file=sys.stderr)
        return EXIT_ERROR
    print(f"连接正常：远端已有 {report.remote_files} 个文件，本地 {report.local_files} 个")
    if report.changed():
        print("还有没同步的改动，跑 pocketagent sync 对齐")
    return EXIT_OK


def run_status_command(argv: list[str], home_dir: Path) -> int:
    parser = argparse.ArgumentParser(prog="pocketagent sync status", description="看两边差了什么，不改任何文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON，给脚本用")
    arguments = parser.parse_args(argv)
    config = _load_config(home_dir)
    if config is None:
        return EXIT_ERROR
    try:
        report = SyncEngine(home_dir, config).status()
    except SyncError as error:
        print(f"同步状态查不了：{error}", file=sys.stderr)
        return EXIT_ERROR
    if arguments.json:
        print(json.dumps(_report_dict(report), ensure_ascii=False, indent=2))
    else:
        for line in report.lines():
            print(line)
        if not report.changed():
            print("两边已经一致")
    return EXIT_ERROR if report.errors else EXIT_OK


def run_sync_command(argv: list[str], home_dir: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="pocketagent sync",
        description="双向同步：本地改的传上去，远端改的拉下来，两边都改的按规则合并",
    )
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--push", action="store_true", help="只上传本地改动；远端也改过的先跳过")
    direction.add_argument("--pull", action="store_true", help="只拉取远端改动；本地也改过的先跳过")
    parser.add_argument("-n", "--dry-run", action="store_true", help="只说要做什么，不写文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON，给脚本用")
    parser.add_argument("-q", "--quiet", action="store_true", help="没有改动时不打印")
    arguments = parser.parse_args(argv)
    config = _load_config(home_dir)
    if config is None:
        return EXIT_ERROR
    chosen = "push" if arguments.push else "pull" if arguments.pull else "both"
    try:
        engine = SyncEngine(home_dir, config)
        report = engine.run(direction=chosen, dry_run=arguments.dry_run)
    except SyncError as error:
        print(f"同步失败：{error}", file=sys.stderr)
        return EXIT_ERROR
    if arguments.json:
        print(json.dumps(_report_dict(report), ensure_ascii=False, indent=2))
    elif not arguments.quiet or report.changed() or report.errors:
        prefix = "（演练，没有写文件）" if arguments.dry_run else ""
        print(f"同步完成{prefix}")
        for line in report.lines():
            print(line)
    return EXIT_ERROR if report.errors else EXIT_OK


def run_conflicts_command(argv: list[str], home_dir: Path) -> int:
    parser = argparse.ArgumentParser(prog="pocketagent sync conflicts", description="看/清冲突时另存的远端版本")
    parser.add_argument("--clear", action="store_true", help="删掉这些备份")
    arguments = parser.parse_args(argv)
    directory = sync_state_dir(home_dir) / "conflicts"
    if not directory.is_dir():
        print("没有冲突记录")
        return EXIT_OK
    files = sorted(path for path in directory.iterdir() if path.is_file())
    if not files:
        print("没有冲突记录")
        return EXIT_OK
    for path in files:
        print(path.name)
    if arguments.clear:
        for path in files:
            path.unlink(missing_ok=True)
        print(f"已删除 {len(files)} 个冲突备份")
    else:
        print(f"共 {len(files)} 个；确认没用后加 --clear 删除")
    return EXIT_OK


def _load_config(home_dir: Path) -> SyncConfig | None:
    try:
        return SyncConfig.load(home_dir)
    except SyncConfigError as error:
        print(str(error), file=sys.stderr)
        return None


def _report_dict(report) -> dict:
    return report.to_dict()
