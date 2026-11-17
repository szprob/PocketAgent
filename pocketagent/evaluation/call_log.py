"""模型调用原文：每次调模型发出去的请求体和收到的完整回复，原封不动留下来，以后拿去训练、排查。

和 trace 的区别：trace 记的是「发生了什么」（给界面、回放、评测用），不记每一步模型实际看到的输入——
系统提示、工具定义、插进去的提示、压缩摘要、各家格式的思考字段都没有。这里在 provider 外面截：
- 请求体：用 provider 自己拼请求的那个函数原样拼一遍（拼法是确定的，和发给接口的一字不差），
  OpenAI SDK 的 extra_body 展开到顶层，跟 SDK 真正发出去的 JSON 一样。密钥在请求头里，不会写进来。
- 回复：流式拼好的完整内容——文字、思考、工具调用（原始参数字符串）、各家的思考签名、用量、结束原因。

存法：~/.pocketagent/calls/<任务 id>.jsonl，一行一次调用，主 agent、子 agent、AI 审批、压缩的调用都在里面。
两次压缩之间历史只追加（见 docs/runtime.md「前缀缓存」），所以消息列表只存「沿用第几次调用的前 k 条 + 新增的几条」，
工具定义没变就只写「同第几次」；read_calls / export_calls 还原成每次完整的请求，一个字不差。
"""

import copy
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from pocketagent.llm.base import LLMProvider, LLMResponse, StreamListener

# 和最近这么多次调用比，挑共同前缀最长的那次当底子（主 agent、子 agent、审批的调用会交错着写进同一个文件）
RECENT_CALLS_COMPARED = 8


@dataclass
class CallMetadata:
    """这次调用是哪个任务、哪个模型配置发的。"""

    session_id: str
    model: str
    model_id: str | None = None
    model_name: str = ""
    base_url: str | None = None


