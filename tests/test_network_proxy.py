"""沙箱联网白名单：域名匹配和本地代理的放行 / 拒绝。全部走本机回环，不连外网。"""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from pocketagent.safety.network_proxy import (
    SandboxNetworkProxy,
    host_allowed,
    split_allowlist_entry,
)


class AllowlistTest(unittest.TestCase):
    def test_exact_wildcard_and_port_entries(self) -> None:
        entries = ["pypi.org", "*.github.com", "registry.npmjs.org:443"]

        self.assertTrue(host_allowed("pypi.org", 443, entries))
        self.assertTrue(host_allowed("PyPI.org.", 80, entries))
        self.assertTrue(host_allowed("github.com", 443, entries))
        self.assertTrue(host_allowed("api.github.com", 443, entries))
        self.assertFalse(host_allowed("evilgithub.com", 443, entries))
        self.assertFalse(host_allowed("example.com", 443, entries))
        self.assertTrue(host_allowed("registry.npmjs.org", 443, entries))
        self.assertFalse(host_allowed("registry.npmjs.org", 80, entries))
        self.assertTrue(host_allowed("anything.example", 22, ["*"]))
        self.assertFalse(host_allowed("pypi.org", 443, []))

    def test_entries_can_carry_a_port(self) -> None:
        self.assertEqual(split_allowlist_entry("example.com:443"), ("example.com", 443))
        self.assertEqual(split_allowlist_entry("example.com"), ("example.com", None))
        self.assertEqual(split_allowlist_entry("[::1]:80"), ("::1", 80))
        self.assertEqual(split_allowlist_entry("*.Example.com"), ("*.example.com", None))


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"pocketagent-proxy-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def read_until_close(client: socket.socket) -> bytes:
    chunks = []
    while True:
        data = client.recv(65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def send_proxy_request(proxy_port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as client:
        client.sendall(request)
        return read_until_close(client)


class NetworkProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = HTTPServer(("127.0.0.1", 0), _OkHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.proxy = SandboxNetworkProxy(["127.0.0.1"])
        self.addCleanup(self.proxy.close)

    def tearDown(self) -> None:
        self.upstream.shutdown()
        self.upstream.server_close()

    def test_plain_http_is_forwarded(self) -> None:
        upstream_port = self.upstream.server_address[1]
        request = (
            f"GET http://127.0.0.1:{upstream_port}/ HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{upstream_port}\r\n\r\n"
        ).encode()

        response = send_proxy_request(self.proxy.port, request)

        self.assertTrue(response.startswith(b"HTTP/1."), response[:100])
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"pocketagent-proxy-ok", response)

    def test_hosts_outside_the_allowlist_are_blocked(self) -> None:
        response = send_proxy_request(
            self.proxy.port,
            b"GET http://blocked.example/ HTTP/1.1\r\nHost: blocked.example\r\n\r\n",
        )

        self.assertTrue(response.startswith(b"HTTP/1.1 403"), response[:100])
        self.assertIn("白名单".encode("utf-8"), response)

    def test_connect_tunnel_is_allowed_only_for_listed_hosts(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        upstream_port = listener.getsockname()[1]

        def serve_once() -> None:
            connection, _ = listener.accept()
            connection.sendall(b"tunnel-ok")
            connection.close()

        thread = threading.Thread(target=serve_once, daemon=True)
        thread.start()
        allowed = send_proxy_request(
            self.proxy.port, f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode()
        )
        denied = send_proxy_request(
            self.proxy.port, b"CONNECT blocked.example:443 HTTP/1.1\r\n\r\n"
        )

        self.assertTrue(allowed.startswith(b"HTTP/1.1 200"), allowed[:100])
        self.assertIn(b"tunnel-ok", allowed)
        self.assertTrue(denied.startswith(b"HTTP/1.1 403"), denied[:100])

    def test_environment_points_at_the_proxy(self) -> None:
        environment = self.proxy.environment()

        self.assertEqual(environment["HTTPS_PROXY"], f"http://127.0.0.1:{self.proxy.port}")
        self.assertEqual(environment["http_proxy"], environment["HTTPS_PROXY"])
        self.assertIn("localhost", environment["NO_PROXY"])
