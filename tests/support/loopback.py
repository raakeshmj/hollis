"""A real HTTP(S) server on 127.0.0.1 that speaks both protocols, for end-to-end network tests.

Unlike the mock transports, requests cross a real socket (and TLS, when enabled), so this
exercises the client factory, certificate verification and the SDK's real HTTP path.
"""

import json
import ssl
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.support.anthropic import fixture as anthropic_fixture
from tests.support.openai_chat import fixture as openai_fixture


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class LoopbackServer:
    port: int
    scheme: str
    requests: list[Recorded] = field(default_factory=list[Recorded])
    status: int = 200
    """Override to make every response an error with this status."""

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://localhost:{self.port}"


def _handler(server: LoopbackServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # http.server naming
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            server.requests.append(
                Recorded(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            if server.status != 200:
                payload: dict[str, Any] = {
                    "error": {"type": "not_found_error", "message": "model not found"}
                }
            elif self.path.endswith("/chat/completions"):
                payload = openai_fixture("openai_reference.json") | {"model": body.get("model")}
            elif self.path.endswith("/messages"):
                payload = anthropic_fixture("message_thinking_and_text.json")
            else:
                payload = {"error": {"message": f"no route {self.path}"}}
            data = json.dumps(payload).encode()
            self.send_response(server.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("request-id", "req_loopback")
            self.send_header("x-request-id", "req_loopback")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return None

    return Handler


@contextmanager
def loopback_server(tls_context: ssl.SSLContext | None = None) -> Generator[LoopbackServer]:
    state = LoopbackServer(port=0, scheme="https" if tls_context else "http")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    if tls_context is not None:
        httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
    state.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