class CallLog:
    """一个任务一个文件。同一个进程里同一个文件只有一个实例（见 call_log_for），编号和底子才对得上。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.next_id = _count_lines(path)
        # (编号, 每条消息的规范 JSON)：算增量用；进程重启后是空的，第一条就存完整的消息
        self.recent: list[tuple[int, list[str]]] = []
        self.last_tools: tuple[int, str] | None = None

    def record(
        self,
        request: dict[str, Any],
        response: LLMResponse | None,
        metadata: CallMetadata,
        started: float | None = None,
        error: str | None = None,
    ) -> int:
        """started：发请求的时间；time（写入时）就是收完回复的时间。轨迹的时间轴靠这两个画模型花了多久。
        调用失败（重试完还是不行）时 response 是 None、error 写原因：请求确实发出去了，原文里也要有。"""
        body = copy.deepcopy(request)
        messages = body.pop("messages", [])
        tools = body.pop("tools", None)
        call_id = self.next_id
        record: dict[str, Any] = {
            "id": call_id,
            "started": started,
            "time": time.time(),
            "meta": asdict(metadata),
            "request": body,
            "messages": self._messages_delta(call_id, messages),
            "tools": self._tools_delta(call_id, tools),
            "response": response_record(response) if response is not None else None,
        }
        if error is not None:
            record["error"] = error
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.next_id += 1
        return call_id

    def _messages_delta(self, call_id: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
        keys = [_canonical(message) for message in messages]
        base, keep = None, 0
        for recent_id, recent_keys in self.recent:
            shared = _common_prefix_length(recent_keys, keys)
            if shared > keep:
                base, keep = recent_id, shared
        self.recent = ([(call_id, keys)] + self.recent)[:RECENT_CALLS_COMPARED]
        return {"base": base, "keep": keep, "append": messages[keep:]}

    def _tools_delta(self, call_id: int, tools: Any) -> dict[str, Any]:
        if tools is None:
            return {"absent": True}
        key = _canonical(tools)
        if self.last_tools is not None and self.last_tools[1] == key:
            return {"same_as": self.last_tools[0]}
        self.last_tools = (call_id, key)
        return {"value": tools}


_LOGS: dict[Path, CallLog] = {}


def call_log_for(directory: Path, session_id: str) -> CallLog:
    path = (directory / f"{session_id}.jsonl").resolve()
    if path not in _LOGS:
        _LOGS[path] = CallLog(path)
    return _LOGS[path]


def forget_call_log(directory: Path, session_id: str) -> None:
    """删任务时删掉它的原文文件，内存里的实例也扔掉（不然编号和底子还指着已经删掉的记录）。"""
    path = (directory / f"{session_id}.jsonl").resolve()
    _LOGS.pop(path, None)
    path.unlink(missing_ok=True)


def wire_request(provider: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """provider 这次会发给接口的请求体。OpenAI 兼容的叫 _build_request、Anthropic 的叫 build_request；
    都没有（测试用的假模型）就记 PocketAgent 交给它的消息和工具。"""
    build = getattr(provider, "_build_request", None) or getattr(provider, "build_request", None)
    if build is None:
        return {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)}
    request = copy.deepcopy(build(messages, tools))
    # OpenAI SDK 把 extra_body 合进请求 JSON 的顶层发出去，这里照样展开
    extra_body = request.pop("extra_body", None) or {}
    return {**request, **extra_body}


def response_record(response: LLMResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "reasoning": response.reasoning,
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.raw_arguments,
                **({"extra_content": call.extra_content} if call.extra_content else {}),
            }
            for call in response.tool_calls
        ],
        "reasoning_details": response.reasoning_details,
        "thinking_blocks": response.thinking_blocks,
        "finish_reason": response.finish_reason,
        "usage": asdict(response.usage) if response.usage else None,
    }


class CallRecordingProvider:
    """包在真正的 provider 外面：调用成功后把请求体和回复原样写进 CallLog。开关随设置（enabled 每次现问）。
    记录出错不影响调用本身。"""

    def __init__(
        self, inner: LLMProvider, call_log: CallLog, metadata: CallMetadata, enabled: Callable[[], bool]
    ) -> None:
        self.inner = inner
        self.call_log = call_log
        self.metadata = metadata
        self.enabled = enabled

    def with_reasoning_effort(self, reasoning_effort: str | None) -> "CallRecordingProvider":
        derive = getattr(self.inner, "with_reasoning_effort", None)
        if derive is None:
            return self
        return CallRecordingProvider(derive(reasoning_effort), self.call_log, self.metadata, self.enabled)

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], listener: StreamListener
    ) -> LLMResponse:
        request = None
        if self.enabled():
            # 先拼好：provider 发请求前不会改 messages，但调用期间历史可能被别的协程追加
            try:
                request = wire_request(self.inner, messages, tools)
            except Exception:
                request = None
        started = time.time()
        try:
            response = await self.inner.complete(messages, tools, listener)
        except Exception as error:
            # 失败的调用也记：请求确实发出去了，轨迹里要看得到这一步卡在哪
            self._record(request, None, started, f"{type(error).__name__}: {error}")
            raise
        self._record(request, response, started, None)
        return response

    def _record(self, request: dict[str, Any] | None, response: LLMResponse | None, started: float, error: str | None) -> None:
        if request is None:
            return
        try:
            self.call_log.record(request, response, self.metadata, started, error)
        except OSError:
            pass


def read_calls(path: Path) -> Iterator[dict[str, Any]]:
    """按顺序读一个文件，把增量还原成完整的请求体（messages、tools 放回 request 里）。"""
    messages_by_id: dict[int, list[dict[str, Any]]] = {}
    tools_by_id: dict[int, Any] = {}
    with path.open(encoding="utf-8") as log_file:
        for line in log_file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            delta = record.get("messages") or {}
            base = delta.get("base")
            prefix = messages_by_id.get(base, [])[: delta.get("keep", 0)] if base is not None else []
            messages = prefix + list(delta.get("append") or [])
            messages_by_id[record["id"]] = messages
            request = dict(record.get("request") or {})
            request["messages"] = messages
            tools_delta = record.get("tools") or {}
            if "value" in tools_delta:
                tools_by_id[record["id"]] = tools_delta["value"]
                request["tools"] = tools_delta["value"]
            elif "same_as" in tools_delta:
                tools_by_id[record["id"]] = tools_by_id.get(tools_delta["same_as"])
                request["tools"] = tools_by_id[record["id"]]
            yield {
                "id": record["id"],
                "started": record.get("started"),
                "time": record.get("time"),
                "meta": record.get("meta"),
                "request": request,
                "response": record.get("response"),
                **({"error": record["error"]} if "error" in record else {}),
            }


def export_calls(directory: Path, output: Path, session_ids: list[str] | None = None) -> int:
    """把 calls 目录（或其中几个任务）还原成完整记录写进一个 JSONL，返回写了多少条。"""
    paths = sorted(directory.glob("*.jsonl"))
    if session_ids:
        wanted = set(session_ids)
        paths = [path for path in paths if path.stem in wanted]
    count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as export_file:
        for path in paths:
            for call in read_calls(path):
                export_file.write(json.dumps(call, ensure_ascii=False) + "\n")
                count += 1
    return count


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _common_prefix_length(left: list[str], right: list[str]) -> int:
    length = 0
    for left_item, right_item in zip(left, right):
        if left_item != right_item:
            break
        length += 1
    return length


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("rb") as log_file:
        return sum(1 for _ in log_file)
