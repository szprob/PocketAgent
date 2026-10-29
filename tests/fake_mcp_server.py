"""测试用的假 MCP 服务器（stdio 传输），行为用环境变量切换，见 test_mcp.py。

- 默认暴露 echo / picture / fail / chatty / sleepy 五个工具
- FAKE_TOOL_NAME：把 echo 换成这个名字（测重名时用）
- FAKE_ONLY_ECHO=1：只暴露 echo，方便断言工具数量
- FAKE_DIE_ON_CALL=1：收到 tools/call 直接退出，模拟服务器崩掉
- FAKE_DIE_ONCE=1：第一次 tools/call 退出、之后正常（FAKE_STATE_FILE 记状态），测自动重连
- FAKE_SLEEP_SECONDS：sleepy 工具睡多久，默认 3 秒（测超时）
"""

import json
import os
import sys
import time
from pathlib import Path

# 1x1 的透明 PNG，用来测图片结果
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def env_flag(name: str) -> bool:
    return os.environ.get(name) == "1"


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties}


def tool_definitions() -> list[dict]:
    echo_name = os.environ.get("FAKE_TOOL_NAME", "echo")
    tools = [
        {
            "name": echo_name,
            "description": "把 text 原样返回",
            "inputSchema": object_schema({"text": {"type": "string"}}) | {"required": ["text"]},
        }
    ]
    if env_flag("FAKE_ONLY_ECHO"):
        return tools
    tools += [
        {
            "name": "picture",
            "description": "返回一张 1x1 的图片",
            "inputSchema": object_schema({}),
        },
        {
            "name": "fail",
            "description": "总是失败",
            "inputSchema": object_schema({}),
        },
        {
            "name": "chatty",
            "description": "先报进度再返回",
            "inputSchema": object_schema({}),
        },
        {
            "name": "sleepy",
            "description": "睡一会儿再返回",
            "inputSchema": object_schema({}),
        },
    ]
    return tools


def handle_initialize(message: dict) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": (message.get("params") or {}).get("protocolVersion"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-mcp", "version": "0"},
            },
        }
    )


def handle_tools_call(message: dict) -> None:
    if env_flag("FAKE_DIE_ON_CALL"):
        sys.exit(3)
    state_file = os.environ.get("FAKE_STATE_FILE")
    if env_flag("FAKE_DIE_ONCE") and state_file and not os.path.exists(state_file):
        Path(state_file).write_text("died", encoding="utf-8")
        sys.exit(4)

    params = message.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    request_id = message["id"]
    if name == os.environ.get("FAKE_TOOL_NAME", "echo"):
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]},
            }
        )
    elif name == "picture":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": "这是你要的图"},
                        {"type": "image", "data": TINY_PNG_BASE64, "mimeType": "image/png"},
                    ]
                },
            }
        )
    elif name == "fail":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": "这个工具坏掉了"}], "isError": True},
            }
        )
    elif name == "chatty":
        token = (params.get("_meta") or {}).get("progressToken")
        if token is not None:
            for step in (1, 2):
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": step * 50, "message": f"第 {step} 步"},
                    }
                )
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": "说完了"}]},
            }
        )
    elif name == "sleepy":
        time.sleep(float(os.environ.get("FAKE_SLEEP_SECONDS", "3")))
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": "睡醒了"}]},
            }
        )
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"没有这个工具：{name}"},
            }
        )


def handle(message: dict) -> None:
    method = message.get("method")
    if method == "initialize":
        handle_initialize(message)
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tool_definitions()}})
    elif method == "tools/call":
        handle_tools_call(message)
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif "id" in message:
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"不支持 {method}"},
            }
        )


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            handle(message)


if __name__ == "__main__":
    main()
