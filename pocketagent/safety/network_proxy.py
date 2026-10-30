"""沙箱联网白名单：放在 PocketAgent 进程里的本地 HTTP 代理。

macOS 的 Seatbelt 只认 `*` 和 `localhost` 两种主机（不接受具体域名 / IP），bubblewrap 只有全开或全关，
所以「按域名放行」只能靠代理：沙箱里只放行到 `localhost:<代理端口>`，命令的 HTTP(S) 请求都转给本地代理，
代理看过目标域名再决定连不连。

白名单写法（大小写不敏感，不写端口表示任何端口）：
- `pypi.org`：只匹配这个域名
- `*.github.com`：匹配 github.com 和它的子域名
- `registry.npmjs.org:443`：只放行 443 端口
"""

import socket
import socketserver
import threading
from collections.abc import Sequence
from urllib.parse import urlsplit

READ_CHUNK_BYTES = 4096
RELAY_CHUNK_BYTES = 64 * 1024
MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 10

STATUS_REASONS = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}

# 代理只监听本机；同一个白名单在进程里只起一个
_PROXIES: dict[tuple[str, ...], "SandboxNetworkProxy"] = {}
_PROXY_LOCK = threading.Lock()


def normalize_host(host: str) -> str:
    text = host.strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text


def split_allowlist_entry(entry: str) -> tuple[str, int | None]:
    """把一条白名单拆成 (域名, 端口)；没写端口就是 None（任何端口）。"""
    text = entry.strip().lower()
    if text.startswith("["):
        closing = text.find("]")
        if closing > 0:
            host = normalize_host(text[1:closing])
            rest = text[closing + 1 :]
            if rest.startswith(":") and rest[1:].isdigit():
                return host, int(rest[1:])
            return host, None
    host, separator, port_text = text.rpartition(":")
    if separator and port_text.isdigit() and ":" not in host:
        return normalize_host(host), int(port_text)
    return normalize_host(text), None


def host_allowed(host: str, port: int, entries: Sequence[str]) -> bool:
    normalized = normalize_host(host)
    if not normalized:
        return False
    for entry in entries:
        entry_host, entry_port = split_allowlist_entry(entry)
        if not entry_host:
            continue
        if entry_port is not None and entry_port != port:
            continue
        if entry_host == "*":
            return True
        if entry_host.startswith("*."):
            base = entry_host[2:]
            if normalized == base or normalized.endswith("." + base):
                return True
            continue
        if normalized == entry_host:
            return True
    return False


def blocked_message(host: str) -> str:
    return (
        f"沙箱联网白名单不允许访问 {host}。"
        "要长期放行，在设置 →「权限与安全」→「命令沙箱」的联网白名单里加上它；"
        "只跑这一次可以改用 outside_sandbox=true。"
    )


