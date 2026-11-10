"""上下文压缩：对话快塞满上下文窗口时，把整段历史换成一份摘要，再接着干。

参考 Claude Code / Qoder / WorkBuddy 的做法（2026-11-10 看过它们装在本机的代码）：
- 整段历史一起总结，上一次的摘要也在里面，由模型自己挑还有用的留下，过时的去掉；不另外保留最近几条原文。
  后台压缩期间新加的消息（快照之后的）原样接在摘要后面，这是唯一的例外。
- 做摘要的请求复用平时那一份前缀：同样的系统提示、工具定义和消息，末尾追加一条压缩指令，
  接口的前缀缓存能整段命中，这次请求几乎只为输出付钱。工具定义照带（换了就不命中缓存），指令里要求只写文字。
- 请求放不下（接口报超长）或模型没给出文字时退回老办法：不带工具、不发图片，长消息截短、思考裁短写进正文，
  还放不下就从最早的对话开始丢。
- 换上摘要时把还要用的东西原样接在后面：加载过的技能正文、任务清单、计划，
  还有最近读过、改过的几个文件现在的内容（和 Claude Code / Qoder 一样：最多 5 个、每个约 5k token），
  接着改代码不用先把文件重读一遍。

默认在后台压缩，不挡着对话（见 nodes.py 的 GuardNode）：
- 占用超过阈值：拿当时的历史快照在后台做摘要，这一轮照常往下跑；
- 摘要做好：在两步之间换上「摘要 + 快照之后新加的消息」；
- 只有快顶到上限、或者接口报超长时才停下来等。
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pocketagent.core.tokens import estimate_text_tokens as estimate_tokens_of_text
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

# 压缩指令，照 Claude Code 的九节结构写（Qoder 原样用的也是这一套），加上 WorkBuddy 的「做完的别重做」
# 和 Qoder 的「这条指令不算用户的话」（实测不写这句，模型会把它列进用户消息、还写进下一步）。
# 篇幅：Claude Code / Qoder 不限字数，WorkBuddy 限 1000 词；这里给个大概（约 5k token），把篇幅留给具体信息
SUMMARY_INSTRUCTION = """现在先不要继续任务，也不要调用任何工具：只用文字写一份交接摘要。压缩之后，上面这些对话会被这份摘要整个替换掉，接着干活的你只看得到它。
这条压缩指令是 PocketAgent 自动发的，不是用户说的话：不要把它列进「用户说过的话」，也不要把「不调用工具、只写摘要」写进待办或下一步。

请把到目前为止的整段对话（如果开头有上一次压缩留下的摘要，也把它当成对话的一部分）重新整理成一份摘要。还有用的留下，已经过时、被推翻、和后面无关的去掉。按下面九节写：

1. 用户的需求和意图：用户要什么，后来补充、纠正、明确不要做的，尽量保留原话
2. 关键技术点：用到的技术、框架、约定、重要的设计决定
3. 文件和代码：看过、改过、新建的文件，每个写清为什么重要、改了什么；关键的代码片段照原样贴
4. 报错和修复：遇到的报错（原文）和怎么修的；用户纠正过的做法和原因
5. 解决问题的过程：已经解决的问题、正在排查的问题、试过但不行的办法和原因（免得接手后再试一遍）、还没验证的猜测
6. 用户说过的话：用户的每一条消息（工具结果不算）按顺序列出，原话或贴近原话，这是判断用户意图变化的依据
7. 待办：用户明确交代、还没做完的事
8. 正在做的事：压缩前最后在做什么，具体到文件、函数、命令
9. 下一步：接下来要做的那一步，必须是用户最近要求的事；引用最近对话的原话，免得做偏。已经做完、用户没再要求的事不要列成下一步

