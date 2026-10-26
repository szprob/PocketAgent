"""把存下来的 OpenAI 格式消息转成前端渲染用的条目，用于刷新页面或切换会话时重放历史。"""

import json
from typing import Any

from pocketagent.compaction import SUMMARY_PREFIX
from pocketagent.tools.base import ToolRegistry


def _parse_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_call_item(tool_call: dict[str, Any], tools: ToolRegistry) -> dict[str, Any]:
    function = tool_call.get("function", {})
    name = function.get("name", "")
    raw_arguments = function.get("arguments", "")
    tool = tools.get(name)
    summary = tool.describe(_parse_arguments(raw_arguments)) if tool else raw_arguments
    return {"kind": "tool", "call_id": tool_call.get("id"), "name": name, "summary": summary, "result": None}


def _user_item(content: str) -> dict[str, Any]:
    if content.startswith(SUMMARY_PREFIX):
        return {"kind": "summary", "text": content[len(SUMMARY_PREFIX) :]}
    return {"kind": "user", "text": content}


def history_items(messages: list[dict[str, Any]], tools: ToolRegistry) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    tool_items_by_call_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "user":
            items.append(_user_item(content))
        elif role == "assistant":
            if content:
                items.append({"kind": "assistant", "text": content})
            for tool_call in message.get("tool_calls") or []:
                item = _tool_call_item(tool_call, tools)
                items.append(item)
                tool_items_by_call_id[item["call_id"]] = item
        elif role == "tool":
            item = tool_items_by_call_id.get(message.get("tool_call_id"))
            if item is not None:
                item["result"] = content
    return items
