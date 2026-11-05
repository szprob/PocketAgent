"""读 JSONL trace：按轮整理成摘要——用户说了什么、谁答的、走了几步、调了哪些工具、结果、用量、耗时、起始检查点。

trace 是事件总线的原样记录（见 core/trace.py），一个任务一个文件，续跑的轮次接着往后写。
进程被杀时最后一行可能只写了一半：读的时候跳过坏行并记个数，不让整份 trace 作废。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 正常说完的一轮；其他结束原因（error / denied / step_limit / interrupted ...）都算没做成
SUCCESS_STOP_REASON = "done"
# 一轮没有 turn_end：进程被杀、或者 trace 被截断了
INCOMPLETE_STOP_REASON = "incomplete"


class TraceError(RuntimeError):
    """trace 文件读不了（不存在、不是文本）。"""


@dataclass
class ToolCallSummary:
    call_id: str
    name: str
    # ok / error / denied / missing（trace 里没有结果：被中断或者截断了）
    status: str = "missing"


@dataclass
class TurnSummary:
    index: int
    user_text: str
    started_at: float
    # 这一轮最后一条记录的时间：没有 turn_end 时拿它算耗时
    last_event_at: float
    ended_at: float | None = None
    stop_reason: str = INCOMPLETE_STOP_REASON
    final_text: str = ""
    # 每一步实际用的模型（llm_response 里记的），一轮中途换了模型时会有好几个
    step_models: list[str] = field(default_factory=list)
    # turn_start 里记的模型名和模型配置（id、名称），回放时拿来找回同一个模型
    start_model: str = ""
    model_id: str | None = None
    model_name: str = ""
    # 调了几次模型（ThinkNode 进了几次），和主循环的 step_count 一个口径
    steps: int = 0
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    # 接口报了费用才有（OpenRouter 等）；一次都没报是 None，和「花了 0 元」区分开
    cost: float | None = None
    checkpoint_commit: str | None = None
    checkpoint_label: str | None = None
    attachment_count: int = 0
    interjections: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else self.last_event_at
        return round(max(0.0, end - self.started_at), 3)

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == SUCCESS_STOP_REASON

    @property
    def models(self) -> list[str]:
        """这一轮用过的模型：有逐步记录就用它，没有（加这个字段之前的 trace）就用开始时记的；都没有是空的。"""
        if self.step_models:
            return list(self.step_models)
        return [self.start_model] if self.start_model else []

    def count_tool_calls(self, status: str) -> int:
        return sum(1 for call in self.tool_calls if call.status == status)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["models"] = self.models
        data["seconds"] = self.seconds
        data["succeeded"] = self.succeeded
        return data


@dataclass
class RunTotals:
    """整份 trace 加起来：对比表里的一列。"""

    turns: int
    succeeded: bool
    stop_reasons: list[str]
    models: list[str]
    steps: int
    tool_calls: int
    tool_errors: int
    denied_calls: int
    error_messages: list[str]
    prompt_tokens: int
    completion_tokens: int
    cost: float | None
    seconds: float
    # 最后一轮的回答：多轮对话里最后那句才是最终结果
    final_text: str

    @property
    def errors(self) -> int:
        """出错次数：工具执行出错 + 这一轮报的错（接口失败等）。被拒绝的调用单独算，不在这里。"""
        return self.tool_errors + len(self.error_messages)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["errors"] = self.errors
        return data


@dataclass
class TraceSummary:
    path: Path
    session_id: str
    workspace: str | None
    turns: list[TurnSummary]
    # 解析不了的行（半截 JSON 之类）
    skipped_lines: int = 0
    # 第一句用户的话之前出现的记录：归不到哪一轮，只记个数
    orphan_records: int = 0

    def start_checkpoint(self) -> TurnSummary | None:
        """第一个拍了检查点的轮次：它的检查点就是「任务开始时」工作区的样子。"""
        for turn in self.turns:
            if turn.checkpoint_commit:
                return turn
        return None

    def totals(self) -> RunTotals:
        return RunTotals(
            turns=len(self.turns),
            succeeded=bool(self.turns) and all(turn.succeeded for turn in self.turns),
            stop_reasons=[turn.stop_reason for turn in self.turns],
            models=_unique([model for turn in self.turns for model in turn.models]),
            steps=sum(turn.steps for turn in self.turns),
            tool_calls=sum(len(turn.tool_calls) for turn in self.turns),
            tool_errors=sum(turn.count_tool_calls("error") for turn in self.turns),
            denied_calls=sum(turn.count_tool_calls("denied") for turn in self.turns),
            error_messages=[message for turn in self.turns for message in turn.error_messages],
            prompt_tokens=sum(turn.prompt_tokens for turn in self.turns),
            completion_tokens=sum(turn.completion_tokens for turn in self.turns),
            cost=_sum_costs([turn.cost for turn in self.turns]),
            seconds=round(sum(turn.seconds for turn in self.turns), 3),
            final_text=self.turns[-1].final_text if self.turns else "",
        )


def read_trace(path: Path) -> TraceSummary:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise TraceError(f"读不了 trace 文件 {path}：{error}") from error
    reader = _TraceReader(path)
    for line in lines:
        reader.feed_line(line)
    return reader.finish()


def first_line(text: str) -> str:
    """回答的第一行非空文字：对比表里一格放不下整段回答。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


