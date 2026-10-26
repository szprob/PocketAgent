"""上下文压缩：对话快塞满上下文窗口时，让模型把历史总结成一段摘要，再接着干。

除系统提示外的历史换成一条摘要消息；整段替换不会把 assistant 的 tool_calls 和对应的 tool 结果拆开。

默认在后台压缩，不挡着对话（见 nodes.py 的 GuardNode）：
- 占用超过阈值：拿当时的历史快照在后台做摘要，这一轮照常往下跑；
- 摘要做好：在两步之间换上「摘要 + 快照之后新加的消息」；
- 只有快顶到上限、或者接口报超长时才停下来等。
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from pocketagent.llm.base import LLMProvider, LLMResponse, SilentListener
from pocketagent.tokens import estimate_tokens

SUMMARY_INSTRUCTION = """请把上面的对话压缩成一份交接摘要，让接手的人不看原始记录也能继续工作。必须包含：
1. 用户的原始需求和后来补充的要求（尽量保留原话）
2. 已经完成了什么，改了哪些文件、关键改动是什么
3. 读过的关键文件、重要发现、踩过的坑
4. 还没做完的事和下一步计划
只输出摘要本身，不要调用工具。"""

SUMMARY_PREFIX = "（之前的对话已压缩，以下是摘要）\n\n"
RESUME_HINT = "\n\n请根据摘要继续完成任务。"
# 给模型做摘要时，单条消息最多保留这么多字（长的工具输出头尾各留一半）
SUMMARY_MESSAGE_LIMIT_CHARS = 4000


async def summarize_history(provider: LLMProvider, messages: list[dict[str, Any]]) -> str:
    request_messages = messages + [{"role": "user", "content": SUMMARY_INSTRUCTION}]
    # 摘要不往界面上流式输出
    response: LLMResponse = await provider.complete(request_messages, tools=[], listener=SilentListener())
    summary = response.text.strip()
    if not summary:
        raise RuntimeError("压缩失败：模型返回了空摘要")
    return summary


def _truncated_copy(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str) or len(content) <= SUMMARY_MESSAGE_LIMIT_CHARS:
        return message
    half = SUMMARY_MESSAGE_LIMIT_CHARS // 2
    omitted = len(content) - SUMMARY_MESSAGE_LIMIT_CHARS
    return {**message, "content": f"{content[:half]}\n…（省略 {omitted} 字）…\n{content[-half:]}"}


def _drop_oldest_exchange(messages: list[dict[str, Any]]) -> bool:
    """丢掉系统提示和第一条用户消息之后最早的一段：一条 assistant 连同它的 tool 结果一起丢，
    这样不会留下对不上的 tool_call。返回是否丢了东西。"""
    start = 2 if len(messages) > 1 and messages[1].get("role") == "user" else 1
    if len(messages) <= start + 1:
        return False
    end = start + 1
    if messages[start].get("role") == "assistant":
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
    del messages[start:end]
    return True


def shrink_for_summary(messages: list[dict[str, Any]], limit_tokens: int) -> list[dict[str, Any]]:
    """要总结的历史本身可能已经超过上下文（接口报超长时就是这样）。
    先把每条长消息截短，还超就从最早的对话开始丢，系统提示和第一条用户消息总是保留。"""
    shrunk = [_truncated_copy(message) for message in messages]
    while estimate_tokens(shrunk) > limit_tokens and _drop_oldest_exchange(shrunk):
        pass
    return shrunk


@dataclass
class PendingCompaction:
    """后台正在做的压缩。"""

    task: "asyncio.Task[str]"
    # 摘要覆盖历史里的前多少条；换上摘要时，这之后新加的消息原样接在摘要后面
    snapshot_length: int
    # 开始时历史的版本号；期间历史被别的压缩换掉了，这份摘要就作废
    history_version: int
    reason: str
    tokens_before: int


def rebuild_with_summary_and_tail(
    messages: list[dict[str, Any]], snapshot_length: int, summary: str
) -> list[dict[str, Any]]:
    """快照部分换成摘要，快照之后的消息接在后面。快照总是在两步之间拍的，后面不会以 tool 结果开头。"""
    return rebuild_messages_with_summary(messages[:snapshot_length], summary) + messages[snapshot_length:]


def rebuild_messages_with_summary(messages: list[dict[str, Any]], summary: str) -> list[dict[str, Any]]:
    """保留开头的系统提示，其余换成一条 user 摘要消息。"""
    rebuilt = []
    if messages and messages[0].get("role") == "system":
        rebuilt.append(messages[0])
    rebuilt.append({"role": "user", "content": SUMMARY_PREFIX + summary + RESUME_HINT})
    return rebuilt
