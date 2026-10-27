"""ripgrep 的调用封装：找可执行文件、跑搜索、解析 --json 输出。

只在这里碰外部进程；search.py 负责把它包成工具，并在 rg 用不了时回退到纯 Python 实现。
回退不是可选项：Rust 正则不支持 lookaround 这类 Python 写法，旧机器上也可能没装 rg。
"""

import asyncio
import base64
import json
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

GREP_TIMEOUT_SECONDS = 60.0
GLOB_TIMEOUT_SECONDS = 30.0
# asyncio 的 StreamReader 默认单行上限 64KB；一行的 JSON 里有整行源码，放宽一些
MAX_JSON_LINE_BYTES = 8_000_000
# 这些规则让 rg 的行为和原来的 Python 实现对齐：搜隐藏文件、不读用户自己的 ripgreprc、
# 不依赖当前目录是不是 git 仓库（--no-require-git），但 .git 本身永远不搜。
BASE_ARGUMENTS = [
    "--no-config",
    "--color", "never",
    "--hidden",
    "--no-require-git",
    "--glob", "!.git",
    "--glob", "!.git/**",
]


class RipgrepError(RuntimeError):
    """rg 用不了：没装、参数不认、正则语法不支持……调用方回退 Python 实现。"""


class RipgrepTimeout(RipgrepError):
    """搜索/列文件超时。不要回退 Python，那只会更慢。"""


@dataclass
class GrepMatch:
    path: Path
    line_number: int
    line: str


def find_ripgrep(configured_path: str | None = None) -> str | None:
    """按「配置里写的 > POCKETAGENT_RIPGREP > PATH」找 rg；找不到返回 None。"""
    for candidate in (configured_path, os.environ.get("POCKETAGENT_RIPGREP")):
        resolved = _resolve_executable(candidate)
        if resolved:
            return resolved
    return shutil.which("rg")


def _resolve_executable(candidate: str | None) -> str | None:
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return shutil.which(candidate)


def _base_directory(root: Path) -> Path:
    """rg 的输出是相对 cwd 的：目录就在它里面找，文件就在它父目录里找。"""
    return root if root.is_dir() else root.parent


def _search_argument(root: Path) -> str:
    return "." if root.is_dir() else root.name


def _resolve_output_path(line: str, base_directory: Path) -> Path:
    path = Path(line)
    return path if path.is_absolute() else base_directory / path


def _decode_json_text(value: object) -> str:
    """--json 里的路径和内容可能是 {"text": ...}，非 UTF-8 时是 {"bytes": base64}。"""
    if not isinstance(value, dict):
        return ""
    if "text" in value:
        return str(value["text"])
    encoded = value.get("bytes")
    if not isinstance(encoded, str):
        return ""
    try:
        return base64.b64decode(encoded).decode("utf-8", "replace")
    except ValueError:
        return ""


def parse_match_line(raw_line: bytes) -> GrepMatch | None:
    """解析 rg --json 的一行；不是 match 事件（begin/end/summary）或解析不了就返回 None。"""
    try:
        event = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(event, dict) or event.get("type") != "match":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path_text = _decode_json_text(data.get("path"))
    line_text = _decode_json_text(data.get("lines"))
    try:
        line_number = int(data.get("line_number") or 0)
    except (TypeError, ValueError):
        return None
    if not path_text or line_number <= 0:
        return None
    return GrepMatch(path=Path(path_text), line_number=line_number, line=line_text.rstrip("\r\n"))


async def _collect_stderr(process: asyncio.subprocess.Process) -> bytes:
    if process.stderr is None:
        return b""
    return await process.stderr.read()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def _error_text(stderr: bytes) -> str:
    return stderr.decode("utf-8", "replace").strip()[:500]


async def grep(
    executable: str,
    pattern: str,
    root: Path,
    ignore_case: bool,
    max_matches: int,
    max_file_bytes: int,
    keep_path: Callable[[Path], bool] | None = None,
) -> list[GrepMatch]:
    """跑一次 rg --json，返回前 max_matches 条匹配；一条都没有返回空列表。

    keep_path 用来按文件名过滤（调用方传 glob）。不在 rg 里用 --glob，是因为
    rg 的显式 glob 会覆盖 .gitignore；文件名过滤放在这里，.gitignore 就还能生效。
    """
    base_directory = _base_directory(root)
    arguments = [
        executable,
        *BASE_ARGUMENTS,
        "--json",
        f"--max-filesize={max_file_bytes}",
    ]
    if ignore_case:
        arguments.append("--ignore-case")
    arguments.extend(["--regexp", pattern, "--", _search_argument(root)])
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=base_directory,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=MAX_JSON_LINE_BYTES,
    )
    stderr_reader = asyncio.create_task(_collect_stderr(process))
    matches: list[GrepMatch] = []
    timed_out = False
    try:
        async with asyncio.timeout(GREP_TIMEOUT_SECONDS):
            async for raw_line in process.stdout:
                match = parse_match_line(raw_line)
                if match is None:
                    continue
                resolved_path = _resolve_output_path(str(match.path), base_directory)
                if keep_path is not None and not keep_path(resolved_path):
                    continue
                matches.append(
                    GrepMatch(
                        path=resolved_path,
                        line_number=match.line_number,
                        line=match.line,
                    )
                )
                if len(matches) >= max_matches:
                    break
    except TimeoutError:
        timed_out = True
    finally:
        await _stop_process(process)
        stderr = await stderr_reader
    if timed_out:
        raise RipgrepTimeout(f"搜索超过 {GREP_TIMEOUT_SECONDS:.0f} 秒")
    if process.returncode == 2 and not matches:
        raise RipgrepError(_error_text(stderr) or "rg 退出码 2")
    return matches


async def list_files(executable: str, root: Path) -> list[Path]:
    """rg --files：列出目录里没被 .gitignore 忽略的文件。文件名过滤由调用方做。"""
    base_directory = _base_directory(root)
    arguments = [
        executable,
        *BASE_ARGUMENTS,
        "--files",
        "--", _search_argument(root),
    ]
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=base_directory,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=MAX_JSON_LINE_BYTES,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GLOB_TIMEOUT_SECONDS)
    except TimeoutError:
        await _stop_process(process)
        raise RipgrepTimeout(f"列文件超过 {GLOB_TIMEOUT_SECONDS:.0f} 秒")
    if process.returncode == 2:
        raise RipgrepError(_error_text(stderr) or "rg 退出码 2")
    paths = []
    for line in stdout.decode("utf-8", "replace").splitlines():
        if line:
            paths.append(_resolve_output_path(line, base_directory))
    return paths
