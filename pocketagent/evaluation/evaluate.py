"""评测：拿一组用例（提示词 + 样例目录 + 检查）逐个模型跑一遍，记下每条检查过没过。报告格式和回放一样。

每个用例 × 每个模型各用一份样例目录的新副本，跑完在副本里跑检查，再删掉副本。
通过 = 这一轮正常说完（done）而且每条检查都过了。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pocketagent.config import default_home_dir
from pocketagent.core.agent import create_provider
from pocketagent.core.events import EventHandler
from pocketagent.evaluation.cases import EvalCase, load_cases, run_checks
from pocketagent.evaluation.report import (
    ANSWER_CELL_WIDTH,
    CheckResult,
    RunReport,
    TableRow,
    check_rows,
    comparison_rows,
    evals_directory,
    markdown_answer_rows,
    now_text,
    render_markdown_table,
    render_text_table,
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
from pocketagent.evaluation.trace_reader import first_line
from pocketagent.evaluation.workspace import (
    EmptySource,
    TemporaryWorkspace,
    WorkspaceError,
    WorkspaceSource,
    directory_source,
)


@dataclass
class EvalRequest:
    cases_path: Path
    # 空的就用配置里的默认模型
    model_keys: list[str] = field(default_factory=list)
    system_prompt_template: str | None = None
    system_prompt_file: str | None = None
    keep_workspaces: bool = False


@dataclass
class CaseResult:
    case: EvalCase
    # 每个模型一列，顺序和 EvalResult.model_labels 一样
    runs: list[RunReport]


@dataclass
class EvalResult:
    cases_path: Path
    model_labels: list[str]
    case_results: list[CaseResult]
    warnings: list[str]
    report_dir: Path | None = None

    @property
    def all_passed(self) -> bool:
        return all(run.passed for case_result in self.case_results for run in case_result.runs)

    def passed_count(self, model_index: int) -> int:
        return sum(1 for case_result in self.case_results if case_result.runs[model_index].passed)

    def summary_rows(self) -> list[TableRow]:
        """汇总表：每个用例一行、每个模型一列，最后一行是通过数。"""
        rows = []
        for case_result in self.case_results:
            cells = ["通过" if run.passed else "未通过" for run in case_result.runs]
            rows.append(TableRow(case_result.case.name, cells))
        total = len(self.case_results)
        totals = [f"{self.passed_count(index)}/{total}" for index in range(len(self.model_labels))]
        rows.append(TableRow("合计", totals))
        return rows

    def text_tables(self) -> str:
        sections = []
        for case_result in self.case_results:
            header = [f"用例：{case_result.case.name}", *self.model_labels]
            rows = comparison_rows(case_result.runs, answer_limit=ANSWER_CELL_WIDTH) + check_rows(case_result.runs)
            sections.append(render_text_table(header, rows))
        sections.append(render_text_table(["汇总", *self.model_labels], self.summary_rows()))
        return "\n\n".join(sections)


async def evaluate_cases(
    request: EvalRequest,
    provider_factory: ProviderFactory = create_provider,
    progress: Callable[[str], None] = print,
    event_handler: EventHandler | None = None,
) -> EvalResult:
    cases = load_cases(request.cases_path)
    choices = resolve_models(request.model_keys)
    warnings = [choice.note for choice in choices if choice.note]
    no_sandbox = sandbox_warning()
    if no_sandbox:
        warnings.append(no_sandbox + "；检查命令也只能直接在临时副本里执行")
    progress(f"评测 {request.cases_path}：{len(cases)} 个用例 × {len(choices)} 个模型")
    for warning in warnings:
        progress(f"提醒：{warning}")
    case_results = []
    for case in cases:
        source = directory_source(case.fixture, label="复制样例目录") if case.fixture else EmptySource()
        runs = []
        for choice in choices:
            run = await _evaluate_once(case, choice, source, request, provider_factory, event_handler)
            progress(f"{case.name} / {choice.label}：{'通过' if run.passed else '未通过'}")
            runs.append(run)
        case_results.append(CaseResult(case=case, runs=runs))
    result = EvalResult(
        cases_path=request.cases_path,
        model_labels=[choice.label for choice in choices],
        case_results=case_results,
        warnings=warnings,
    )
    result.report_dir = write_report(
        evals_directory(default_home_dir()), eval_report_data(result, request), eval_markdown(result, request)
    )
    return result


async def _evaluate_once(
    case: EvalCase,
    choice: ModelChoice,
    source: WorkspaceSource,
    request: EvalRequest,
    provider_factory: ProviderFactory,
    event_handler: EventHandler | None,
) -> RunReport:
    with TemporaryWorkspace(prefix="pocketagent-eval-", keep=request.keep_workspaces) as workspace:
        try:
            await source.materialize(workspace)
        except OSError as error:
            raise WorkspaceError(f"复制「{case.name}」的样例目录失败：{error}") from error
        outcome = await run_prompts(
            RunRequest(
                label=choice.label,
                settings=unattended_settings(choice.settings, workspace),
                prompts=case.prompts,
                provider_factory=provider_factory,
                system_prompt_template=request.system_prompt_template,
                event_handler=event_handler,
                source="eval",
            )
        )
        run = run_report_from_summary(
            label=outcome.label,
            model=outcome.model,
            summary=outcome.summary,
            crash=outcome.crash,
            workspace=str(workspace) if request.keep_workspaces else None,
        )
        if outcome.crash is None:
            run.checks = await run_checks(case.checks, workspace, outcome.final_text, outcome.sandbox)
        else:
            run.checks = [CheckResult(check.describe(), passed=False, detail="没跑起来") for check in case.checks]
    return run


# ---- 报告 ----


def eval_report_data(result: EvalResult, request: EvalRequest) -> dict:
    total = len(result.case_results)
    return {
        "kind": "eval",
        "created_at": now_text(),
        "cases": str(result.cases_path),
        "models": result.model_labels,
        "system_prompt_file": request.system_prompt_file,
        "warnings": result.warnings,
        "all_passed": result.all_passed,
        "summary": [
            {"model": label, "passed": result.passed_count(index), "total": total}
            for index, label in enumerate(result.model_labels)
        ],
        "results": [
            {
                "case": case_result.case.name,
                "prompts": case_result.case.prompts,
                "fixture": str(case_result.case.fixture) if case_result.case.fixture else None,
                "runs": [run.to_dict() for run in case_result.runs],
            }
            for case_result in result.case_results
        ],
    }


def eval_markdown(result: EvalResult, request: EvalRequest) -> str:
    lines = [
        "# 评测报告",
        "",
        f"- 用例：`{result.cases_path}`（{len(result.case_results)} 个）",
        f"- 模型：{'、'.join(result.model_labels)}",
        f"- 系统提示词：{f'`{request.system_prompt_file}`' if request.system_prompt_file else '默认'}",
        f"- 时间：{now_text()}",
        "",
        "## 汇总",
        "",
        render_markdown_table(["用例", *result.model_labels], result.summary_rows()),
        "",
    ]
    if result.warnings:
        lines += ["## 提醒", ""] + [f"- {warning}" for warning in result.warnings] + [""]
    for case_result in result.case_results:
        lines += _case_section(case_result, result.model_labels)
    return "\n".join(lines)


def _case_section(case_result: CaseResult, model_labels: list[str]) -> list[str]:
    case = case_result.case
    lines = [f"## 用例：{case.name}", ""]
    for index, prompt in enumerate(case.prompts, start=1):
        lines.append(f"{index}. {first_line(prompt)}")
    rows = markdown_answer_rows(case_result.runs) + check_rows(case_result.runs)
    lines += ["", render_markdown_table(["", *model_labels], rows), ""]
    for run in case_result.runs:
        failed = [check for check in run.checks if not check.passed]
        if run.trace_path:
            lines.append(f"- {run.label} 的 trace：`{run.trace_path}`")
        if run.workspace:
            lines.append(f"- {run.label} 留下的工作区：`{run.workspace}`")
        for check in failed:
            # 第一行是原因（退出码、超时），后面是命令输出：放进代码块，缩进和空行才不会被 Markdown 吃掉
            reason, _, output = check.detail.partition("\n")
            lines.append(f"- {run.label} 没过「{check.description}」：{reason}")
            if output.strip():
                lines += ["", "  ```text", *[f"  {line}" for line in output.splitlines()], "  ```", ""]
    lines.append("")
    return lines
