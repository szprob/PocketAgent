"""评测用例：一句（或几句）话 + 一个当工作区的样例目录 + agent 跑完之后的检查。

用例写成 JSON（不为这个引入 YAML 依赖）：一个文件放一个用例对象，或者一个用例数组；
给目录时读目录下所有 *.json，不往子目录找——样例目录里的 package.json 之类不会被误认成用例。

    {
      "name": "修复加法",
      "prompt": "calc.py 的 add 算错了，修好它",
      "fixture": "fixtures/calc",
      "checks": [
        {"type": "command", "command": "python3 -m unittest", "timeout_seconds": 120},
        {"type": "answer_contains", "text": "calc.py"}
      ]
    }

prompt 也可以是数组（按顺序一句一轮）；fixture 相对用例文件所在的目录，不写就从空目录开始。
检查的命令在 agent 改过的副本里跑，和 agent 的 bash 用同一套沙箱：副本里的代码可能是 agent 写的，
不能让它借检查命令跑到沙箱外面去。
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocketagent.evaluation.report import CheckResult
from pocketagent.safety.processes import terminate_process_tree
from pocketagent.safety.sandbox import BashUnavailable, SandboxPolicy, wrap_command

CHECK_COMMAND = "command"
CHECK_ANSWER_CONTAINS = "answer_contains"
CHECK_KINDS = (CHECK_COMMAND, CHECK_ANSWER_CONTAINS)
DEFAULT_CHECK_TIMEOUT_SECONDS = 120
# 检查失败时附上命令输出的最后这么多字，够看出是哪里挂了
CHECK_OUTPUT_TAIL_CHARS = 1500


class CaseError(ValueError):
    """用例文件写得不对。消息直接给用户看，带上是哪个文件。"""


@dataclass
class EvalCheck:
    kind: str
    command: str = ""
    text: str = ""
    timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS

    def describe(self) -> str:
        if self.kind == CHECK_COMMAND:
            return f"命令 `{self.command}` 退出码为 0"
        return f"回答里包含「{self.text}」"


@dataclass
class EvalCase:
    name: str
    prompts: list[str]
    # 当工作区复制的样例目录；None 表示从空目录开始
    fixture: Path | None
    checks: list[EvalCheck] = field(default_factory=list)
    source_file: Path | None = None


def load_cases(path: Path) -> list[EvalCase]:
    """读一个用例文件，或者一个目录下所有的 *.json。名字不能重复：报告里按名字区分用例。"""
    path = path.expanduser()
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        if not files:
            raise CaseError(f"{path} 下面没有 .json 用例文件")
    elif path.is_file():
        files = [path]
    else:
        raise CaseError(f"找不到用例：{path}")
    cases: list[EvalCase] = []
    for case_file in files:
        cases += _load_case_file(case_file)
    names = [case.name for case in cases]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise CaseError(f"用例名字重复了：{'、'.join(duplicates)}")
    return cases


def _load_case_file(case_file: Path) -> list[EvalCase]:
    try:
        data = json.loads(case_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseError(f"{case_file} 读不了或者不是合法的 JSON：{error}") from error
    items = data if isinstance(data, list) else [data]
    if not items:
        raise CaseError(f"{case_file} 里没有用例")
    return [_parse_case(item, case_file) for item in items]


def _parse_case(data: Any, case_file: Path) -> EvalCase:
    if not isinstance(data, dict):
        raise CaseError(f"{case_file}：每个用例都要是一个 JSON 对象")
    name = str(data.get("name") or "").strip()
    if not name:
        raise CaseError(f"{case_file}：用例缺少 name")
    prompts = _parse_prompts(data.get("prompt"), case_file, name)
    checks_data = data.get("checks") or []
    if not isinstance(checks_data, list) or not checks_data:
        raise CaseError(f"{case_file}「{name}」：至少要有一条检查（checks）")
    return EvalCase(
        name=name,
        prompts=prompts,
        fixture=_parse_fixture(data.get("fixture"), case_file, name),
        checks=[_parse_check(item, case_file, name) for item in checks_data],
        source_file=case_file,
    )


def _parse_prompts(value: Any, case_file: Path, name: str) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return list(value)
    raise CaseError(f"{case_file}「{name}」：prompt 要是一句话，或者一组话（数组，按顺序一句一轮）")


def _parse_fixture(value: Any, case_file: Path, name: str) -> Path | None:
    if value in (None, ""):
        return None
    fixture = (case_file.parent / str(value)).expanduser().resolve()
    if not fixture.is_dir():
        raise CaseError(f"{case_file}「{name}」：样例目录不存在：{fixture}")
    return fixture


def _parse_check(data: Any, case_file: Path, name: str) -> EvalCheck:
    where = f"{case_file}「{name}」"
    if not isinstance(data, dict) or data.get("type") not in CHECK_KINDS:
        raise CaseError(f"{where}：检查的 type 只能是 {' / '.join(CHECK_KINDS)}")
    if data["type"] == CHECK_COMMAND:
        command = str(data.get("command") or "").strip()
        if not command:
            raise CaseError(f"{where}：command 检查要写 command")
        timeout = data.get("timeout_seconds", DEFAULT_CHECK_TIMEOUT_SECONDS)
        if not isinstance(timeout, int) or timeout <= 0:
            raise CaseError(f"{where}：timeout_seconds 要是正整数")
        return EvalCheck(kind=CHECK_COMMAND, command=command, timeout_seconds=timeout)
    text = str(data.get("text") or "")
    if not text:
        raise CaseError(f"{where}：answer_contains 检查要写 text")
    return EvalCheck(kind=CHECK_ANSWER_CONTAINS, text=text)


# ---- 跑检查 ----


async def run_checks(
    checks: list[EvalCheck], workspace: Path, final_text: str, sandbox: SandboxPolicy | None
) -> list[CheckResult]:
    results = []
    for check in checks:
        if check.kind == CHECK_COMMAND:
            results.append(await run_command_check(check, workspace, sandbox))
        else:
            results.append(answer_contains_check(check, final_text))
    return results


def answer_contains_check(check: EvalCheck, final_text: str) -> CheckResult:
    passed = check.text in (final_text or "")
    detail = "" if passed else f"最终回答里没有「{check.text}」"
    return CheckResult(description=check.describe(), passed=passed, detail=detail)


async def run_command_check(check: EvalCheck, workspace: Path, sandbox: SandboxPolicy | None) -> CheckResult:
    """在副本里用 bash 跑命令，退出码为 0 算过。超时、找不到 bash 都算没过，原因写进 detail。"""
    description = check.describe()
    try:
        prepared = wrap_command(check.command, sandbox)
    except BashUnavailable as error:
        return CheckResult(description=description, passed=False, detail=str(error))
    process = await asyncio.create_subprocess_exec(
        *prepared.command_line,
        cwd=workspace,
        env={**os.environ, **prepared.environment},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # 单独的进程组：超时时连同它拉起的子进程一起结束
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=check.timeout_seconds)
    except asyncio.TimeoutError:
        await terminate_process_tree(process)
        detail = f"超过 {check.timeout_seconds} 秒没结束，已终止"
        return CheckResult(description=description, passed=False, detail=detail)
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
    if process.returncode == 0:
        return CheckResult(description=description, passed=True)
    text = output.decode("utf-8", "replace").strip()
    tail = text[-CHECK_OUTPUT_TAIL_CHARS:]
    detail = f"退出码 {process.returncode}" + (f"，输出的最后一段：\n{tail}" if tail else "")
    return CheckResult(description=description, passed=False, detail=detail)
