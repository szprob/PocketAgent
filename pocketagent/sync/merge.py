"""两边都改过同一个文件时怎么办。

会话是 JSON：按消息逐条求并集，前缀关系直接取长的，真分叉时取「更新时间晚的那份」当骨架、
把另一份缺的消息追加到末尾。会话文件里没有稳定的消息 id（老会话只有最后一条有 _id），
所以身份用「角色 + 内容 + 工具调用 id」的哈希，重复内容按出现次数区分。

项目记忆是 Markdown、hooks 是脚本、配置是 JSON：没有可靠的自动合并办法，
保留本地版本，把远端那份原样存到 ~/.pocketagent/sync/conflicts/ 让用户自己看。
不往文件里写冲突标记——那些标记会被 agent 当正文读进上下文。
"""

import hashlib
import json
from dataclasses import dataclass

SENTINEL = object()


@dataclass
class MergeResult:
    data: bytes
    conflict: bool = False
    note: str = ""


def merge_session(local: bytes, remote: bytes) -> MergeResult:
    local_session = _load_object(local)
    remote_session = _load_object(remote)
    if local_session is None and remote_session is not None:
        # 本地文件坏了：用远端那份，别把好的覆盖掉
        return MergeResult(remote, note="本地会话不是合法 JSON，用远端版本")
    if remote_session is None:
        return MergeResult(local, conflict=True, note="远端会话不是合法 JSON，本地这份保留")

    local_messages = _messages(local_session)
    remote_messages = _messages(remote_session)
    local_keys = _message_keys(local_messages)
    remote_keys = _message_keys(remote_messages)

    if local_messages == remote_messages:
        merged_messages = local_messages
        # 只有元数据不同（标题、todos、审批模式）：取更新时间晚的那份
        primary = remote_session if _newer(remote_session, local_session) else local_session
    elif _is_prefix(local_keys, remote_keys):
        primary = remote_session
        merged_messages = list(remote_messages)
    elif _is_prefix(remote_keys, local_keys):
        primary = local_session
        merged_messages = list(local_messages)
    else:
        # 真分叉：骨架用更新晚的那份，另一份独有的消息按原顺序接在后面
        if _newer(remote_session, local_session):
            primary = remote_session
            primary_messages, primary_keys = remote_messages, remote_keys
            secondary_messages, secondary_keys = local_messages, local_keys
        else:
            primary = local_session
            primary_messages, primary_keys = local_messages, local_keys
            secondary_messages, secondary_keys = remote_messages, remote_keys
        merged_messages = list(primary_messages)
        seen_keys = set(primary_keys)
        for message, key in zip(secondary_messages, secondary_keys):
            if key not in seen_keys:
                merged_messages.append(message)
                seen_keys.add(key)

    merged = dict(primary)
    merged["messages"] = merged_messages
    merged["turns"] = max(_integer(local_session.get("turns")), _integer(remote_session.get("turns")))
    merged["tool_calls"] = max(_integer(local_session.get("tool_calls")), _integer(remote_session.get("tool_calls")))
    created = [text for text in (local_session.get("created_at"), remote_session.get("created_at")) if text]
    if created:
        merged["created_at"] = min(created)
    updated = [text for text in (local_session.get("updated_at"), remote_session.get("updated_at")) if text]
    if updated:
        merged["updated_at"] = max(updated)
    # 会话不做冲突副本：消息已经求过并集，没有需要人来做决定的部分
    diverged = bool(set(local_keys) - set(remote_keys)) and bool(set(remote_keys) - set(local_keys))
    note = "两边都往前走了，消息取并集" if diverged else ""
    return MergeResult(_dump_json(merged), conflict=False, note=note)


def merge_text(local: bytes, remote: bytes, base: bytes | None) -> MergeResult:
    if base is not None:
        if local == base:
            return MergeResult(remote)
        if remote == base:
            return MergeResult(local)
    if local == remote:
        return MergeResult(local)
    return MergeResult(local, conflict=True, note="两边都改过，保留本地版本")


def merge_json_object(local: bytes, remote: bytes, base: bytes | None) -> MergeResult:
    local_object = _load_object(local)
    remote_object = _load_object(remote)
    base_object = _load_object(base) if base is not None else None
    if local_object is None or remote_object is None:
        return MergeResult(local, conflict=True, note="不是合法 JSON，保留本地版本")
    if base_object is None:
        if local_object == remote_object:
            return MergeResult(local)
        return MergeResult(local, conflict=True, note="没有上次同步的底稿，保留本地版本")

    merged = dict(local_object)
    conflict = False
    for key in set(base_object) | set(local_object) | set(remote_object):
        base_value = base_object.get(key, SENTINEL)
        local_value = local_object.get(key, SENTINEL)
        remote_value = remote_object.get(key, SENTINEL)
        if local_value == remote_value:
            chosen = local_value
        elif local_value == base_value:
            chosen = remote_value
        elif remote_value == base_value:
            chosen = local_value
        else:
            chosen = local_value
            conflict = True
        if chosen is SENTINEL:
            merged.pop(key, None)
        else:
            merged[key] = chosen
    return MergeResult(_dump_json(merged), conflict=conflict, note="两个键都改过，本地优先" if conflict else "")


def merge_by_kind(kind: str, local: bytes, remote: bytes, base: bytes | None) -> MergeResult:
    if kind == "session":
        return merge_session(local, remote)
    if kind in ("config", "automations", "mcp"):
        return merge_json_object(local, remote, base)
    return merge_text(local, remote, base)


def _messages(session: dict) -> list[dict]:
    messages = session.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def _message_keys(messages: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    keys: list[str] = []
    for message in messages:
        identity = _message_identity(message)
        counts[identity] = counts.get(identity, 0) + 1
        keys.append(f"{identity}#{counts[identity]}")
    return keys


def _message_identity(message: dict) -> str:
    message_id = message.get("_id")
    if message_id:
        return f"id:{message_id}"
    tool_ids = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("id"):
            tool_ids.append(call["id"])
    payload = json.dumps(
        [message.get("role"), message.get("content"), tool_ids, message.get("name")],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "hash:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_prefix(shorter: list[str], longer: list[str]) -> bool:
    if len(shorter) >= len(longer):
        return False
    return all(longer[index] == key for index, key in enumerate(shorter))


def _newer(candidate: dict, other: dict) -> bool:
    return str(candidate.get("updated_at") or "") > str(other.get("updated_at") or "")


def _integer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load_object(data: bytes | None) -> dict | None:
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _dump_json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")
