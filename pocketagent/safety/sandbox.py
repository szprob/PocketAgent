"""bash 的笼子：命令只能写工作目录、临时目录、缓存目录和免问目录，读不到密钥，默认不能联网。

- macOS：系统自带的 sandbox-exec（Seatbelt），Codex 用的也是它
- Linux：bubblewrap（bwrap）；Linux 这条路径还没有在真机上测过
- Windows：还没有沙箱实现，命令直接执行
- 都没有：照常执行，但在结果里写明「没有隔离」

真需要在笼子外跑（装依赖要联网、写工作目录外的文件），bash 带上 outside_sandbox=true，
这种调用一定要用户确认（「完全访问」模式除外）。
"""

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pocketagent.processes import bash_executable

# 不管怎样都不让命令读的地方：密钥、凭据
SECRET_PATHS = [
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.netrc",
    "~/.config/gh",
    "~/.docker/config.json",
    "~/.kube/config",
]
# 除了工作目录，命令还能写的地方：临时目录、各种工具的缓存
EXTRA_WRITABLE = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "~/.cache", "~/Library/Caches"]
# 工作目录里也不让写的：改了 hooks / config 能让代码在笼子外执行
PROTECTED_IN_WORKSPACE = [".git/hooks", ".git/config"]
# 看到这些输出，多半是被笼子拦了，给模型一句提示
DENIAL_HINTS = (
    "Operation not permitted",
    "Permission denied",
    "Could not resolve host",
    "nodename nor servname",
    "Temporary failure in name resolution",
    "Network is unreachable",
    "Failed to establish a new connection",
    "getaddrinfo",
)


def default_secret_dirs() -> list[Path]:
    return [Path(os.path.expanduser(item)) for item in SECRET_PATHS]


class BashUnavailable(RuntimeError):
    """找不到 bash，命令没法执行。"""


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass
class SandboxPolicy:
    workspace: Path
    allow_network: bool = False
    # 读不到的地方：密钥、凭据，再加上 PocketAgent 自己的目录（放着 API Key）
    secret_dirs: list[Path] = field(default_factory=default_secret_dirs)
    # 上面这些目录里例外能读的，例如这个会话的附件目录
    readable_exceptions: list[Path] = field(default_factory=list)
    # 额外能写的地方：用户配的免问目录（工作目录以外，读写都不用确认）
    writable_roots: list[Path] = field(default_factory=list)

    def all_writable(self) -> list[Path]:
        roots = [self.workspace] + [Path(os.path.expanduser(item)) for item in EXTRA_WRITABLE] + self.writable_roots
        roots.append(Path(tempfile.gettempdir()))
        return _unique_real_paths(roots)

    def writable_inside_secret(self) -> list[Path]:
        """能写、但落在密钥目录里的那些（临时任务的工作区在 ~/.pocketagent/scratch 下）。

        密钥目录默认连读都不让读，这些得单独放开，否则临时任务里连 ls 都跑不动。
        """
        secrets = _unique_real_paths(self.secret_dirs)
        return [root for root in self.all_writable() if any(is_within(root, secret) for secret in secrets)]

    def protected_paths(self) -> list[Path]:
        return [self.workspace.resolve() / relative for relative in PROTECTED_IN_WORKSPACE]


def _unique_real_paths(paths: list[Path]) -> list[Path]:
    """沙箱规则按真实路径匹配（macOS 的 /tmp 其实是 /private/tmp），原路径和真实路径都写上。"""
    unique: list[Path] = []
    for path in paths:
        for candidate in (path, path.resolve()):
            if candidate not in unique:
                unique.append(candidate)
    return unique


def available_sandbox() -> str | None:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap"
    return None


