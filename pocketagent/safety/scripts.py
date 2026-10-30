"""跑用户自己的脚本：钩子和触发器的条件检查共用这一份。

两边的要求是一样的——给 stdin 一份 JSON、限时、超时连派生出来的子进程一起结束。
差别只在「退出码不是 0 意味着什么」：钩子把它当成出错（退出码 2 还意味着拦住），
触发器把它当成「这次不算数」。那层解释留给调用方，这里只负责把进程跑完。

脚本按用户的权限跑，不进沙箱、不要审批：它本来就是用来看住 agent 的，
再让 agent 去批准它就说不通了。
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from pocketagent.safety.processes import bash_executable, terminate_process_tree

MIN_SCRIPT_TIMEOUT_SECONDS = 1
MAX_SCRIPT_TIMEOUT_SECONDS = 600


@dataclass
class ScriptResult:
    """一次脚本执行的结果。timed_out 时 exit_code 是 None。"""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    # 这台机器上没有 bash：不是脚本的错，是环境的问题，界面上要说清楚
    missing_bash: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def describe_failure(self) -> str:
        """给界面用的一句话，说明这次为什么没跑成。"""
        if self.missing_bash:
            return "这台机器上找不到 bash，脚本没有执行"
        if self.timed_out:
            return "脚本超时，已经终止"
        detail = (self.stderr or self.stdout or "没有输出").strip()
        if self.exit_code is None:
            return f"脚本起不来：{detail}"
        return f"退出码 {self.exit_code}：{detail}"


async def run_script(
    command: str,
    *,
    payload: dict[str, Any],
    cwd: str,
    timeout_seconds: int,
    environment: dict[str, str] | None = None,
) -> ScriptResult:
    """把 payload 从 stdin 喂给脚本，等它结束；超时就连子进程一起杀掉。"""
    bash = bash_executable()
    if bash is None:
        return ScriptResult(exit_code=None, missing_bash=True)
    process_environment = dict(os.environ)
    if environment:
        process_environment.update(environment)
    try:
        process = await asyncio.create_subprocess_exec(
            bash,
            "-c",
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_environment,
            # 单独的进程组：超时能连脚本派生出来的子进程一起结束
            start_new_session=True,
        )
    except OSError as error:
        return ScriptResult(exit_code=None, stderr=f"{type(error).__name__}: {error}")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(body), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await terminate_process_tree(process)
        return ScriptResult(exit_code=None, timed_out=True)
    return ScriptResult(
        exit_code=process.returncode,
        stdout=stdout.decode("utf-8", "replace").strip(),
        stderr=stderr.decode("utf-8", "replace").strip(),
    )
