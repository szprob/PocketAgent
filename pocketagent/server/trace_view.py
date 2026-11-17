"""界面「轨迹」面板的数据：把一个任务的每一步整理成按时间排好的条目。

数据来源优先用模型调用原文（~/.pocketagent/calls/<任务 id>.jsonl，见 evaluation/call_log.py）：
轨迹和原文是同一份东西，这里只是换个好看、好读的排法——系统提示、用户的话、插进去的上下文、
模型的思考和回复、工具调用和结果都直接从每次请求 / 回复里取，导出的也是这份原文。
没开记录的老任务没有原文，退回读事件记录（~/.pocketagent/traces/<任务 id>.jsonl），payload 里 source 写明来源。

参考 DeepSeek Harness 的轨迹页：上面一条时间轴（输入 / 模型 / 工具三行色块），下面按轮列出每一步。
条目带开始 / 结束时间，时间轴的色块直接从条目算。
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pocketagent.core.compaction import SUMMARY_INSTRUCTION, SUMMARY_PREFIX
from pocketagent.evaluation.call_log import read_calls

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
    # system / user / interjection / assistant / tool / context / error / review（AI 审批）/ summary（做压缩摘要）
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


# ---- 从模型调用原文整理 ----

SUBAGENT_MARK = "# 你现在是子 agent"
REVIEW_MARK = "你是编程助手的安全审查员"
# PocketAgent 自己插进对话的提示（模式切换、日期、钩子、截断说明……）：显示成「上下文」，不算新的一轮
NOTICE_PREFIXES = ("（工作模式切换了", "（今天是 ", "（钩子补充的上下文", "（系统提示：", "钩子拦住了这句话")
# 原文的完整内容在导出里；面板上点开看的这一份放宽一些
CALL_DETAIL_LIMIT_CHARS = 20_000


@dataclass
class _Message:
    """各家格式（OpenAI chat / Anthropic messages）的消息统一成一个样子，方便按顺序排。"""

    role: str
    text: str = ""
    reasoning: str = ""
    tool_calls: list[tuple[str, str, str]] = field(default_factory=list)
    tool_call_id: str = ""
    images: int = 0

    def key(self) -> str:
        """比较「是不是同一条消息」用：不看思考（跨轮会被去掉），工具参数按 JSON 规整后比。"""
        calls = [(call_id, name, _canonical_arguments(arguments)) for call_id, name, arguments in self.tool_calls]
        return json.dumps([self.role, self.text, calls, self.tool_call_id, self.images], ensure_ascii=False)


def build_trace_view_from_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    builder = _CallsTraceBuilder()
    for call in calls:
        builder.feed(call)
    return {
        "source": "calls",
        "entries": [asdict(entry) for entry in builder.entries],
        "turns": [asdict(turn) for turn in builder.turns],
    }


def build_task_trace(calls_path: Path, trace_path: Path, system_prompt: str = "") -> dict[str, Any]:
    """有原文就用原文，没有（老任务、关了记录）就退回事件记录。"""
    if calls_path.is_file():
        calls = list(read_calls(calls_path))
        if calls:
            return build_trace_view_from_calls(calls)
    view = build_trace_view(trace_path, system_prompt)
    view["source"] = "trace"
    return view


class _CallsTraceBuilder:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []
        self.turns: list[TraceTurn] = []
        self.tools_by_call: dict[str, TraceEntry] = {}
        # 主 agent 上一次请求 + 回复的消息（用 key 比），算这一次新增了哪些
        self.shown: list[str] = []
        self.system_text: str | None = None

    @property
    def turn(self) -> int:
        return self.turns[-1].index if self.turns else 0

    def _add(self, kind: str, text: str, **fields: Any) -> TraceEntry:
        entry = TraceEntry(id=len(self.entries), kind=kind, turn=self.turn, text=text, **fields)
        self.entries.append(entry)
        return entry

    def feed(self, call: dict[str, Any]) -> None:
        messages = normalize_request(call.get("request") or {})
        purpose = _call_purpose(messages)
        if purpose == "review":
            self._review(call, messages)
        elif purpose == "summary":
            self._summary(call)
        elif purpose == "subagent":
            self._subagent(call)
        else:
            self._main(call, messages)

    def _main(self, call: dict[str, Any], messages: list[_Message]) -> None:
        keys = [message.key() for message in messages]
        common = _common_prefix(self.shown, keys)
        if self.shown and common < len(self.shown):
            # 前面的历史变了：压缩换成了摘要，或者用户「恢复到这里」删掉了后面的对话
            self._add("context", "历史换过了（压缩或恢复到之前的消息），下面是模型这一次看到的内容", start=_started(call), end=_started(call))
        for message in messages[common:]:
            self._show_input(message, call)
        response_message = self._show_response(call)
        self.shown = keys + [response_message.key()]

    def _show_input(self, message: _Message, call: dict[str, Any]) -> None:
        moment = _started(call)
        if message.role == "system":
            if message.text != self.system_text:
                title = "系统提示词" if self.system_text is None else "系统提示词（重建过）"
                self._add("system", title, detail=_clip(message.text, CALL_DETAIL_LIMIT_CHARS))
                self.system_text = message.text
        elif message.role == "tool":
            self._attach_result(message, moment)
        elif message.role == "assistant":
            # 平时这条就是上一次的回复（已经显示过）；走到这里是原文开头没有它的那次调用（比如开记录之前的历史）
            self._add("assistant", _one_line(message.text or message.reasoning or "（回复）"), detail=_clip(_assistant_detail(message), CALL_DETAIL_LIMIT_CHARS))
            for call_id, name, arguments in message.tool_calls:
                self.tools_by_call[call_id] = self._add(
                    "tool", _argument_summary(arguments), name=name, arguments=_clip(arguments, ARGUMENTS_LIMIT_CHARS)
                )
        else:
            self._show_user(message, moment)

    def _show_user(self, message: _Message, moment: float | None) -> None:
        text = message.text + (f"（图片 {message.images} 张）" if message.images else "")
        if message.text.startswith(SUMMARY_PREFIX):
            self._add("context", "压缩后的摘要（替换了之前的对话）", start=moment, end=moment, detail=_clip(message.text, CALL_DETAIL_LIMIT_CHARS))
        elif message.text.startswith(NOTICE_PREFIXES):
            self._add("context", _one_line(text), start=moment, end=moment, detail=_clip(text, CALL_DETAIL_LIMIT_CHARS))
        else:
            self.turns.append(TraceTurn(index=self.turn + 1, start=moment or 0))
            self._add("user", _one_line(text), start=moment, end=moment, detail=_clip(text, CALL_DETAIL_LIMIT_CHARS))

    def _attach_result(self, message: _Message, moment: float | None) -> None:
        entry = self.tools_by_call.get(message.tool_call_id)
        if entry is None:
            entry = self._add("tool", "", name="（结果）")
        if entry.end is None:
            # 原文里没有工具单独的耗时：从模型要求调用到下一次带着结果去调模型，中间就是执行工具的时间
            entry.end = moment
        entry.result = _clip(message.text, CALL_DETAIL_LIMIT_CHARS)

    def _show_response(self, call: dict[str, Any]) -> _Message:
        response = call.get("response") or {}
        message = _response_message(response)
        usage = response.get("usage") or {}
        reasoning_tokens = int(usage.get("reasoning_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        calls = [name for _, name, _ in message.tool_calls]
        summary = message.text or message.reasoning or (f"调用 {'、'.join(calls)}" if calls else "（空回复）")
        meta = call.get("meta") or {}
        end = call.get("time")
        self._add(
            "assistant",
            _one_line(summary),
            start=_started(call) or end,
            end=end,
            detail=_clip(_assistant_detail(message), CALL_DETAIL_LIMIT_CHARS),
            reasoning_tokens=reasoning_tokens,
            output_tokens=max(completion_tokens - reasoning_tokens, 0),
            name=str(meta.get("model_name") or meta.get("model") or ""),
        )
        for call_id, name, arguments in message.tool_calls:
            self.tools_by_call[call_id] = self._add(
                "tool", _argument_summary(arguments), start=end, name=name, arguments=_clip(arguments, ARGUMENTS_LIMIT_CHARS)
            )
        return message

    def _review(self, call: dict[str, Any], messages: list[_Message]) -> None:
        response = call.get("response") or {}
        verdict = str(response.get("text") or response.get("reasoning") or "")
        prompt = messages[-1].text if messages else ""
        self._add(
            "review",
            _one_line(verdict) or "（没有结论）",
            start=_started(call),
            end=call.get("time"),
            detail=_clip(f"〔审查请求〕\n{prompt}\n\n〔结论〕\n{verdict}", CALL_DETAIL_LIMIT_CHARS),
        )

    def _summary(self, call: dict[str, Any]) -> None:
        response = call.get("response") or {}
        self._add(
            "summary",
            "做压缩摘要",
            start=_started(call),
            end=call.get("time"),
            detail=_clip(str(response.get("text") or ""), CALL_DETAIL_LIMIT_CHARS),
        )

    def _subagent(self, call: dict[str, Any]) -> None:
        """子 agent 的每次调用挂在正在跑的那个 subagent 工具底下，当子调用显示。"""
        parent = next(
            (entry for entry in reversed(self.entries) if entry.kind == "tool" and entry.name == "subagent" and entry.end is None),
            None,
        )
        if parent is None or len(parent.children) >= MAX_CHILDREN:
            return
        message = _response_message(call.get("response") or {})
        names = "、".join(name for _, name, _ in message.tool_calls) or "回复"
        summary = message.text or message.reasoning or "、".join(_argument_summary(arguments) for _, _, arguments in message.tool_calls)
        parent.children.append(TraceChild(name=names, summary=_one_line(summary), time=float(call.get("time") or 0)))


def normalize_request(request: dict[str, Any]) -> list[_Message]:
    """请求体里的消息统一成 _Message。Anthropic 格式的 system 在顶层、工具结果在 user 的 content 块里。"""
    messages: list[_Message] = []
    system = request.get("system")
    if system:
        messages.append(_Message(role="system", text=_block_text(system)))
    for message in request.get("messages") or []:
        content = message.get("content")
        role = message.get("role", "")
        if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") in ANTHROPIC_BLOCK_TYPES for block in content):
            messages.extend(_from_anthropic(role, content))
        else:
            messages.append(_from_openai(message))
    return messages


ANTHROPIC_BLOCK_TYPES = {"tool_use", "tool_result", "thinking", "redacted_thinking", "image"}


def _from_openai(message: dict[str, Any]) -> _Message:
    content = message.get("content")
    images = sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url") if isinstance(content, list) else 0
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        calls.append((str(call.get("id") or ""), str(function.get("name") or ""), str(function.get("arguments") or "{}")))
    return _Message(
        role=str(message.get("role") or ""),
        text=_block_text(content),
        reasoning=_reasoning_text(message),
        tool_calls=calls,
        tool_call_id=str(message.get("tool_call_id") or ""),
        images=images,
    )


def _from_anthropic(role: str, blocks: list[Any]) -> list[_Message]:
    """一条 Anthropic 消息可能是几样东西：工具结果各算一条 tool，剩下的文字 / 图片 / 思考 / 工具调用合成一条。"""
    results: list[_Message] = []
    merged = _Message(role=role)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            results.append(_Message(role="tool", text=_block_text(block.get("content")), tool_call_id=str(block.get("tool_use_id") or "")))
        elif block_type == "text":
            merged.text = f"{merged.text}\n{block.get('text', '')}" if merged.text else str(block.get("text", ""))
        elif block_type == "image":
            merged.images += 1
        elif block_type == "thinking":
            merged.reasoning += str(block.get("thinking") or "")
        elif block_type == "tool_use":
            arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)
            merged.tool_calls.append((str(block.get("id") or ""), str(block.get("name") or ""), arguments))
    has_content = merged.text or merged.images or merged.reasoning or merged.tool_calls
    return results + ([merged] if has_content else [])


def _response_message(response: dict[str, Any]) -> _Message:
    calls = [
        (str(call.get("id") or ""), str(call.get("name") or ""), str(call.get("arguments") or "{}"))
        for call in response.get("tool_calls") or []
    ]
    return _Message(role="assistant", text=str(response.get("text") or ""), reasoning=str(response.get("reasoning") or ""), tool_calls=calls)


def _call_purpose(messages: list[_Message]) -> str:
    if messages and messages[-1].role == "user" and messages[-1].text == SUMMARY_INSTRUCTION:
        return "summary"
    if len(messages) == 1 and messages[0].text.startswith(REVIEW_MARK):
        return "review"
    if messages and messages[0].role == "system" and SUBAGENT_MARK in messages[0].text:
        return "subagent"
    return "main"


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(texts)
    return ""


def _reasoning_text(message: dict[str, Any]) -> str:
    text = message.get("reasoning_content")
    if text:
        return str(text)
    parts = []
    for item in message.get("reasoning_details") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or item.get("summary") or ""))
    return "".join(parts)


def _assistant_detail(message: _Message) -> str:
    parts = []
    if message.reasoning:
        parts.append(f"〔思考〕\n{message.reasoning}")
    if message.text:
        parts.append(f"〔回复〕\n{message.text}")
    if message.tool_calls:
        parts.append("〔调用〕" + "、".join(name for _, name, _ in message.tool_calls))
    return "\n\n".join(parts)


def _argument_summary(arguments: str) -> str:
    """列表那一行的工具参数：取前两个字符串值（路径、命令、搜索词），取不到就原样。"""
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return _one_line(arguments, 160)
    if isinstance(parsed, dict):
        values = [str(value) for value in parsed.values() if isinstance(value, (str, int, float)) and str(value).strip()]
        if values:
            return _one_line(" ".join(values[:2]), 160)
    return _one_line(arguments, 160)


def _canonical_arguments(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments or "{}"), ensure_ascii=False, sort_keys=True)
    except json.JSONDecodeError:
        return arguments


def _common_prefix(left: list[str], right: list[str]) -> int:
    length = 0
    for left_key, right_key in zip(left, right):
        if left_key != right_key:
            break
        length += 1
    return length


def _started(call: dict[str, Any]) -> float | None:
    started = call.get("started")
    return float(started) if started is not None else None
