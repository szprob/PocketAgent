"""把事件逐行写成 JSONL，供事后排查、回放和评测。"""

import json
from pathlib import Path

from pocketagent.events import Event, EventType, json_default


class JsonlTracer:
    """事件订阅者。默认不记逐字的流式增量，完整文本在 llm_response 事件里有。"""

    def __init__(self, path: Path, include_deltas: bool = False) -> None:
        self.path = path
        self.include_deltas = include_deltas
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        if not self.include_deltas and event.type in (EventType.TEXT_DELTA, EventType.REASONING_DELTA):
            return
        with self.path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(event.to_record(), ensure_ascii=False, default=json_default) + "\n")
