"""界面「轨迹」面板的数据：把一个任务的 trace（~/.pocketagent/traces/<任务 id>.jsonl）整理成按时间排好的条目。

参考 DeepSeek Harness 的轨迹页：上面一条时间轴（输入 / 模型 / 工具三行色块），下面按轮列出每一步——
系统提示、用户的话、模型的思考和回复、工具调用和结果、子 agent 的调用、压缩、出错。
条目带开始 / 结束时间，时间轴的色块直接从条目算，不另外存。trace 里每条事件已经带时间，读一遍就够了。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 每条条目的详细内容最多带这么多字，长任务的轨迹也不至于一次传好几兆
DETAIL_LIMIT_CHARS = 4000
ARGUMENTS_LIMIT_CHARS = 1200
MAX_CHILDREN = 200
NOTICE_TEXT = {
    "step_limit": "到了单轮步数上限，这一轮停下了",
}


@dataclass
class TraceChild:
    """子 agent 里的一次调用（subagent 工具底下的子调用）。"""

    name: str
    summary: str
    time: float


@dataclass
class TraceEntry:
    id: int
    # system / user / interjection / assistant / tool / context / error
    kind: str
    # 第几轮；系统提示是 0
    turn: int
    # 列表里那一行显示的文字
    text: str
    start: float | None = None
    end: float | None = None
    # 点开之后看的完整内容
    detail: str = ""
    # 模型回复：思考 / 输出 token，时间轴上的色块按比例分成浅色（思考）和深色（输出）
    reasoning_tokens: int = 0
    output_tokens: int = 0
    # 工具调用
    name: str = ""
    arguments: str = ""
    result: str = ""
    is_error: bool = False
    denied: bool = False
    children: list[TraceChild] = field(default_factory=list)


@dataclass
class TraceTurn:
    index: int
    start: float
    end: float | None = None
    stop_reason: str = ""


class _TraceBuilder:
    def __init__(self, system_prompt: str) -> None:
        self.entries: list[TraceEntry] = []
        self.turns: list[TraceTurn] = []
        self.tools_by_call: dict[str, TraceEntry] = {}
        # 最近一次开始调模型的时间（ThinkNode 的 node_start）；llm_response 用它当开始时间
        self.think_started: float | None = None
        if system_prompt:
            self._add("system", "初始系统提示词", detail=_clip(system_prompt, DETAIL_LIMIT_CHARS))

    @property
    def turn(self) -> int:
        return self.turns[-1].index if self.turns else 0

    def _add(self, kind: str, text: str, **fields: Any) -> TraceEntry:
        entry = TraceEntry(id=len(self.entries), kind=kind, turn=self.turn, text=text, **fields)
        self.entries.append(entry)
        return entry

    def feed(self, record: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{record.get('type')}", None)
        if handler is not None:
            handler(record, float(record.get("time") or 0))

    def _on_turn_start(self, record: dict[str, Any], time: float) -> None:
        self.turns.append(TraceTurn(index=self.turn + 1, start=time))
        text = str(record.get("user_text") or "")
        attachments = record.get("attachments") or []
        if attachments:
            text = f"{text}（附件 {len(attachments)} 个）" if text else f"（附件 {len(attachments)} 个）"
        self._add("user", _one_line(text), start=time, end=time, detail=_clip(text, DETAIL_LIMIT_CHARS))

    def _on_interjection_applied(self, record: dict[str, Any], time: float) -> None:
        text = str(record.get("user_text") or "（附件）")
        self._add("interjection", _one_line(text), start=time, end=time, detail=_clip(text, DETAIL_LIMIT_CHARS))

    def _on_node_start(self, record: dict[str, Any], time: float) -> None:
        if record.get("node") == "ThinkNode":
            self.think_started = time

    def _on_llm_response(self, record: dict[str, Any], time: float) -> None:
        text = str(record.get("text") or "")
        reasoning = str(record.get("reasoning") or "")
        usage = record.get("usage") or {}
        reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        detail_parts = []
        if reasoning:
            detail_parts.append(f"〔思考〕\n{reasoning}")
        if text:
            detail_parts.append(f"〔回复〕\n{text}")
        calls = [str(call.get("name") or "") for call in record.get("tool_calls") or []]
        if calls:
            detail_parts.append(f"〔调用〕{'、'.join(calls)}")
        summary = text or reasoning or (f"调用 {'、'.join(calls)}" if calls else "（空回复）")
        self._add(
            "assistant",
            _one_line(summary),
            start=self.think_started if self.think_started is not None else time,
            end=time,
            detail=_clip("\n\n".join(detail_parts), DETAIL_LIMIT_CHARS),
            reasoning_tokens=reasoning_tokens,
            output_tokens=max(completion_tokens - reasoning_tokens, 0),
            name=str(record.get("model") or ""),
        )
        self.think_started = None

    def _on_tool_call(self, record: dict[str, Any], time: float) -> None:
        arguments = record.get("arguments")
        arguments_text = json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""
        name = str(record.get("name") or "")
        entry = self._add(
            "tool",
            _one_line(str(record.get("summary") or "")),
            start=time,
            name=name,
            arguments=_clip(arguments_text, ARGUMENTS_LIMIT_CHARS),
        )
        self.tools_by_call[str(record.get("call_id") or "")] = entry

    def _on_tool_result(self, record: dict[str, Any], time: float) -> None:
        entry = self.tools_by_call.get(str(record.get("call_id") or ""))
        if entry is None:
            return
        entry.end = time
        entry.result = _clip(str(record.get("content") or ""), DETAIL_LIMIT_CHARS)
        entry.is_error = bool(record.get("is_error"))
        entry.denied = bool(record.get("denied"))

    def _on_subagent_progress(self, record: dict[str, Any], time: float) -> None:
        entry = self.tools_by_call.get(str(record.get("call_id") or ""))
        if entry is None or len(entry.children) >= MAX_CHILDREN:
            return
        entry.children.append(
            TraceChild(name=str(record.get("name") or ""), summary=_one_line(str(record.get("summary") or "")), time=time)
        )

    def _on_compaction(self, record: dict[str, Any], time: float) -> None:
        before, after = record.get("tokens_before"), record.get("tokens_after")
        where = "后台" if record.get("background") else "当场"
        self._add("context", f"压缩上下文（{where}）：约 {before} → {after} token", start=time, end=time)

    def _on_agent_mode(self, record: dict[str, Any], time: float) -> None:
        self._add("context", f"切换工作模式：{record.get('mode')}", start=time, end=time)

    def _on_llm_retry(self, record: dict[str, Any], time: float) -> None:
        error = str(record.get("error") or "")
        text = f"第 {record.get('attempt')} 次重试（等 {record.get('wait_seconds')} 秒）：{_one_line(error)}"
        self._add("error", text, start=time, end=time, detail=_clip(error, DETAIL_LIMIT_CHARS))

    def _on_error(self, record: dict[str, Any], time: float) -> None:
        message = str(record.get("message") or "")
        self._add("error", _one_line(message), start=time, end=time, detail=_clip(message, DETAIL_LIMIT_CHARS))

    def _on_step_limit(self, record: dict[str, Any], time: float) -> None:
        self._add("error", NOTICE_TEXT["step_limit"], start=time, end=time)

    def _on_turn_end(self, record: dict[str, Any], time: float) -> None:
        if self.turns:
            self.turns[-1].end = time
            self.turns[-1].stop_reason = str(record.get("stop_reason") or "")


def build_trace_view(trace_path: Path, system_prompt: str = "") -> dict[str, Any]:
    """读整个 trace 文件，返回 {entries, turns}。文件不存在（还没跑过）就只有系统提示。坏行跳过。"""
    builder = _TraceBuilder(system_prompt)
    if trace_path.is_file():
        with trace_path.open(encoding="utf-8", errors="replace") as trace_file:
            for line in trace_file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    builder.feed(record)
    return {
        "entries": [asdict(entry) for entry in builder.entries],
        "turns": [asdict(turn) for turn in builder.turns],
    }


def _one_line(text: str, limit: int = 300) -> str:
    """列表那一行：多行压成一行，太长截掉（完整内容在 detail 里）。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（后面还有 {len(text) - limit} 字）"
