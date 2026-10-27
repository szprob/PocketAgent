"""上下文压缩：对话快塞满上下文窗口时，把历史换成摘要，再接着干。

- 增量合并：历史开头已经是上一次的摘要时，不是把摘要当普通历史重新压一遍（那样每压一次
  早期细节就失真一轮），而是让模型在旧摘要的基础上合并新增的对话，旧摘要的内容原样保留。
- 末尾留原文：后台压缩只把前面换成摘要，最近几条消息原样保留，最近的工具输出、文件内容
  不会因为进了摘要而走样（见 nodes.py 的 _snapshot_length）。

默认在后台压缩，不挡着对话（见 nodes.py 的 GuardNode）：
- 占用超过阈值：拿当时的历史快照在后台做摘要，这一轮照常往下跑；
- 摘要做好：在两步之间换上「摘要 + 快照之后新加的消息」；
- 只有快顶到上限、或者接口报超长时才停下来等。
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from pocketagent.llm.base import LLMProvider, LLMResponse, SilentListener
from pocketagent.skills import loaded_skill_blocks
from pocketagent.tokens import estimate_tokens

SUMMARY_INSTRUCTION = """请把上面的对话压缩成一份交接摘要，让接手的人不看原始记录也能继续工作。必须包含：
1. 用户的原始需求和后来补充的要求（尽量保留原话）
2. 已经完成了什么，改了哪些文件、关键改动是什么
3. 读过的关键文件、重要发现、踩过的坑
4. 还没做完的事和下一步计划
只输出摘要本身，不要调用工具。"""

MERGE_SUMMARY_INSTRUCTION = """上面带「（之前的对话已压缩，以下是摘要）」开头那条消息是上一次的交接摘要，它之后的消息是摘要之后新增的对话。请把两边合并成一份新的交接摘要，让接手的人不看原始记录也能继续工作：
1. 旧摘要里的关键信息原样保留：用户的原始需求和补充要求、已完成的工作、改过的文件、读过的关键文件、踩过的坑，不要省略、不要改写成更模糊的说法
2. 把新增对话里发生的事并进去：新完成的工作、新改的文件、新读的关键文件、新踩的坑
3. 还没做完的事和下一步计划按最新情况更新
4. 旧摘要和新增对话有出入时，以新增对话为准
只输出合并后的摘要本身，不要调用工具。"""

SUMMARY_PREFIX = "（之前的对话已压缩，以下是摘要）\n\n"
RESUME_HINT = "\n\n请根据摘要继续完成任务。"
# 加载过的技能正文不进摘要，原样接在摘要后面（Agent Skills 规范建议压缩时保留）
PRESERVED_SKILLS_HEADER = "\n\n（压缩前加载过的技能，完整说明原样保留如下）\n\n"
# 给模型做摘要时，单条消息最多保留这么多字（长的工具输出头尾各留一半）
SUMMARY_MESSAGE_LIMIT_CHARS = 4000


def previous_summary_text(messages: list[dict[str, Any]]) -> str | None:
    """历史开头如果已经是上一次的摘要，取出它的正文（剥掉标记和当时保留的技能正文）。"""
    if len(messages) < 2 or messages[0].get("role") != "system":
        return None
    content = messages[1].get("content")
    if not isinstance(content, str) or not content.startswith(SUMMARY_PREFIX):
        return None
    body = content[len(SUMMARY_PREFIX) :]
    header = body.find(PRESERVED_SKILLS_HEADER)
    if header != -1:
        body = body[:header]
    if body.endswith(RESUME_HINT):
        body = body[: -len(RESUME_HINT)]
    return body.strip()


async def summarize_history(provider: LLMProvider, messages: list[dict[str, Any]]) -> str:
    previous = previous_summary_text(messages)
    if previous is not None and len(messages) <= 2:
        # 只有旧摘要、没有新增对话，合并是白做一次还可能丢字，直接沿用
        return previous
    instruction = MERGE_SUMMARY_INSTRUCTION if previous is not None else SUMMARY_INSTRUCTION
    request_messages = messages + [{"role": "user", "content": instruction}]
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


def _drop_oldest_exchange(messages: list[dict[str, Any]], start: int) -> bool:
    """从 start 往后丢最早的一段：一条 assistant 连同它的 tool 结果一起丢，这样不会留下对不上的
    tool_call。start 落在 tool 结果上时，往前跳过这串结果、从它后面的 assistant 开始丢
    （结果前面的 assistant 在保护区里，不会被丢）。返回是否丢了东西。"""
    index = start
    if index < len(messages) and messages[index].get("role") == "tool":
        while index < len(messages) and messages[index].get("role") == "tool":
            index += 1
    if index >= len(messages):
        return False
    end = index + 1
    while end < len(messages) and messages[end].get("role") == "tool":
        end += 1
    del messages[index:end]
    return True


def shrink_for_summary(messages: list[dict[str, Any]], limit_tokens: int, protected: int = 2) -> list[dict[str, Any]]:
    """要总结的历史本身可能已经超过上下文（接口报超长时就是这样）。
    先把每条长消息截短，还超就从最早开始丢；前 protected 条（系统提示和第一条用户消息，
    或者系统提示和旧摘要）永不截短、永不丢。"""
    shrunk = [
        message if index < protected else _truncated_copy(message)
        for index, message in enumerate(messages)
    ]
    while estimate_tokens(shrunk) > limit_tokens and _drop_oldest_exchange(shrunk, protected):
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
    """快照部分换成摘要，快照之后的消息接在后面。快照总是在两步之间拍的，后面不会以 tool 结果开头。
    技能正文从整段历史里找（末尾保留的原文里有就用最新的那份），接在摘要后面。"""
    skill_blocks = loaded_skill_blocks(messages[1:])
    rebuilt = rebuild_messages_with_summary(messages[:snapshot_length], summary, skill_blocks)
    return rebuilt + messages[snapshot_length:]


def rebuild_messages_with_summary(
    messages: list[dict[str, Any]], summary: str, skill_blocks: list[str] | None = None
) -> list[dict[str, Any]]:
    """保留开头的系统提示，其余换成一条 user 摘要消息；加载过的技能正文接在摘要后面。"""
    rebuilt = []
    if messages and messages[0].get("role") == "system":
        rebuilt.append(messages[0])
    if skill_blocks is None:
        skill_blocks = loaded_skill_blocks(messages[1:] if rebuilt else messages)
    preserved = PRESERVED_SKILLS_HEADER + "\n\n".join(skill_blocks) if skill_blocks else ""
    rebuilt.append({"role": "user", "content": SUMMARY_PREFIX + summary + preserved + RESUME_HINT})
    return rebuilt
