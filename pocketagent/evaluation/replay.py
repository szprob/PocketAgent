"""trace 回放：把一份 trace 里用户说的话按顺序、无人值守地重跑一遍（可以换模型、换系统提示词），再和原来那次并排对比。

工作区从 trace 的起始检查点恢复到临时副本；检查点找不到时拒绝回放，除非用 --workspace 指定一个目录来复制。
怎么跑、为什么这样限制权限见 runner.py。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pocketagent.config import AppConfig, config_path, default_home_dir
from pocketagent.core.agent import create_provider
from pocketagent.core.events import EventHandler
from pocketagent.core.session import Session, SessionStore
from pocketagent.evaluation.report import (
    ANSWER_CELL_WIDTH,
    RunReport,
    comparison_rows,
    evals_directory,
    markdown_answer_rows,
    now_text,
    render_markdown_table,
    render_text_table,
    result_text,
    run_report_from_summary,
    write_report,
)
from pocketagent.evaluation.runner import (
    ModelChoice,
    ProviderFactory,
    RunRequest,
    resolve_models,
    run_prompts,
    sandbox_warning,
    unattended_settings,
)
from pocketagent.evaluation.trace_reader import TraceSummary, first_line, read_trace
from pocketagent.evaluation.workspace import (
    TemporaryWorkspace,
    WorkspaceError,
    WorkspaceSource,
    directory_source,
    snapshot_source,
)
from pocketagent.safety.checkpoints import CheckpointError

ORIGINAL_LABEL = "原始"
# 会话 id 拿来拼文件路径：只认生成时用的那几种字符，trace 里写了别的就当没有会话记录
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class ReplayError(RuntimeError):
    """没法回放（没有用户的话、检查点找不到又没给 --workspace）。消息直接给用户看。"""


@dataclass
class ReplayRequest:
    trace_path: Path
    # 空的就用原来的模型（配置里找得到的话），找不到用默认模型
    model_keys: list[str] = field(default_factory=list)
    system_prompt_template: str | None = None
    # 只用来写进报告：换了哪个提示词文件
    system_prompt_file: str | None = None
    # 给了就复制这个目录，不管 trace 有没有检查点
    workspace_override: Path | None = None
    keep_workspaces: bool = False


@dataclass
class ReplayResult:
    trace_path: Path
    prompts: list[str]
    workspace_description: str
    runs: list[RunReport]
    warnings: list[str]
    report_dir: Path | None = None

    def text_table(self) -> str:
        header = ["", *[run.label for run in self.runs]]
        return render_text_table(header, comparison_rows(self.runs, answer_limit=ANSWER_CELL_WIDTH))


async def replay_trace(
    request: ReplayRequest,
    provider_factory: ProviderFactory = create_provider,
    progress: Callable[[str], None] = print,
    event_handler: EventHandler | None = None,
) -> ReplayResult:
    summary = read_trace(request.trace_path)
    prompts = replay_prompts(summary)
    if not prompts:
        raise ReplayError(f"{request.trace_path} 里没有用户说的话，没什么可回放的")
    home_dir = default_home_dir()
    session_record = load_session_record(home_dir, summary.session_id)
    source = await choose_workspace_source(summary, session_record, home_dir, request.workspace_override)
    choices = resolve_models(request.model_keys or [original_model_key(summary, session_record)])
    warnings = trace_warnings(summary) + list(source.warnings) + [choice.note for choice in choices if choice.note]
    no_sandbox = sandbox_warning()
    if no_sandbox:
        warnings.append(no_sandbox)
    progress(f"回放 {request.trace_path}：{len(prompts)} 句话；工作区：{source.describe()}")
    for warning in warnings:
        progress(f"提醒：{warning}")
    runs = [original_run(summary, session_record)]
    for index, choice in enumerate(choices, start=1):
        progress(f"[{index}/{len(choices)}] {choice.label} 开始")
        run = await _replay_once(choice, prompts, source, request, provider_factory, event_handler)
        progress(f"[{index}/{len(choices)}] {choice.label}：{result_text(run)}")
        runs.append(run)
    result = ReplayResult(
        trace_path=request.trace_path,
        prompts=prompts,
        workspace_description=source.describe(),
        runs=runs,
        warnings=warnings,
    )
    result.report_dir = write_report(
        evals_directory(home_dir), replay_report_data(result, request), replay_markdown(result, request)
    )
    return result


async def _replay_once(
    choice: ModelChoice,
    prompts: list[str],
    source: WorkspaceSource,
    request: ReplayRequest,
    provider_factory: ProviderFactory,
    event_handler: EventHandler | None,
) -> RunReport:
    with TemporaryWorkspace(prefix="pocketagent-replay-", keep=request.keep_workspaces) as workspace:
        try:
            await source.materialize(workspace)
        except (CheckpointError, OSError) as error:
            raise ReplayError(f"准备工作区副本失败：{error}") from error
        outcome = await run_prompts(
            RunRequest(
                label=choice.label,
                settings=unattended_settings(choice.settings, workspace),
                prompts=prompts,
                provider_factory=provider_factory,
                system_prompt_template=request.system_prompt_template,
                event_handler=event_handler,
                source="replay",
            )
        )
    return run_report_from_summary(
        label=outcome.label,
        model=outcome.model,
        summary=outcome.summary,
        crash=outcome.crash,
        workspace=str(workspace) if request.keep_workspaces else None,
    )


def replay_prompts(summary: TraceSummary) -> list[str]:
    """要重放的话：只有附件没有文字的轮次跳过（附件不重放，空话发给模型没有意义）。"""
    return [turn.user_text for turn in summary.turns if turn.user_text.strip()]


def trace_warnings(summary: TraceSummary) -> list[str]:
    """回放和原来那次不一样的地方，先告诉用户，免得拿着对不上的结果去比。"""
    warnings = []
    if summary.skipped_lines:
        warnings.append(f"trace 里有 {summary.skipped_lines} 行坏了（可能写到一半进程就退出了），已跳过")
    for turn in summary.turns:
        if not turn.user_text.strip():
            warnings.append(f"第 {turn.index} 轮只有附件没有文字，跳过")
        elif turn.attachment_count:
            warnings.append(f"第 {turn.index} 轮带了 {turn.attachment_count} 个附件，回放只重放文字")
        if turn.interjections:
            warnings.append(f"第 {turn.index} 轮中途插了 {len(turn.interjections)} 句话，回放不重放插话")
    return warnings


def load_session_record(home_dir: Path, session_id: str) -> Session | None:
    """原任务的会话记录：老 trace 没记的模型、工作区、嵌套仓库的检查点都在这里。任务删了就没有。"""
    if not _SESSION_ID_PATTERN.fullmatch(session_id or ""):
        return None
    try:
        return SessionStore(home_dir / "sessions").load(session_id, recover=False)
    except (OSError, ValueError, TypeError):
        return None


async def choose_workspace_source(
    summary: TraceSummary,
    session_record: Session | None,
    home_dir: Path,
    workspace_override: Path | None,
) -> WorkspaceSource:
    """显式给了 --workspace 就用它；否则从起始检查点恢复，恢复不了就拒绝——不能拿用户现在的工作区凑合。"""
    if workspace_override is not None:
        try:
            return directory_source(workspace_override)
        except WorkspaceError as error:
            raise ReplayError(str(error)) from error
    start_turn = summary.start_checkpoint()
    hint = "用 --workspace DIR 指定一个目录，回放会在它的临时副本里跑"
    if start_turn is None or not start_turn.checkpoint_commit:
        raise ReplayError(f"trace 里没有起始检查点（可能是检查点没开或者拍失败了），没法恢复当时的工作区；{hint}")
    commit = start_turn.checkpoint_commit
    workspace = summary.workspace or (session_record.workspace if session_record else None)
    source = await snapshot_source(
        home_dir / "shadow", commit, workspace, nested_commits=_nested_commits(session_record, commit)
    )
    if source is None:
        raise ReplayError(
            f"影子仓库里找不到起始检查点 {commit[:12]}（任务删掉后快照会被清理，或者没装 git）；{hint}"
        )
    return source


def _nested_commits(session_record: Session | None, commit: str) -> dict[str, str]:
    if session_record is None:
        return {}
    for checkpoint in session_record.checkpoints:
        if checkpoint.get("commit") == commit:
            return dict(checkpoint.get("nested") or {})
    return {}


def original_model_key(summary: TraceSummary, session_record: Session | None) -> str | None:
    """回放默认用原来的模型：trace 里记的模型配置 id / 名称，老 trace 看会话记录；配置里都找不到就用模型名直接试。"""
    app_config = AppConfig.load(config_path(default_home_dir()))
    first_turn = summary.turns[0] if summary.turns else None
    candidates: list[str | None] = []
    if first_turn is not None:
        candidates += [first_turn.model_id, first_turn.model_name, first_turn.start_model]
    if session_record is not None:
        candidates.append(session_record.model_profile)
    for candidate in candidates:
        if candidate and app_config.find_model(candidate) is not None:
            return candidate
    return first_turn.start_model if first_turn and first_turn.start_model else None


def original_run(summary: TraceSummary, session_record: Session | None) -> RunReport:
    totals = summary.totals()
    model = "、".join(totals.models)
    if not model and session_record is not None and session_record.model_profile:
        profile = AppConfig.load(config_path(default_home_dir())).find_model(session_record.model_profile)
        model = profile.name if profile else ""
    return run_report_from_summary(ORIGINAL_LABEL, model, summary)


# ---- 报告 ----


def replay_report_data(result: ReplayResult, request: ReplayRequest) -> dict:
    return {
        "kind": "replay",
        "created_at": now_text(),
        "trace": str(result.trace_path),
        "workspace": result.workspace_description,
        "system_prompt_file": request.system_prompt_file,
        "prompts": result.prompts,
        "warnings": result.warnings,
        "runs": [run.to_dict() for run in result.runs],
    }


def replay_markdown(result: ReplayResult, request: ReplayRequest) -> str:
    header = ["", *[run.label for run in result.runs]]
    lines = [
        "# 回放报告",
        "",
        f"- trace：`{result.trace_path}`",
        f"- 工作区：{result.workspace_description}",
        f"- 系统提示词：{f'`{request.system_prompt_file}`' if request.system_prompt_file else '默认'}",
        f"- 时间：{now_text()}",
        "",
        "## 用户说的话",
        "",
    ]
    for index, prompt in enumerate(result.prompts, start=1):
        lines.append(f"{index}. {first_line(prompt)}")
    lines += ["", "## 对比", "", render_markdown_table(header, markdown_answer_rows(result.runs)), ""]
    if result.warnings:
        lines += ["## 提醒", ""] + [f"- {warning}" for warning in result.warnings] + [""]
    lines += ["## 各次运行", ""]
    for run in result.runs:
        lines += _run_details(run)
    return "\n".join(lines)


def _run_details(run: RunReport) -> list[str]:
    lines = [f"### {run.label}", "", f"- 模型：{run.model or '未记录'}"]
    if run.trace_path:
        lines.append(f"- trace：`{run.trace_path}`")
    if run.workspace:
        lines.append(f"- 留下的工作区：`{run.workspace}`")
    if run.crash:
        lines.append(f"- 没跑起来：{run.crash}")
    for turn in run.turns:
        answer = first_line(turn.get("final_text", "")) or "（没有回答）"
        lines.append(f"- 第 {turn['index']} 轮：{turn['stop_reason']}，{turn['steps']} 步 —— {answer}")
    lines.append("")
    return lines
