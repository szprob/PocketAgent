"""按剧本依次返回预设回复的假模型，单测和离线调试用。"""

import copy
from typing import Any

from pocketagent.llm.base import LLMResponse, StreamListener


class ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.remaining_responses = list(responses)
        # 每次调用时模型看到的消息，测试里拿来断言
        self.received_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        listener: StreamListener,
    ) -> LLMResponse:
        self.received_messages.append(copy.deepcopy(messages))
        if not self.remaining_responses:
            raise RuntimeError("ScriptedProvider 的剧本已经用完")
        response = self.remaining_responses.pop(0)
        if response.reasoning:
            await listener.on_reasoning(response.reasoning)
        if response.text:
            await listener.on_text(response.text)
        return response