class _TraceReader:
    """逐行喂记录，遇到 turn_start 开新的一轮，其余记录记到当前这一轮上。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.session_id = path.stem
        self.workspace: str | None = None
        self.turns: list[TurnSummary] = []
        self.skipped_lines = 0
        self.orphan_records = 0

    def feed_line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            self.skipped_lines += 1
            return
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            self.skipped_lines += 1
            return
        self.feed_record(record)

    def feed_record(self, record: dict[str, Any]) -> None:
        if record["type"] == "turn_start":
            self._start_turn(record)
            return
        if not self.turns:
            self.orphan_records += 1
            return
        turn = self.turns[-1]
        if isinstance(record.get("time"), (int, float)):
            turn.last_event_at = max(turn.last_event_at, float(record["time"]))
        handler = _RECORD_HANDLERS.get(record["type"])
        if handler is not None:
            handler(turn, record)

    def _start_turn(self, record: dict[str, Any]) -> None:
        started_at = _number(record.get("time"))
        if record.get("session_id"):
            self.session_id = str(record["session_id"])
        if record.get("workspace") and self.workspace is None:
            self.workspace = str(record["workspace"])
        turn = TurnSummary(
            index=len(self.turns) + 1,
            user_text=str(record.get("user_text") or ""),
            started_at=started_at,
            last_event_at=started_at,
            start_model=str(record.get("model") or ""),
            model_id=record.get("model_id") or None,
            model_name=str(record.get("model_name") or ""),
            attachment_count=len(record.get("attachments") or []),
        )
        self.turns.append(turn)

    def finish(self) -> TraceSummary:
        return TraceSummary(
            path=self.path,
            session_id=self.session_id,
            workspace=self.workspace,
            turns=self.turns,
            skipped_lines=self.skipped_lines,
            orphan_records=self.orphan_records,
        )


def _on_node_start(turn: TurnSummary, record: dict[str, Any]) -> None:
    if record.get("node") == "ThinkNode":
        turn.steps += 1


def _on_llm_response(turn: TurnSummary, record: dict[str, Any]) -> None:
    model = record.get("model")
    if model and str(model) not in turn.step_models:
        turn.step_models.append(str(model))
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return
    turn.prompt_tokens += _integer(usage.get("prompt_tokens"))
    turn.completion_tokens += _integer(usage.get("completion_tokens"))
    turn.cached_tokens += _integer(usage.get("cached_tokens"))
    turn.reasoning_tokens += _integer(usage.get("reasoning_tokens"))
    if isinstance(usage.get("cost"), (int, float)):
        turn.cost = (turn.cost or 0.0) + float(usage["cost"])


def _on_tool_call(turn: TurnSummary, record: dict[str, Any]) -> None:
    call = ToolCallSummary(call_id=str(record.get("call_id") or ""), name=str(record.get("name") or ""))
    turn.tool_calls.append(call)


def _on_tool_result(turn: TurnSummary, record: dict[str, Any]) -> None:
    if record.get("denied"):
        status = "denied"
    elif record.get("is_error"):
        status = "error"
    else:
        status = "ok"
    call_id = str(record.get("call_id") or "")
    for call in turn.tool_calls:
        if call.call_id == call_id and call.status == "missing":
            call.status = status
            return
    # 截断的 trace 可能只剩结果没有调用：照样算上
    turn.tool_calls.append(ToolCallSummary(call_id=call_id, name=str(record.get("name") or ""), status=status))


def _on_checkpoint(turn: TurnSummary, record: dict[str, Any]) -> None:
    if turn.checkpoint_commit is None and record.get("commit"):
        turn.checkpoint_commit = str(record["commit"])
        turn.checkpoint_label = str(record.get("label") or "")


def _on_error(turn: TurnSummary, record: dict[str, Any]) -> None:
    turn.error_messages.append(str(record.get("message") or ""))


def _on_interjection(turn: TurnSummary, record: dict[str, Any]) -> None:
    turn.interjections.append(str(record.get("user_text") or ""))


def _on_turn_end(turn: TurnSummary, record: dict[str, Any]) -> None:
    turn.stop_reason = str(record.get("stop_reason") or INCOMPLETE_STOP_REASON)
    turn.final_text = str(record.get("final_text") or "")
    turn.ended_at = _number(record.get("time"), turn.last_event_at)


_RECORD_HANDLERS = {
    "node_start": _on_node_start,
    "llm_response": _on_llm_response,
    "tool_call": _on_tool_call,
    "tool_result": _on_tool_result,
    "checkpoint": _on_checkpoint,
    "error": _on_error,
    "interjection_applied": _on_interjection,
    "turn_end": _on_turn_end,
}


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _integer(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _sum_costs(costs: list[float | None]) -> float | None:
    reported = [cost for cost in costs if cost is not None]
    return round(sum(reported), 6) if reported else None


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen
