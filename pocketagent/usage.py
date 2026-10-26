"""本机的 token 用量记录：每调用一次模型记一行 JSONL（~/.pocketagent/usage.jsonl），设置页的「用量」从这里统计。

接口没返回 usage 时按字符数估算，记录里标 estimated。
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pocketagent.compaction import estimate_tokens
from pocketagent.llm.base import LLMProvider, LLMResponse, StreamListener

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
    calls: int = 0
    estimated_calls: int = 0

    def add(self, entry: dict[str, Any]) -> None:
        self.prompt += int(entry.get("prompt_tokens", 0))
        self.completion += int(entry.get("completion_tokens", 0))
        self.calls += 1
        if entry.get("estimated"):
            self.estimated_calls += 1

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "total": self.prompt + self.completion,
            "calls": self.calls,
            "estimated_calls": self.estimated_calls,
        }


class UsageLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, metadata: UsageMetadata, prompt_tokens: int, completion_tokens: int, estimated: bool) -> None:
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "session_id": metadata.session_id,
            "workspace": metadata.workspace,
            "model_id": metadata.model_id,
            "model": metadata.model,
            "model_name": metadata.model_name or metadata.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated": estimated,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # 进程被杀时可能留下半行，跳过
                continue
        return entries

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
    characters = len(response.text) + len(response.reasoning)
    for call in response.tool_calls:
        characters += len(call.name) + len(call.raw_arguments)
    return characters // 3


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
            self.usage_log.record(
                self.metadata, response.usage.prompt_tokens, response.usage.completion_tokens, estimated=False
            )
        else:
            self.usage_log.record(
                self.metadata, estimate_tokens(messages), _estimate_completion_tokens(response), estimated=True
            )
        return response
