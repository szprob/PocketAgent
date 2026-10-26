"""把存下来的消息转成前端渲染用的条目，用于刷新页面或切换任务时重放历史。"""

import json
from typing import Any

from pocketagent.attachments import message_display
from pocketagent.compaction import PRESERVED_SKILLS_HEADER, SUMMARY_PREFIX
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
    return {
        "kind": "tool",
        "call_id": tool_call.get("id"),
        "name": name,
        "summary": summary,
        "result": None,
        "is_error": False,
        "denied": False,
        "review": None,
    }


def _user_item(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("_kind") == "notice":
        return {"kind": "notice", "text": message.get("content") or ""}
    display = message_display(message)
    if display["text"].startswith(SUMMARY_PREFIX):
        # 摘要后面保留的技能正文不用显示给用户
        summary = display["text"][len(SUMMARY_PREFIX) :].split(PRESERVED_SKILLS_HEADER)[0]
        return {"kind": "summary", "text": summary}
    return {"kind": "user", "text": display["text"], "attachments": display["attachments"]}


def _fill_tool_result(item: dict[str, Any], message: dict[str, Any]) -> None:
    item["result"] = message.get("content") or ""
    item["is_error"] = bool(message.get("_is_error"))
    item["denied"] = bool(message.get("_denied"))
    item["review"] = message.get("_review")


def history_items(messages: list[dict[str, Any]], tools: ToolRegistry) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    tool_items_by_call_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        role = message.get("role")
        if role == "user":
            items.append(_user_item(message))
        elif role == "assistant":
            content = message.get("content") or ""
            if content:
                items.append({"kind": "assistant", "text": content})
            for tool_call in message.get("tool_calls") or []:
                item = _tool_call_item(tool_call, tools)
                items.append(item)
                tool_items_by_call_id[item["call_id"]] = item
        elif role == "tool":
            item = tool_items_by_call_id.get(message.get("tool_call_id"))
            if item is not None:
                _fill_tool_result(item, message)
    return items
