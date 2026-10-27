"""本机的 token 用量记录：每调用一次模型记一行 JSONL（~/.pocketagent/usage.jsonl），设置页的「用量」从这里统计。

除了输入 / 输出，还记缓存命中（cached_tokens）、思考 token、接口报的费用。
接口没返回 usage 时按字符数估算，记录里标 estimated。
读的时候按文件偏移增量读，统计页、右侧栏频繁刷新也不会每次把整个文件重读一遍。
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pocketagent.core.tokens import estimate_text_tokens, estimate_tokens
from pocketagent.llm.base import LLMProvider, LLMResponse, StreamListener, Usage

DAILY_CHART_DAYS = 30


@dataclass
class UsageMetadata:
    """这次调用属于哪个会话、哪个模型。"""

    model: str
    model_name: str = ""
    model_id: str | None = None
    session_id: str | None = None
    workspace: str | None = None


@dataclass
class TokenCount:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    reasoning: int = 0
    cost: float = 0.0
    calls: int = 0
    estimated_calls: int = 0

    def add(self, entry: dict[str, Any]) -> None:
        self.prompt += int(entry.get("prompt_tokens", 0))
        self.completion += int(entry.get("completion_tokens", 0))
        self.cached += int(entry.get("cached_tokens") or 0)
        self.reasoning += int(entry.get("reasoning_tokens") or 0)
        self.cost += float(entry.get("cost") or 0)
        self.calls += 1
        if entry.get("estimated"):
            self.estimated_calls += 1

    def cache_hit_rate(self) -> float | None:
        """缓存命中的 token 占输入 token 的比例；没有输入时为 None。"""
        if self.prompt <= 0:
            return None
        return round(self.cached / self.prompt, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "total": self.prompt + self.completion,
            "cached": self.cached,
            "cache_hit_rate": self.cache_hit_rate(),
            "reasoning": self.reasoning,
            "cost": round(self.cost, 6),
            "calls": self.calls,
            "estimated_calls": self.estimated_calls,
        }


@dataclass
class _ReadState:
    """一个用量文件已经读到哪了。同一个文件可能有多个 UsageLog 在写，读的时候都从这里接着读。"""

    offset: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)


_READ_STATES: dict[Path, _ReadState] = {}


class UsageLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, metadata: UsageMetadata, usage: Usage, estimated: bool) -> None:
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "session_id": metadata.session_id,
            "workspace": metadata.workspace,
            "model_id": metadata.model_id,
            "model": metadata.model,
            "model_name": metadata.model_name or metadata.model,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "cached_tokens": usage.cached_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cost": usage.cost,
            "estimated": estimated,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        """全部记录。只读上次读完之后新追加的部分；文件被截短或删掉就从头读。"""
        if not self.path.is_file():
            _READ_STATES.pop(self.path, None)
            return []
        state = _READ_STATES.setdefault(self.path, _ReadState())
        size = self.path.stat().st_size
        if size < state.offset:
            state = _READ_STATES[self.path] = _ReadState()
        if size > state.offset:
            with self.path.open("rb") as log_file:
                log_file.seek(state.offset)
                new_bytes = log_file.read()
            # 只认完整的行，最后半行（别人正在写）留到下次
            complete_length = new_bytes.rfind(b"\n") + 1
            for line in new_bytes[:complete_length].decode("utf-8", errors="replace").splitlines():
                try:
                    state.entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            state.offset += complete_length
        return state.entries

    def session_total(self, session_id: str) -> dict[str, int]:
        count = TokenCount()
        for entry in self.entries():
            if entry.get("session_id") == session_id:
                count.add(entry)
        return count.to_dict()

    def summarize(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        periods = {
            "今天": today,
            "近 7 天": today - timedelta(days=6),
            "近 30 天": today - timedelta(days=DAILY_CHART_DAYS - 1),
        }
        period_counts = {label: TokenCount() for label in periods}
        all_time = TokenCount()
        by_model: dict[str, TokenCount] = defaultdict(TokenCount)
        by_day: dict[str, TokenCount] = defaultdict(TokenCount)
        for entry in self.entries():
            entry_day = _entry_day(entry, today)
            all_time.add(entry)
            by_model[entry.get("model_name") or entry.get("model") or "未知"].add(entry)
            by_day[entry_day.isoformat()].add(entry)
            for label, start_day in periods.items():
                if start_day <= entry_day <= today:
                    period_counts[label].add(entry)
        daily = []
        for offset in range(DAILY_CHART_DAYS - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            daily.append({"date": day, **by_day.get(day, TokenCount()).to_dict()})
        models = [{"model": name, **count.to_dict()} for name, count in by_model.items()]
        models.sort(key=lambda item: item["total"], reverse=True)
        return {
            "periods": [{"label": label, **count.to_dict()} for label, count in period_counts.items()]
            + [{"label": "全部", **all_time.to_dict()}],
            "by_model": models,
            "daily": daily,
        }


def _entry_day(entry: dict[str, Any], fallback: date) -> date:
    try:
        return date.fromisoformat(str(entry.get("time", ""))[:10])
    except ValueError:
        return fallback


def _estimate_completion_tokens(response: LLMResponse) -> int:
    total = estimate_text_tokens(response.text) + estimate_text_tokens(response.reasoning)
    for call in response.tool_calls:
        total += estimate_text_tokens(call.name) + estimate_text_tokens(call.raw_arguments)
    return total


class UsageRecordingProvider:
    """包在真正的 provider 外面，每次调用后记一笔用量。压缩、AI 审批这些调用也都会记上。"""

    def __init__(self, inner: LLMProvider, usage_log: UsageLog, metadata: UsageMetadata) -> None:
        self.inner = inner
        self.usage_log = usage_log
        self.metadata = metadata

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse:
        response = await self.inner.complete(messages, tools, listener)
        if response.usage and (response.usage.prompt_tokens or response.usage.completion_tokens):
            self.usage_log.record(self.metadata, response.usage, estimated=False)
        else:
            estimated = Usage(
                prompt_tokens=estimate_tokens(messages), completion_tokens=_estimate_completion_tokens(response)
            )
            self.usage_log.record(self.metadata, estimated, estimated=True)
        return response
