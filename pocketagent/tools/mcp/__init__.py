"""MCP（Model Context Protocol）客户端：把 MCP 服务器的工具接进工具注册表。

配置在 ~/.pocketagent/mcp.json（和 Claude Desktop / VS Code 等同一个格式）：

    {
      "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
        "docs": {"url": "https://mcp.example.com/mcp",
                  "headers": {"Authorization": "Bearer ${DOCS_KEY}"}, "timeout_seconds": 60}
      }
    }

- 配了 command 的是 stdio 传输：起一个子进程，JSON-RPC 消息一行一条地收发；
  配了 url 的是 streamable HTTP 传输（2025-03-26 之后的标准；更早的纯 SSE 接入方式不支持）。
- command、args、env 的值、url、headers 的值、cwd 里的 ${VAR} 和 ${VAR:-默认值} 会用环境变量展开，
  变量没设又没默认值时展开成空串（密钥漏配会以鉴权失败的形式暴露出来，而不是悄悄带上字面量）。
- 每个服务器可以单独 "enabled": false 停用；"timeout_seconds" 是一次工具调用的超时，默认 120 秒。
- 工具注册名是 mcp__<服务器名>__<工具名>。MCP 工具能做什么从外面说不清，所以一律按「执行命令」
  类走审批（ask 模式每次问，ai_auto 交给 AI 判断）；界面上可以「本任务总是允许」某个工具。
- 连接断了：这一次调用直接报错，不自动重试——请求可能已经在服务器那边执行了，重试会把有副作用
  的操作做两遍。再调用时会自动重启服务器进程。
- 服务器主动发来的进度（notifications/progress）会转发到界面的工具输出流里。

连接固定跑在 McpManager 自己的线程和事件循环上（TUI 每一轮是 asyncio.run 新开的循环，
子进程管道没法跨循环用）；web 端所有会话共用一份连接（SessionManager 持有一个 McpManager），
命令行一个进程一份。

包里按职责分模块，依赖从下往上：
- protocol.py         协议常量、错误类型、客户端信息、JSON-RPC 响应和结果解析（最底层，不依赖别的模块）
- config.py           mcp.json 的读写、环境变量展开、设置界面的脱敏视图
- stdio_transport.py  stdio 传输的客户端
- http_transport.py   streamable HTTP 传输的客户端
- bridge.py           把 MCP 工具包装成内置工具、渲染调用结果
- manager.py          McpManager：专用线程上的连接、重连、关闭，注册桥接工具
外面统一从 pocketagent.tools.mcp 导入，不用关心名字在哪个模块里。
"""

from pocketagent.tools.mcp.bridge import McpToolBridge, bridged_name, render_call_result
from pocketagent.tools.mcp.config import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    MCP_CONFIG_FILE,
    McpConfigs,
    McpServerConfig,
    config_to_public_dict,
    expand_env_value,
    load_mcp_servers,
    mcp_config_path,
    save_mcp_servers,
)
from pocketagent.tools.mcp.http_transport import HttpMcpClient
from pocketagent.tools.mcp.manager import McpManager, McpServerStatus, start_client
from pocketagent.tools.mcp.protocol import (
    CLIENT_INFO,
    OLD_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    STARTUP_TIMEOUT_SECONDS,
    McpCallResult,
    McpError,
    McpServerError,
    McpToolInfo,
    McpTransportError,
    ProgressListener,
    parse_call_result,
    parse_tools_result,
)
from pocketagent.tools.mcp.stdio_transport import MAX_LIST_PAGES, MAX_MESSAGE_BYTES, StdioMcpClient

__all__ = [
    "CLIENT_INFO",
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "MAX_LIST_PAGES",
    "MAX_MESSAGE_BYTES",
    "MCP_CONFIG_FILE",
    "OLD_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "STARTUP_TIMEOUT_SECONDS",
    "HttpMcpClient",
    "McpCallResult",
    "McpConfigs",
    "McpError",
    "McpManager",
    "McpServerConfig",
    "McpServerError",
    "McpServerStatus",
    "McpToolBridge",
    "McpToolInfo",
    "McpTransportError",
    "ProgressListener",
    "StdioMcpClient",
    "bridged_name",
    "config_to_public_dict",
    "expand_env_value",
    "load_mcp_servers",
    "mcp_config_path",
    "parse_call_result",
    "parse_tools_result",
    "render_call_result",
    "save_mcp_servers",
    "start_client",
]