def _sbpl_string(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _subpaths(paths: list[Path]) -> str:
    return " ".join(f"(subpath {_sbpl_string(path)})" for path in paths)


def seatbelt_profile(policy: SandboxPolicy) -> str:
    """macOS Seatbelt 规则。后写的规则优先，所以先放开、再收紧。"""
    writable = _subpaths(policy.all_writable())
    secrets = _subpaths(_unique_real_paths(policy.secret_dirs))
    # 工作区可能就在密钥目录里（临时任务），也要算进读例外
    readable = [*policy.readable_exceptions, *policy.writable_inside_secret()]
    exceptions = _subpaths(_unique_real_paths(readable))
    protected = _subpaths(policy.protected_paths())
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm*)",
        "(allow ipc-posix-sem)",
        "(allow iokit-open)",
        "(allow pseudo-tty)",
        "(allow file-ioctl)",
        "(allow file-read*)",
        f"(deny file-read* {secrets})",
        f"(allow file-read* {exceptions})" if exceptions else "",
        f"(allow file-write* {writable})",
        '(allow file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/stdout") (literal "/dev/stderr")'
        ' (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
        f"(deny file-write* {protected})",
        # 本机进程间通信（unix socket）要放开，不然很多工具起不来
        "(allow network* (local unix-socket))",
        "(allow network-outbound (remote unix-socket))",
    ]
    if policy.allow_network:
        lines.append("(allow network*)")
    return "\n".join(line for line in lines if line)


def bubblewrap_arguments(policy: SandboxPolicy) -> list[str]:
    """Linux bubblewrap：整个根目录只读挂进去，再把能写的目录可写挂上，密钥目录用空目录盖住。"""
    arguments = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--die-with-parent"]
    writable = policy.all_writable()
    for root in writable:
        if root.exists():
            arguments += ["--bind", str(root), str(root)]
    for protected in policy.protected_paths():
        if protected.exists():
            arguments += ["--ro-bind", str(protected), str(protected)]
    for secret in _unique_real_paths(policy.secret_dirs):
        if secret.is_dir():
            arguments += ["--tmpfs", str(secret)]
        elif secret.exists():
            arguments += ["--ro-bind", "/dev/null", str(secret)]
    # 密钥目录盖住之后，再把例外的子目录挂回来（只读）
    writable_inside_secret = policy.writable_inside_secret()
    for exception in policy.readable_exceptions:
        if any(is_within(exception, root) for root in writable_inside_secret):
            # 这个是临时任务的工作区，下面要按可写挂回来，别先挂成只读
            continue
        if exception.exists():
            arguments += ["--ro-bind", str(exception), str(exception)]
    # 工作区在密钥目录里（临时任务）：盖住之后重新挂回去，而且得能写
    for root in writable_inside_secret:
        if root.exists():
            arguments += ["--bind", str(root), str(root)]
    if not policy.allow_network:
        arguments.append("--unshare-net")
    return arguments


def wrap_command(command: str, policy: SandboxPolicy | None) -> tuple[list[str], str]:
    """返回 (要执行的参数, 说明)；说明非空时附在工具结果里告诉模型。"""
    bash = bash_executable()
    if bash is None:
        raise BashUnavailable(
            "找不到 bash：Windows 上需要安装 Git for Windows（https://git-scm.com/download/win），"
            "已经装了就把 Git 的 bin 目录放进 PATH；macOS / Linux 上装好 bash 后重试"
        )
    plain = [bash, "-c", command]
    if policy is None:
        return plain, ""
    kind = available_sandbox()
    if kind == "seatbelt":
        return ["sandbox-exec", "-p", seatbelt_profile(policy), *plain], ""
    if kind == "bubblewrap":
        return [*bubblewrap_arguments(policy), *plain], ""
    return plain, "（这台机器没有可用的沙箱：macOS 需要 sandbox-exec，Linux 需要 bubblewrap，Windows 还没有沙箱实现；这条命令没有隔离）"


def denial_hint(output: str, policy: SandboxPolicy | None) -> str:
    if policy is None or not any(hint in output for hint in DENIAL_HINTS):
        return ""
    network = "可以联网" if policy.allow_network else "不能联网"
    return (
        f"\n（可能是被沙箱拦了：命令只能写工作目录、临时目录和缓存目录，{network}，读不到密钥目录。"
        "确实需要的话，用 outside_sandbox=true 再执行一次，会请用户确认。）"
    )