写法：
- 具体：文件路径、函数名、命令、报错原文、数字、链接照原样写，不要概括成「改了一些文件」「遇到了报错」
- 结论带依据（跑了什么测试、看了哪段输出），没验证的写明没验证；已经做完的事写明做完了，接手后不要重做
- 篇幅通常 5000 token（中文六七千字）以内就够了；内容少就短，不要凑字数、不要重复
- 只输出摘要正文，不要寒暄，不要调用工具"""

# 摘要消息开头的标记：界面、会话列表靠它认出这是摘要（改了要兼容旧会话）
SUMMARY_PREFIX = "（之前的对话已压缩，以下是摘要）\n\n"
RESUME_HINT = "\n\n以上是压缩前的对话摘要。接着上次停下的地方继续：没做完的事直接接着做，不要向用户复述或确认摘要。"
# 以前版本写在摘要末尾的话，认旧会话里的摘要时一起剥掉
LEGACY_RESUME_HINTS = ("\n\n请根据摘要继续完成任务。",)
# 加载过的技能正文不进摘要，原样接在摘要后面（Agent Skills 规范建议压缩时保留）
PRESERVED_SKILLS_HEADER = "\n\n（压缩前加载过的技能，完整说明原样保留如下）\n\n"
TODOS_HEADER = "\n\n（压缩前的任务清单，原样保留）\n"
PLAN_HEADER = "\n\n（压缩前的计划，原样保留）\n"
RESTORED_FILES_HEADER = "\n\n（压缩前最近读过、改过的文件，下面是它们现在的内容，行号和 read_file 一样；没列全的部分需要时再读）\n"
# 压缩后重新附上的文件：从这几个工具的调用里找，越近的越先放
FILE_TOOL_NAMES = ("read_file", "edit_file", "write_file")
RESTORE_FILE_COUNT = 5
RESTORE_FILE_MAX_TOKENS = 5000
RESTORE_TOTAL_MAX_TOKENS = 50_000
# 退回老办法时，单条消息最多保留这么多字（长的工具输出头尾各留一半）
SUMMARY_MESSAGE_LIMIT_CHARS = 4000
# 退回老办法时每一步的思考最多带这么多字：思考动辄上万字，全带会把请求撑满、挤掉更早的对话；
# 结论和计划多在后半段，所以尾巴留得多
SUMMARY_REASONING_HEAD_CHARS = 1000
SUMMARY_REASONING_TAIL_CHARS = 2000
REASONING_LABEL = "〔当时的思考〕"
REPLY_LABEL = "〔回复〕"
SUMMARY_TAG_PATTERN = re.compile(r"<summary>(.*?)</summary>", re.S)
ANALYSIS_TAG_PATTERN = re.compile(r"<analysis>.*?</analysis>", re.S)


class SummaryFailed(RuntimeError):
    """模型没给出能用的摘要（空的、只想调工具）。"""


def previous_summary_text(messages: list[dict[str, Any]]) -> str | None:
    """历史开头如果已经是上一次的摘要，取出它的正文（剥掉标记和当时保留的技能正文）。"""
    if len(messages) < 2 or messages[0].get("role") != "system":
        return None
    content = messages[1].get("content")
    if not isinstance(content, str) or not content.startswith(SUMMARY_PREFIX):
        return None
    body = content[len(SUMMARY_PREFIX) :]
    for hint in (RESUME_HINT, *LEGACY_RESUME_HINTS):
        if body.endswith(hint):
            body = body[: -len(hint)]
    # 摘要后面原样接着的技能、任务清单、计划不算摘要正文
    for header in (PRESERVED_SKILLS_HEADER, TODOS_HEADER, PLAN_HEADER, RESTORED_FILES_HEADER):
        position = body.find(header)
        if position != -1:
            body = body[:position]
    return body.strip()


async def summarize_history(
    provider: LLMProvider, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> str:
    """messages 末尾追加压缩指令，让模型写摘要。tools 传平时那份工具定义时请求前缀和平时一样、能命中缓存。"""
    previous = previous_summary_text(messages)
    if previous is not None and len(messages) <= 2:
        # 只有旧摘要、没有新增对话，再压一遍是白做还可能丢字，直接沿用
        return previous
    request_messages = list(messages) + [{"role": "user", "content": SUMMARY_INSTRUCTION}]
    # 摘要不往界面上流式输出
    response: LLMResponse = await provider.complete(request_messages, tools=tools or [], listener=SilentListener())
    summary = clean_summary(response.text)
    if not summary:
        detail = "它想调用工具" if response.tool_calls else "返回了空内容"
        raise SummaryFailed(f"压缩失败：模型没有给出摘要（{detail}）")
    return summary


def clean_summary(text: str) -> str:
    """有的模型照 Claude Code 的格式先写 <analysis> 再写 <summary>：只要 summary 里的正文。"""
    text = ANALYSIS_TAG_PATTERN.sub("", text or "")
    match = SUMMARY_TAG_PATTERN.search(text)
    if match:
        text = match.group(1)
    return text.strip()


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


@dataclass
class RestoredFile:
    """压缩后重新附上的一个文件：路径（模型当时怎么写的就怎么写）和现在的内容（带行号，可能截了尾）。"""

    path: str
    content: str


@dataclass
class PreservedState:
    """换上摘要时原样接在后面的东西：摘要难免走样，这些后面还要照着做。"""

    todos: list[dict[str, Any]] | None = None
    plan: str = ""
    files: list[RestoredFile] = field(default_factory=list)


def recent_file_paths(messages: list[dict[str, Any]], limit: int | None = RESTORE_FILE_COUNT) -> list[str]:
    """最近读过、改过的文件路径，越近越靠前、不重复；从 read_file / edit_file / write_file 的调用参数里找。"""
    paths: list[str] = []
    for message in reversed(messages):
        for call in reversed(message.get("tool_calls") or []):
            path = _file_tool_path(call)
            if path and path not in paths:
                paths.append(path)
                if limit is not None and len(paths) >= limit:
                    return paths
    return paths


def _file_tool_path(call: dict[str, Any]) -> str:
    function = call.get("function") or {}
    if function.get("name") not in FILE_TOOL_NAMES:
        return ""
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        return ""
    path = arguments.get("path") if isinstance(arguments, dict) else None
    return path.strip() if isinstance(path, str) else ""


def clip_to_tokens(numbered_text: str, max_tokens: int) -> str:
    """带行号的文件内容超过预算时按整行截尾，末尾说明从哪行接着读。"""
    if estimate_tokens_of_text(numbered_text) <= max_tokens:
        return numbered_text
    kept: list[str] = []
    used = 0
    for line in numbered_text.splitlines():
        cost = estimate_tokens_of_text(line) + 1
        if used + cost > max_tokens:
            break
        kept.append(line)
        used += cost
    next_line = len(kept) + 1
    kept.append(f"（后面还有，需要时用 read_file 从 offset={next_line} 接着读）")
    return "\n".join(kept)


def rebuild_with_summary_and_tail(
    messages: list[dict[str, Any]], snapshot_length: int, summary: str, preserved: PreservedState | None = None
) -> list[dict[str, Any]]:
    """快照部分换成摘要，快照之后（后台做摘要期间）新加的消息接在后面。快照总是在两步之间拍的，后面不会以 tool 结果开头。
    技能正文从整段历史里找（快照之后的消息里有就用最新的那份），接在摘要后面。"""
    skill_blocks = loaded_skill_blocks(messages[1:])
    rebuilt = rebuild_messages_with_summary(messages[:snapshot_length], summary, skill_blocks, preserved)
    return rebuilt + messages[snapshot_length:]


def rebuild_messages_with_summary(
    messages: list[dict[str, Any]],
    summary: str,
    skill_blocks: list[str] | None = None,
    preserved: PreservedState | None = None,
) -> list[dict[str, Any]]:
    """保留开头的系统提示，其余换成一条 user 摘要消息；技能正文、任务清单、计划接在摘要后面。"""
    rebuilt = []
    if messages and messages[0].get("role") == "system":
        rebuilt.append(messages[0])
    if skill_blocks is None:
        skill_blocks = loaded_skill_blocks(messages[1:] if rebuilt else messages)
    attached = PRESERVED_SKILLS_HEADER + "\n\n".join(skill_blocks) if skill_blocks else ""
    attached += _preserved_text(preserved)
    rebuilt.append({"role": "user", "content": SUMMARY_PREFIX + summary + attached + RESUME_HINT})
    return rebuilt


def _preserved_text(preserved: PreservedState | None) -> str:
    if preserved is None:
        return ""
    text = ""
    if preserved.todos:
        lines = [f"- [{item.get('status', 'pending')}] {item.get('content', '')}" for item in preserved.todos]
        text += TODOS_HEADER + "\n".join(lines)
    if preserved.plan.strip():
        text += PLAN_HEADER + preserved.plan.strip()
    if preserved.files:
        blocks = [f"### {restored.path}\n{restored.content}" for restored in preserved.files]
        text += RESTORED_FILES_HEADER + "\n\n".join(blocks)
    return text
