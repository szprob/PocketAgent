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

from pocketagent.core.tokens import estimate_tokens
from pocketagent.knowledge.skills import loaded_skill_blocks
from pocketagent.llm.base import (
    REASONING_FIELD,
    LLMProvider,
    LLMResponse,
    SilentListener,
    without_earlier_reasoning,
    without_reasoning,
)

# 摘要宁可详细：压缩之后模型只看得到它，漏掉的细节（路径、报错原文、用户原话、试过不行的办法）就再也找不回来，
# 只能重新查、重新踩一遍坑。所以要求写具体、写依据，不限字数
SUMMARY_DETAIL_RULES = """写法要求：
- 宁可详细也不要漏，不用追求简短；内容多就写长，按上面几节分标题写
- 写具体的东西：文件路径、函数名、命令、报错原文、关键数字、链接、配置项，照原样写，不要概括成「改了一些文件」「遇到了报错」
- 结论要带依据：怎么确认的（跑了什么测试、看了哪段输出），没验证的标明没验证
- assistant 消息里「〔当时的思考〕」是模型当时的思考过程，从里面提取定下的计划、排除过的方向和原因、还没验证的猜测，不要照抄推理过程"""

SUMMARY_SECTIONS = """1. 用户的需求：原始需求和后来补充、纠正的要求，尽量保留原话；用户明确说过不要做的事
2. 已经完成的工作：做了什么、改了哪些文件、每处关键改动是什么、怎么验证的
3. 重要发现：读过的关键文件和其中的要点、查到的资料（带来源）、确认过的事实
4. 试过但不行的办法和踩过的坑：为什么不行，免得接手后再试一遍
5. 当时的计划和判断：思考里定下的做法、排除过的方向、还没验证的猜测
6. 还没做完的事和下一步：具体到下一步要做什么、在哪个文件、还卡在什么地方"""

SUMMARY_INSTRUCTION = f"""请把上面的对话压缩成一份交接摘要，让接手的人不看原始记录也能接着干，不用重新查一遍。包含：
{SUMMARY_SECTIONS}

{SUMMARY_DETAIL_RULES}
只输出摘要本身，不要调用工具。"""

MERGE_SUMMARY_INSTRUCTION = f"""上面带「（之前的对话已压缩，以下是摘要）」开头那条消息是上一次的交接摘要，它之后的消息是摘要之后新增的对话。请把两边合并成一份新的交接摘要，让接手的人不看原始记录也能接着干：
- 旧摘要里的信息原样保留，不要省略、不要改写成更模糊的说法；只有被新增对话推翻的才改，以新增对话为准
- 把新增对话里发生的事按下面几节并进去，还没做完的事和下一步按最新情况更新
{SUMMARY_SECTIONS}

{SUMMARY_DETAIL_RULES}
只输出合并后的摘要本身，不要调用工具。"""

SUMMARY_PREFIX = "（之前的对话已压缩，以下是摘要）\n\n"
RESUME_HINT = "\n\n请根据摘要继续完成任务。"
# 加载过的技能正文不进摘要，原样接在摘要后面（Agent Skills 规范建议压缩时保留）
PRESERVED_SKILLS_HEADER = "\n\n（压缩前加载过的技能，完整说明原样保留如下）\n\n"
# 给模型做摘要时，单条消息最多保留这么多字（长的工具输出头尾各留一半）
SUMMARY_MESSAGE_LIMIT_CHARS = 4000
# 每一步的思考最多带这么多字进摘要请求：思考动辄上万字，全带会把请求撑满、挤掉更早的对话；
# 结论和计划多在后半段，所以尾巴留得多
SUMMARY_REASONING_HEAD_CHARS = 1000
SUMMARY_REASONING_TAIL_CHARS = 2000
REASONING_LABEL = "〔当时的思考〕"
REPLY_LABEL = "〔回复〕"


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
    request_messages = with_reasoning_as_text(messages) + [{"role": "user", "content": instruction}]
    # 摘要不往界面上流式输出
    response: LLMResponse = await provider.complete(request_messages, tools=[], listener=SilentListener())
    summary = response.text.strip()
    if not summary:
        raise RuntimeError("压缩失败：模型返回了空摘要")
    return summary


def with_reasoning_as_text(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """做摘要时把每一步的思考（裁短后）写进 assistant 的正文，其他格式的思考字段去掉。不改原消息。

    思考里有当时的计划、排除过的方向和还没验证的猜测，摘要要用。不能留在 reasoning_content 里交给接口：
    有的接口直接忽略这个字段（DeepSeek 官方），OpenRouter / Claude 的加密块模型也读不出来，所以转成正文。
    已经转过的消息没有思考字段了，再调一次不会重复加。"""
    converted = []
    for message, original in zip(without_reasoning(messages), messages):
        reasoning = str(original.get(REASONING_FIELD) or "").strip()
        if original.get("role") == "assistant" and reasoning:
            reply = _content_text(message.get("content"))
            parts = [f"{REASONING_LABEL}\n{_clip_reasoning(reasoning)}"]
            if reply:
                parts.append(f"{REPLY_LABEL}\n{reply}")
            message = {**message, "content": "\n\n".join(parts)}
        converted.append(message)
    return converted


def _clip_reasoning(text: str) -> str:
    limit = SUMMARY_REASONING_HEAD_CHARS + SUMMARY_REASONING_TAIL_CHARS
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    head, tail = text[:SUMMARY_REASONING_HEAD_CHARS], text[-SUMMARY_REASONING_TAIL_CHARS:]
    return f"{head}\n…（中间省略 {omitted} 字）…\n{tail}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
    return ""


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
    或者系统提示和旧摘要）永不截短、永不丢。
    思考内容裁短后写进正文（见 with_reasoning_as_text），长度按转换后的算。"""
    truncated = [message if index < protected else _truncated_copy(message) for index, message in enumerate(messages)]
    shrunk = with_reasoning_as_text(truncated)
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


def without_old_reasoning(messages: list[dict[str, Any]], turn_in_progress: bool) -> list[dict[str, Any]]:
    """压缩后留下的消息里不带旧的思考内容，不管「跨轮保留思考内容」开没开：压缩本来就要重建缓存，正好清掉。
    正在进行的这一轮还要接着调工具，这一轮的思考得照常传回，留着。不改原消息。"""
    if turn_in_progress:
        return without_earlier_reasoning(messages)
    return without_reasoning(messages)


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
