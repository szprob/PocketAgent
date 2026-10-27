"""上下文 token 记账。

两层：
1. 锚点：每次模型调用后，接口报的 prompt_tokens + 这次输出（减掉不回传的思考 token）就是「到这条回复为止」的真实占用，
   记在 AgentContext.token_anchor 里；之后只需要估算锚点以后新加的消息（工具结果、新的一句话……）。
2. 估算：没有锚点（新会话、刚压缩完）或者估锚点之后的增量时，按字符分类估：
   2026-10-26 拿 DeepSeek / 智谱 / Grok 的真实分词器量过，中文每字 0.63–0.68 token，代码 0.22–0.26，英文 0.20。
   这里取偏高的值，宁可早一点压缩也别超长。原来「JSON 字符数 ÷ 3」把中文少算了一半。
"""

import json
import math
import re
from typing import Any

# 中日韩文字、全角标点
CJK_PATTERN = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯　-〿]")
CJK_TOKENS_PER_CHAR = 0.7
OTHER_TOKENS_PER_CHAR = 0.27
# 每条消息的角色、分隔符等开销
MESSAGE_OVERHEAD_TOKENS = 4
# 一张图大概占多少 token（发之前已经缩到长边 2048 以内，各家大多在 1000–1600）
IMAGE_TOKENS = 1600


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = len(CJK_PATTERN.findall(text))
    other_count = len(text) - cjk_count
    return math.ceil(cjk_count * CJK_TOKENS_PER_CHAR + other_count * OTHER_TOKENS_PER_CHAR)


def _content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if part.get("type") == "image_url":
            total += IMAGE_TOKENS
        else:
            total += estimate_text_tokens(part.get("text", ""))
    return total


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total = MESSAGE_OVERHEAD_TOKENS + _content_tokens(message.get("content"))
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        total += estimate_text_tokens(function.get("name", "")) + estimate_text_tokens(function.get("arguments", ""))
    return total


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tools_tokens(tool_schemas: list[dict[str, Any]]) -> int:
    """工具定义也算在 prompt 里，没有锚点时要加上。"""
    return estimate_text_tokens(json.dumps(tool_schemas, ensure_ascii=False)) if tool_schemas else 0
