"""Fake Telegram Bot API server for integration tests (T024).

Stdlib only (``http.server`` + ``threading``). Binds loopback ONLY on an
ephemeral port and serves the minimal Bot API surface the integration
tests need:

- ``POST /bot<token>/getMe`` -> canned bot identity.
- ``POST /bot<token>/sendPhoto`` -> incrementing ``message_id``.
- ``POST /bot<token>/sendMessage`` -> incrementing ``message_id``.

Per-method scripted response queues (``(status, payload)`` tuples) let
tests drive retry paths (429/5xx) deterministically. Every request is
tracked (method, header-presence flag, body length); the bot token is
never stored outside the request path itself and ``Authorization`` is
never recorded (the client sends none).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_PHOTO_METHODS = ("sendPhoto", "sendMediaGroup", "sendMessage")


class FakeTelegramServer:
    """Loopback-only fake Telegram server (ephemeral port)."""

    def __init__(
        self,
        token: str = "TEST_TOKEN_xxx",
        host: str = "127.0.0.1",
        expected_chat_id: str = "-100123",
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"refusing to bind non-loopback host {host!r}; "
                "the fake Telegram server binds loopback only"
            )
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        self._token = token
        self._host = host
        self._expected_chat_id = expected_chat_id
        self._lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []
        self._errors: list[str] = []
        self._scripts: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._next_message_id = 1
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port = 0

    # -- lifecycle -----------------------------------------------------

    @property
    def port(self) -> int:
        """Ephemeral port assigned at bind time (0 before start)."""
        return self._port

    @property
    def base_url(self) -> str:
        """Base URL for ``TelegramClient(base_url=...)``."""
        return f"http://{self._host}:{self._port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Snapshot of tracked requests (method, header flag, body length)."""
        with self._lock:
            return [dict(entry) for entry in self._requests]

    @property
    def route_errors(self) -> list[str]:
        """Payload-assertion failures recorded without crashing the handler."""
        with self._lock:
            return list(self._errors)

    def requests_for(self, method: str) -> list[dict[str, Any]]:
        """Tracked requests for one Telegram method (e.g. ``"sendPhoto"``)."""
        with self._lock:
            return [dict(e) for e in self._requests if e["method"] == method]

    def queue(self, method: str, status: int, payload: dict[str, Any]) -> None:
        """Append one scripted ``(status, payload)`` response for ``method``."""
        with self._lock:
            self._scripts.setdefault(method, []).append((status, payload))

    def script(self, method: str, responses: list[tuple[int, dict[str, Any]]]) -> None:
        """Replace the scripted response queue for ``method``."""
        with self._lock:
            self._scripts[method] = list(responses)

    def start(self) -> FakeTelegramServer:
        """Bind 127.0.0.1:0 and serve in a daemon thread. Returns self."""
        if self._httpd is not None:
            return self
        server = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output clean
                pass

            def do_POST(self) -> None:
                try:
                    server._handle(self)
                except Exception as exc:  # never crash the loop on one bad hit
                    server._record_error(f"handler error: {type(exc).__name__}")
                    self._send(500, {"ok": False, "error_code": 500})

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # client only POSTs; be explicit
                self._send(405, {"ok": False, "error_code": 405})

        httpd = ThreadingHTTPServer((self._host, 0), _Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self._port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="fake-telegram", daemon=True
        )
        self._thread.start()
        return self

    def shutdown(self) -> None:
        """Clean shutdown: shutdown() + server_close() + thread join."""
        httpd, thread = self._httpd, self._thread
        self._httpd, self._thread = None, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=10)

    def __enter__(self) -> FakeTelegramServer:
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.shutdown()

    # -- internals -----------------------------------------------------

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)

    def _chat_echo(self) -> Any:
        try:
            return int(self._expected_chat_id)
        except (TypeError, ValueError):
            return self._expected_chat_id

    def _default_success(self, method: str) -> tuple[int, dict[str, Any]]:
        if method == "getMe":
            return 200, {
                "ok": True,
                "result": {
                    "id": 123,
                    "username": "testbot",
                    "first_name": "Test",
                },
            }
        if method in ("sendPhoto", "sendMessage"):
            with self._lock:
                message_id = self._next_message_id
                self._next_message_id += 1
            return 200, {
                "ok": True,
                "result": {"message_id": message_id, "chat": {"id": self._chat_echo()}},
            }
        return 404, {"ok": False, "error_code": 404, "description": f"unknown method {method}"}

    def _pop_scripted(self, method: str) -> tuple[int, dict[str, Any]] | None:
        with self._lock:
            queue = self._scripts.get(method)
            if queue:
                return queue.pop(0)
        return None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        raw_length = handler.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        body = handler.rfile.read(length) if length > 0 else b""

        prefix = f"/bot{self._token}/"
        path = handler.path
        if path.startswith(prefix):
            method = path[len(prefix):].split("?", 1)[0].split("/", 1)[0]
            authorized = True
        else:
            method = "unknown"
            authorized = False

        found_chat: bool | None = None
        if authorized and method in _PHOTO_METHODS and self._expected_chat_id:
            found_chat = self._expected_chat_id.encode("utf-8") in body
            if not found_chat:
                self._record_error(
                    f"{method}: expected chat_id bytes missing from body "
                    f"(body_length={len(body)})"
                )

        with self._lock:
            self._requests.append(
                {
                    "method": method,
                    "content_type_present": handler.headers.get("Content-Type") is not None,
                    "body_length": len(body),
                    "found_chat_id": found_chat,
                }
            )

        if not authorized:
            handler._send(404, {"ok": False, "error_code": 404})  # type: ignore[attr-defined]
            return
        scripted = self._pop_scripted(method)
        if scripted is not None:
            status, payload = scripted
            handler._send(status, payload)  # type: ignore[attr-defined]
            return
        status, payload = self._default_success(method)
        handler._send(status, payload)  # type: ignore[attr-defined]
