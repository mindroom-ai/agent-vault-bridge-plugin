# ruff: noqa: E402
from __future__ import annotations

import json
import socket
import threading
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from agent_vault_bridge_test_import.adapter import start_adapter
from local.broker_smoke.servers import start_fake_agent_vault, start_header_echo


def _fetch(url: str, *, proxy_url: str | None = None) -> dict[str, object]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else {}),
    )
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_adapter_brokers_hidden_url_without_exposing_session_token() -> None:
    with (
        start_header_echo() as upstream,
        start_fake_agent_vault(
            required_proxy_token="adapter-session",
            injected_authorization="Bearer fake-secret",
        ) as fake_vault,
        start_adapter(
            upstream_proxy_url=fake_vault.proxy_url,
            session_token="adapter-session",
        ) as adapter,
    ):
        data = _fetch(upstream.url("/headers"), proxy_url=adapter.proxy_url)

    headers = data["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer fake-secret"
    assert "proxy-authorization" not in headers


def test_fake_agent_vault_rejects_requests_without_proxy_authorization() -> None:
    with (
        start_header_echo() as upstream,
        start_fake_agent_vault(
            required_proxy_token="adapter-session",
            injected_authorization="Bearer fake-secret",
        ) as fake_vault,
    ):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _fetch(upstream.url("/headers"), proxy_url=fake_vault.proxy_url)

    assert exc_info.value.code == 407


def test_adapter_forwards_connect_proxy_authorization() -> None:
    seen_headers: dict[str, str] = {}

    class ConnectProxyHandler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:  # noqa: N802
            seen_headers.update({key.lower(): value for key, value in self.headers.items()})
            self.send_response(200, "Connection Established")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    fake_proxy = ThreadingHTTPServer(("127.0.0.1", 0), ConnectProxyHandler)
    fake_proxy_thread = threading.Thread(target=fake_proxy.serve_forever, daemon=True)
    fake_proxy_thread.start()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.server_address[1]}"

    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
                response = client.recv(1024)
    finally:
        fake_proxy.shutdown()
        fake_proxy.server_close()
        fake_proxy_thread.join(timeout=5)

    assert response.startswith(b"HTTP/1.0 200")
    assert seen_headers["proxy-authorization"] == "Bearer adapter-session"


def test_adapter_dechunks_post_body_before_forwarding_to_agent_vault() -> None:
    seen: dict[str, object] = {}

    class ChunkedProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["body"] = body
            seen["headers"] = {key.lower(): value for key, value in self.headers.items()}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    fake_proxy = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedProxyHandler)
    fake_proxy_thread = threading.Thread(target=fake_proxy.serve_forever, daemon=True)
    fake_proxy_thread.start()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.server_address[1]}"

    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.sendall(
                    b"POST http://example.invalid/upload HTTP/1.1\r\n"
                    b"Host: example.invalid\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                    b"5\r\nhello\r\n"
                    b"6\r\n world\r\n"
                    b"0\r\n\r\n",
                )
                response = client.recv(1024)
    finally:
        fake_proxy.shutdown()
        fake_proxy.server_close()
        fake_proxy_thread.join(timeout=5)

    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert response.startswith(b"HTTP/1.0 200")
    assert seen["body"] == b"hello world"
    assert headers["content-length"] == "11"
    assert "transfer-encoding" not in headers
    assert headers["proxy-authorization"] == "Bearer adapter-session"