def _unique_entries(entries: Sequence[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for entry in entries:
        text = entry.strip().lower()
        if text and text not in unique:
            unique.append(text)
    return tuple(unique)


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, allowlist: tuple[str, ...]) -> None:
        self.allowlist = allowlist
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class SandboxNetworkProxy:
    """按白名单放行的本地代理；实例在 PocketAgent 进程里共享，后台命令也能用。"""

    def __init__(self, entries: Sequence[str]) -> None:
        self.allowlist = _unique_entries(entries)
        self._server = _ProxyServer(self.allowlist)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="pocketagent-sandbox-proxy",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def environment(self) -> dict[str, str]:
        proxy_url = f"http://127.0.0.1:{self.port}"
        no_proxy = "localhost,127.0.0.1,::1"
        return {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "all_proxy": proxy_url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def network_proxy(entries: Sequence[str]) -> SandboxNetworkProxy:
    key = _unique_entries(entries)
    with _PROXY_LOCK:
        proxy = _PROXIES.get(key)
        if proxy is None:
            proxy = SandboxNetworkProxy(key)
            _PROXIES[key] = proxy
        return proxy


def _read_head(client: socket.socket) -> tuple[bytes, bytes] | None:
    """读到空行为止，返回 (头, 已经多读的正文)；头太大或者连不上返回 None。"""
    buffer = b""
    while True:
        for separator in (b"\r\n\r\n", b"\n\n"):
            position = buffer.find(separator)
            if position >= 0:
                return buffer[:position], buffer[position + len(separator) :]
        if len(buffer) > MAX_HEAD_BYTES:
            return None
        try:
            chunk = client.recv(READ_CHUNK_BYTES)
        except OSError:
            return None
        if not chunk:
            return None
        buffer += chunk


def _parse_head(head: bytes) -> tuple[str, str, str, list[tuple[str, str]]] | None:
    lines = head.decode("latin-1").splitlines()
    if not lines:
        return None
    parts = lines[0].split()
    if len(parts) != 3:
        return None
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            return None
        headers.append((name.strip(), value.strip()))
    return parts[0], parts[1], parts[2], headers


def _connect_target(target: str) -> tuple[str, int] | None:
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0:
            return None
        host = target[1:closing]
        rest = target[closing + 1 :]
        if not rest.startswith(":"):
            return None
        port_text = rest[1:]
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            return None
    if not host or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 0 < port < 65536:
        return None
    return host, port


def _http_target(target: str) -> tuple[str, int, str] | None:
    parsed = urlsplit(target)
    if parsed.scheme != "http" or not parsed.hostname:
        return None
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return parsed.hostname, port, path


def _open_upstream(host: str, port: int) -> socket.socket | None:
    try:
        upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
    except OSError:
        return None
    upstream.settimeout(None)
    return upstream


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(RELAY_CHUNK_BYTES)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    try:
        target.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    client_to_upstream = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
    client_to_upstream.start()
    _pump(upstream, client)


def _send_error(client: socket.socket, status: int, message: str) -> None:
    reason = STATUS_REASONS.get(status, "Error")
    body = (message + "\n").encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    try:
        client.sendall(head.encode("latin-1") + body)
    except OSError:
        pass


class _ProxyHandler(socketserver.BaseRequestHandler):
    server: _ProxyServer

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(HEAD_TIMEOUT_SECONDS)
        head_and_rest = _read_head(client)
        if head_and_rest is None:
            _send_error(client, 400, "读不到完整的代理请求")
            return
        head, leftover = head_and_rest
        parsed = _parse_head(head)
        if parsed is None:
            _send_error(client, 400, "代理请求格式不对")
            return
        client.settimeout(None)
        method, target, version, headers = parsed
        if method.upper() == "CONNECT":
            self._handle_connect(client, target, leftover)
        else:
            self._handle_http(client, method, target, version, headers, leftover)

    def _handle_connect(self, client: socket.socket, target: str, leftover: bytes) -> None:
        address = _connect_target(target)
        if address is None:
            _send_error(client, 400, "CONNECT 的目标要写成 host:port")
            return
        host, port = address
        if not host_allowed(host, port, self.server.allowlist):
            _send_error(client, 403, blocked_message(host))
            return
        upstream = _open_upstream(host, port)
        if upstream is None:
            _send_error(client, 502, f"白名单允许 {host}，但连不上它（{port} 端口）")
            return
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if leftover:
                upstream.sendall(leftover)
            _relay(client, upstream)
        except OSError:
            pass
        finally:
            upstream.close()

    def _handle_http(
        self,
        client: socket.socket,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[str, str]],
        leftover: bytes,
    ) -> None:
        parsed = _http_target(target)
        if parsed is None:
            _send_error(client, 400, "只接受 http:// 开头的代理请求；HTTPS 走 CONNECT")
            return
        host, port, path = parsed
        if not host_allowed(host, port, self.server.allowlist):
            _send_error(client, 403, blocked_message(host))
            return
        upstream = _open_upstream(host, port)
        if upstream is None:
            _send_error(client, 502, f"白名单允许 {host}，但连不上它（{port} 端口）")
            return
        try:
            lines = [f"{method} {path} {version}"]
            for name, value in headers:
                if name.lower() in {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}:
                    continue
                lines.append(f"{name}: {value}")
            # 一条连接只转发这一条请求，避免客户端复用连接去访问没校验过的域名
            lines.append("Connection: close")
            request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
            upstream.sendall(request)
            if leftover:
                upstream.sendall(leftover)
            _relay(client, upstream)
        except OSError:
            pass
        finally:
            upstream.close()
