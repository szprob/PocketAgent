"""对比表和报告文件：终端里打一张并排的表（每次运行一列），同时把 JSON + Markdown 写到 ~/.pocketagent/evals/<时间>/。

报告不进仓库目录：和会话、trace 一样是运行产物。
"""

import json
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.evaluation.trace_reader import RunTotals, TraceSummary, first_line

# 终端表格里「最终回答」一格最多占多宽（中文按两格算）
ANSWER_CELL_WIDTH = 36
# Markdown 里不怕宽，但一行太长也没法看
MARKDOWN_ANSWER_LIMIT = 200


@dataclass
class CheckResult:
    description: str
    passed: bool
    detail: str = ""


@dataclass
class RunReport:
    """对比表里的一列：原始 trace，或者某个模型的一次运行。"""

    label: str
    model: str
    totals: RunTotals | None
    trace_path: str | None = None
    # --keep-workspaces 时留下的副本
    workspace: str | None = None
    # 没跑起来的原因
    crash: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    # 评测才有：每条检查的结果
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.crash is None and self.totals is not None and self.totals.succeeded

    @property
    def passed(self) -> bool:
        """评测用：这一轮正常说完，而且每条检查都过了。"""
        return self.succeeded and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "model": self.model,
            "succeeded": self.succeeded,
            "passed": self.passed if self.checks else None,
            "totals": self.totals.to_dict() if self.totals else None,
            "trace": self.trace_path,
            "workspace": self.workspace,
            "crash": self.crash,
            "checks": [asdict(check) for check in self.checks],
            "turns": self.turns,
        }


def run_report_from_summary(
    label: str,
    model: str,
    summary: TraceSummary | None,
    crash: str | None = None,
    workspace: str | None = None,
) -> RunReport:
    """model 只是兜底：trace 里记了实际用的模型就用记下来的（一次运行中途换过模型也看得出来）。"""
    totals = summary.totals() if summary else None
    used_models = "、".join(totals.models) if totals and totals.models else ""
    return RunReport(
        label=label,
        model=used_models or model,
        totals=totals,
        trace_path=str(summary.path) if summary else None,
        workspace=workspace,
        crash=crash or (None if summary else "没有写出 trace"),
        turns=[turn.to_dict() for turn in summary.turns] if summary else [],
    )


@dataclass
class TableRow:
    label: str
    cells: list[str]


def result_text(run: RunReport) -> str:
    if run.crash:
        return "没跑起来"
    if run.totals is None:
        return "-"
    if run.totals.succeeded:
        return "成功"
    failed_reasons = [reason for reason in run.totals.stop_reasons if reason != "done"]
    return f"失败（{'、'.join(dict.fromkeys(failed_reasons)) or '没有轮次'}）"


@dataclass
class FormattedTotals:
    """一列里各个指标排好版的文字。"""

    steps: str = "-"
    tool_calls: str = "-"
    errors: str = "-"
    denied: str = "-"
    tokens: str = "-"
    cost: str = "-"
    seconds: str = "-"


def format_totals(totals: RunTotals | None) -> FormattedTotals:
    if totals is None:
        return FormattedTotals()
    return FormattedTotals(
        steps=str(totals.steps),
        tool_calls=str(totals.tool_calls),
        errors=str(totals.errors),
        denied=str(totals.denied_calls),
        tokens=f"{totals.prompt_tokens:,} / {totals.completion_tokens:,}",
        # 接口没报费用就不估：没有各家的价目表，估出来的数会误导
        cost="-" if totals.cost is None else f"${totals.cost:.4f}",
        seconds=f"{totals.seconds:.1f}",
    )


def comparison_rows(runs: list[RunReport], answer_limit: int | None = None) -> list[TableRow]:
    """对比表的行：每个指标一行，每次运行一列。answer_limit 是「最终回答」一格的显示宽度上限。"""
    formatted = [format_totals(run.totals) for run in runs]
    return [
        TableRow("模型", [run.model or "未记录" for run in runs]),
        TableRow("结果", [result_text(run) for run in runs]),
        TableRow("步数", [item.steps for item in formatted]),
        TableRow("工具调用", [item.tool_calls for item in formatted]),
        TableRow("出错", [item.errors for item in formatted]),
        TableRow("被拒绝", [item.denied for item in formatted]),
        TableRow("token（入 / 出）", [item.tokens for item in formatted]),
        TableRow("费用", [item.cost for item in formatted]),
        TableRow("耗时（秒）", [item.seconds for item in formatted]),
        TableRow("最终回答", [_answer_cell(run, answer_limit) for run in runs]),
    ]


def check_rows(runs: list[RunReport]) -> list[TableRow]:
    """评测的检查结果：每条检查一行。同一个用例里每次运行的检查是同一组，按第一列的描述取行名。"""
    descriptions = next((run.checks for run in runs if run.checks), [])
    rows = []
    for index, check in enumerate(descriptions):
        cells = []
        for run in runs:
            if index < len(run.checks):
                cells.append("通过" if run.checks[index].passed else "失败")
            else:
                cells.append("-")
        rows.append(TableRow(f"检查：{check.description}", cells))
    rows.append(TableRow("用例", ["通过" if run.passed else "未通过" for run in runs]))
    return rows


def _answer_cell(run: RunReport, limit: int | None) -> str:
    if run.crash:
        text = run.crash
    elif run.totals is None:
        text = ""
    else:
        text = first_line(run.totals.final_text) or (run.totals.error_messages[-1] if run.totals.error_messages else "")
    if limit is None:
        return text
    return truncate_to_width(text, limit)


# ---- 文字表格 ----


def display_width(text: str) -> int:
    """终端里的显示宽度：中日韩全角字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(character) in ("W", "F") else 1 for character in text)


def truncate_to_width(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    result = ""
    for character in text:
        if display_width(result + character) > width - 1:
            break
        result += character
    return result + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * (width - display_width(text))


def render_text_table(header: list[str], rows: list[TableRow]) -> str:
    """header 第一格是左上角的标题，后面每格是一列的名字。"""
    table = [header] + [[row.label, *row.cells] for row in rows]
    widths = [max(display_width(line[column]) for line in table) for column in range(len(header))]
    lines = []
    for line_index, line in enumerate(table):
        lines.append("  ".join(_pad(cell, widths[column]) for column, cell in enumerate(line)).rstrip())
        if line_index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def render_markdown_table(header: list[str], rows: list[TableRow]) -> str:
    lines = [_markdown_line(header), "|" + "---|" * len(header)]
    for row in rows:
        lines.append(_markdown_line([row.label, *row.cells]))
    return "\n".join(lines)


def _markdown_line(cells: list[str]) -> str:
    escaped = [cell.replace("|", "\\|").replace("\n", " ") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def markdown_answer_rows(runs: list[RunReport]) -> list[TableRow]:
    """Markdown 里的对比表：最终回答给长一点。"""
    return comparison_rows(runs, answer_limit=MARKDOWN_ANSWER_LIMIT)


# ---- 写报告 ----


def evals_directory(home_dir: Path) -> Path:
    return home_dir / "evals"


def write_report(evals_dir: Path, data: dict[str, Any], markdown: str) -> Path:
    """写到 evals_dir/<时间>/report.json 和 report.md；同一秒跑了两次就在目录名后面加序号。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = evals_dir / stamp
    suffix = 2
    while directory.exists():
        directory = evals_dir / f"{stamp}-{suffix}"
        suffix += 1
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "report.md").write_text(markdown, encoding="utf-8")
    return directory


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")
