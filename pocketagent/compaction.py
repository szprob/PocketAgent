"""上下文压缩：对话快塞满上下文窗口时，让模型把历史总结成一段摘要，再接着干。

做法和 Claude Code 的 /compact 一样：除系统提示外的全部历史换成一条摘要消息。
整段替换不会把 assistant 的 tool_calls 和对应的 tool 结果拆开，消息格式始终合法。
"""

import json
from typing import Any

from pocketagent.llm.base import LLMProvider, LLMResponse, SilentListener

SUMMARY_INSTRUCTION = """请把上面的对话压缩成一份交接摘要，让接手的人不看原始记录也能继续工作。必须包含：
1. 用户的原始需求和后来补充的要求（尽量保留原话）
2. 已经完成了什么，改了哪些文件、关键改动是什么
3. 读过的关键文件、重要发现、踩过的坑
4. 还没做完的事和下一步计划
只输出摘要本身，不要调用工具。"""

SUMMARY_PREFIX = "（之前的对话已压缩，以下是摘要）\n\n"
RESUME_HINT = "\n\n请根据摘要继续完成任务。"


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估计：按序列化后的字符数除以 3。中文偏低、英文偏高，只用来判断要不要压缩。"""
    serialized = json.dumps(messages, ensure_ascii=False)
    return len(serialized) // 3


async def summarize_history(provider: LLMProvider, messages: list[dict[str, Any]]) -> str:
    request_messages = messages + [{"role": "user", "content": SUMMARY_INSTRUCTION}]
    # 摘要不往界面上流式输出
    response: LLMResponse = await provider.complete(request_messages, tools=[], listener=SilentListener())
    summary = response.text.strip()
    if not summary:
        raise RuntimeError("压缩失败：模型返回了空摘要")
    return summary


def rebuild_messages_with_summary(messages: list[dict[str, Any]], summary: str) -> list[dict[str, Any]]:
    """保留开头的系统提示，其余换成一条 user 摘要消息。"""
    rebuilt = []
    if messages and messages[0].get("role") == "system":
        rebuilt.append(messages[0])
    rebuilt.append({"role": "user", "content": SUMMARY_PREFIX + summary + RESUME_HINT})
    return rebuilt
