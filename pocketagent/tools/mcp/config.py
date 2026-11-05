"""MCP 服务器配置：读写 ~/.pocketagent/mcp.json、展开 ${VAR} 环境变量、给设置界面的脱敏视图。"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocketagent.tools.mcp.protocol import McpError

DEFAULT_CALL_TIMEOUT_SECONDS = 120
MCP_CONFIG_FILE = "mcp.json"


@dataclass
class McpServerConfig:
    """一个 MCP 服务器的配置。command / args / env 是 stdio 传输，url / headers 是 HTTP 传输。"""

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = DEFAULT_CALL_TIMEOUT_SECONDS
    enabled: bool = True

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


@dataclass
class McpConfigs:
    """一次读配置文件的结果：能用的服务器 + 解析不了的问题（问题不挡住能用的部分）。"""

    servers: list[McpServerConfig] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


_ENV_VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env_value(text: str) -> str:
    """把 ${VAR}、${VAR:-默认值} 换成环境变量的值；没设又没默认值就换成空串。"""
    return _ENV_VARIABLE_PATTERN.sub(_expand_one, text)


def _expand_one(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    value = os.environ.get(name)
    if value is None:
        return default if default is not None else ""
    return value


def _expand_everywhere(text: str) -> str:
    return expand_env_value(text) if isinstance(text, str) else text


def mcp_config_path(home_dir: Path) -> Path:
    return Path(home_dir) / MCP_CONFIG_FILE


def load_mcp_servers(home_dir: Path | None) -> McpConfigs:
    """读 ~/.pocketagent/mcp.json。文件不存在（还没配过）不是错误。"""
    if home_dir is None:
        return McpConfigs()
    path = mcp_config_path(home_dir)
    if not path.is_file():
        return McpConfigs()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return McpConfigs(problems=[f"MCP 配置 {path} 读不了：{error}"])
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        return McpConfigs(problems=[f"MCP 配置 {path} 顶层要是一个对象，里面 mcpServers 列各个服务器"])
    servers: list[McpServerConfig] = []
    problems: list[str] = []
    for name, entry in data["mcpServers"].items():
        try:
            servers.append(_parse_server(str(name), entry))
        except McpError as error:
            problems.append(str(error))
    return McpConfigs(servers=servers, problems=problems)


def _parse_server(name: str, entry: Any) -> McpServerConfig:
    server_name = name.strip()
    if not server_name:
        raise McpError("MCP 服务器名字不能为空")
    if not isinstance(entry, dict):
        raise McpError(f"MCP 服务器 {server_name} 的配置要是一个对象")
    command = _optional_expanded_text(entry.get("command"))
    url = _optional_expanded_text(entry.get("url"))
    if command and url:
        raise McpError(f"MCP 服务器 {server_name} 同时配了 command 和 url，只能选一种传输方式")
    if not command and not url:
        raise McpError(f"MCP 服务器 {server_name} 没配 command（stdio）也没配 url（HTTP）")
    return McpServerConfig(
        name=server_name,
        command=command,
        args=[_expand_everywhere(str(item)) for item in _string_list(entry.get("args"))],
        env={str(key): _expand_everywhere(str(value)) for key, value in _dict_of_strings(entry.get("env"), server_name).items()},
        cwd=_optional_expanded_text(entry.get("cwd")),
        url=url,
        headers={str(key): _expand_everywhere(str(value)) for key, value in _dict_of_strings(entry.get("headers"), server_name).items()},
        timeout_seconds=_timeout_seconds(entry.get("timeout_seconds"), server_name),
        enabled=_boolean(entry.get("enabled"), default=True),
    )


def _optional_expanded_text(value: Any) -> str | None:
    if value is None:
        return None
    text = expand_env_value(str(value)).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dict_of_strings(value: Any, server_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpError(f"MCP 服务器 {server_name} 的 env / headers 要是字符串到字符串的对象")
    return {str(key): str(item) for key, item in value.items()}


def _timeout_seconds(value: Any, server_name: str) -> int:
    if value in (None, ""):
        return DEFAULT_CALL_TIMEOUT_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise McpError(f"MCP 服务器 {server_name} 的 timeout_seconds 要是整数：{value}")
    if seconds <= 0:
        raise McpError(f"MCP 服务器 {server_name} 的 timeout_seconds 要大于 0：{value}")
    return seconds


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def config_to_public_dict(config: McpServerConfig) -> dict[str, Any]:
    """发给设置界面的版本：env / headers 只给键名，值不回传（留空表示不改）。"""
    return {
        "name": config.name,
        "transport": config.transport,
        "command": config.command,
        "args": list(config.args),
        "env": {key: "" for key in config.env},
        "url": config.url,
        "headers": {key: "" for key in config.headers},
        "timeout_seconds": config.timeout_seconds,
        "enabled": config.enabled,
    }


def save_mcp_servers(home_dir: Path, entries: list[Any], existing: McpConfigs) -> McpConfigs:
    """把设置界面提交的服务器写成 mcp.json。

    env / headers 里留空的值按同名键保留原值（界面上看不到密钥，留空就是不改）；
    提交里没有的键等于删掉，列表里没有的服务器等于删掉这个服务器。
    """
    existing_by_name = {config.name: config for config in existing.servers}
    payload: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("服务器配置的格式不对")
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            problems.append("有服务器没填名字")
            continue
        old = existing_by_name.get(name)
        transport = entry.get("transport")
        if transport not in ("stdio", "http"):
            transport = "http" if entry.get("url") else "stdio"
        data: dict[str, Any] = {
            "enabled": bool(entry.get("enabled", True)),
            "timeout_seconds": entry.get("timeout_seconds") or DEFAULT_CALL_TIMEOUT_SECONDS,
        }
        if transport == "http":
            data["url"] = str(entry.get("url") or "").strip()
            data["headers"] = _merge_secret_values(entry.get("headers"), old.headers if old else {})
        else:
            data["command"] = str(entry.get("command") or "").strip()
            arguments = entry.get("args")
            data["args"] = [str(item) for item in arguments] if isinstance(arguments, list) else []
            data["env"] = _merge_secret_values(entry.get("env"), old.env if old else {})
        try:
            _parse_server(name, data)
        except McpError as error:
            problems.append(str(error))
            continue
        payload[name] = data
    if problems:
        # 有问题的配置不落盘：别把现有配置写坏
        raise McpError("；".join(problems))
    path = mcp_config_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"mcpServers": payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    # 里面有密钥 / token，只给自己读写
    temporary.chmod(0o600)
    temporary.replace(path)
    return load_mcp_servers(home_dir)


def _merge_secret_values(submitted: Any, previous: dict[str, str]) -> dict[str, str]:
    """设置界面提交的 env / headers：留空的按同名键沿用原来的值（和模型密钥一个逻辑）。"""
    if not isinstance(submitted, dict):
        return dict(previous)
    merged: dict[str, str] = {}
    for key, value in submitted.items():
        name = str(key)
        text = str(value)
        merged[name] = previous.get(name, "") if text == "" else text
    return merged
