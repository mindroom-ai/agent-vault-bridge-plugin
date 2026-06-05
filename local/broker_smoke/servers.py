from __future__ import annotations

import argparse
import http.client
import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlsplit


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass(slots=True)
class RunningServer:
    httpd: ThreadingHTTPServer
    thread: threading.Thread

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def host(self) -> str:
        return str(self.httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"http://{self.host}:{self.port}{path}"


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def start_header_echo(host: str = "127.0.0.1", port: int = 0) -> RunningServer:
    class HeaderEchoHandler(_QuietHandler):
        def do_GET(self) -> None:
            payload = json.dumps(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                },
                sort_keys=True,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _start_server(HeaderEchoHandler, host=host, port=port)


def start_fake_agent_vault(
    *,
    required_proxy_token: str,
    injected_authorization: str,
    host: str = "127.0.0.1",
    port: int = 0,
) -> RunningServer:
    class FakeAgentVaultHandler(_QuietHandler):
        def do_GET(self) -> None:
            expected = f"Bearer {required_proxy_token}"
            if self.headers.get("Proxy-Authorization") != expected:
                self.send_error(407, "Proxy authorization required")
                return
            _forward_absolute_proxy_request(
                self,
                add_headers={"Authorization": injected_authorization},
            )

    return _start_server(FakeAgentVaultHandler, host=host, port=port)


def start_adapter(
    *,
    upstream_proxy_url: str,
    session_token: str,
    host: str = "127.0.0.1",
    port: int = 0,
) -> RunningServer:
    upstream = urlsplit(upstream_proxy_url)
    if upstream.scheme != "http" or not upstream.hostname:
        msg = "upstream_proxy_url must be an http://host:port URL"
        raise ValueError(msg)
    upstream_port = upstream.port or 80

    class AdapterHandler(_QuietHandler):
        def do_GET(self) -> None:
            _forward_to_proxy(
                self,
                proxy_host=upstream.hostname or "",
                proxy_port=upstream_port,
                add_headers={"Proxy-Authorization": f"Bearer {session_token}"},
            )

    return _start_server(AdapterHandler, host=host, port=port)


def _start_server(handler: type[BaseHTTPRequestHandler], *, host: str, port: int) -> RunningServer:
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return RunningServer(httpd=httpd, thread=thread)


def _forward_to_proxy(
    handler: BaseHTTPRequestHandler,
    *,
    proxy_host: str,
    proxy_port: int,
    add_headers: dict[str, str],
) -> None:
    headers = _forward_headers(handler.headers.items(), add_headers=add_headers)
    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=10)
    try:
        connection.request(handler.command, handler.path, headers=headers)
        response = connection.getresponse()
        _copy_response(handler, response)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        handler.send_error(502, f"Bad Gateway: {exc}")
    finally:
        connection.close()


def _forward_absolute_proxy_request(
    handler: BaseHTTPRequestHandler,
    *,
    add_headers: dict[str, str],
) -> None:
    target = urlsplit(handler.path)
    if target.scheme not in {"http", "https"} or not target.hostname:
        handler.send_error(400, "Expected an absolute proxy URL")
        return

    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    target_port = target.port or (443 if target.scheme == "https" else 80)
    target_path = target.path or "/"
    if target.query:
        target_path = f"{target_path}?{target.query}"

    headers = _forward_headers(handler.headers.items(), add_headers=add_headers)
    headers["Host"] = target.netloc
    connection = connection_class(target.hostname, target_port, timeout=10)
    try:
        connection.request(handler.command, target_path, headers=headers)
        response = connection.getresponse()
        _copy_response(handler, response)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        handler.send_error(502, f"Bad Gateway: {exc}")
    finally:
        connection.close()


def _forward_headers(
    items: Iterable[tuple[str, str]],
    *,
    add_headers: dict[str, str],
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in items:
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        if key in headers:
            headers[key] = f"{headers[key]}, {value}"
        else:
            headers[key] = value
    headers.update(add_headers)
    return headers


def _copy_response(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse) -> None:
    body = response.read()
    handler.send_response(response.status, response.reason)
    for key, value in response.getheaders():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def _serve_forever(server: RunningServer) -> None:
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.__exit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local broker smoke-test servers.")
    subparsers = parser.add_subparsers(dest="role", required=True)

    upstream = subparsers.add_parser("upstream")
    upstream.add_argument("--host", default="0.0.0.0")
    upstream.add_argument("--port", type=int, default=8080)

    fake_vault = subparsers.add_parser("fake-agent-vault")
    fake_vault.add_argument("--host", default="0.0.0.0")
    fake_vault.add_argument("--port", type=int, default=18081)
    fake_vault.add_argument("--required-proxy-token", required=True)
    fake_vault.add_argument("--injected-authorization", required=True)

    adapter = subparsers.add_parser("adapter")
    adapter.add_argument("--host", default="0.0.0.0")
    adapter.add_argument("--port", type=int, default=18080)
    adapter.add_argument("--upstream-proxy-url", required=True)
    adapter.add_argument("--session-token", required=True)

    args = parser.parse_args()
    if args.role == "upstream":
        _serve_forever(start_header_echo(host=args.host, port=args.port))
    elif args.role == "fake-agent-vault":
        _serve_forever(
            start_fake_agent_vault(
                host=args.host,
                port=args.port,
                required_proxy_token=args.required_proxy_token,
                injected_authorization=args.injected_authorization,
            ),
        )
    elif args.role == "adapter":
        _serve_forever(
            start_adapter(
                host=args.host,
                port=args.port,
                upstream_proxy_url=args.upstream_proxy_url,
                session_token=args.session_token,
            ),
        )


if __name__ == "__main__":
    main()
