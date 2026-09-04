"""Minimal Telegram Bot API client over stdlib ``urllib`` (direct Bot API).

The token travels only in the HTTPS URL path, never in exception
messages or logs. ``chat_id`` stays a string end-to-end (FR-002).

``transport`` is an injectable callable::

    (url: str, *, files, data: dict, timeout: float) -> tuple[int, dict]

returning ``(http_status, parsed_json)``. Tests inject fakes so they
never touch the network. The default transport uses ``urllib`` with a
default SSL context (TLS always verified) and the configured timeout
for both connect and read.

Timeout note: ``urllib`` applies a single ``timeout`` value to the whole
request (connect and read share it); there is no granular connect/read
split. Pass ``timeout=`` per call to override the client default for
that call only. Revisit if granular control is ever needed.
"""

from __future__ import annotations

import json
import mimetypes
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any, Callable

from .errors import (
    AuthenticationError,
    DestinationError,
    EncodingError,
    PermanentTelegramError,
    RateLimitError,
    TelegramPublisherError,
    TransientTelegramError,
    redact,
)
from .logging import get_logger
from .models import BotInfo, SendMessageResult, SendPhotoResult

if TYPE_CHECKING:
    # Annotation-only: the telegram layer must not depend on the service
    # layer at runtime, so the policy is used structurally (``.run(fn)``).
    from services.retry import RetryPolicy

API_BASE = "https://api.telegram.org"

_logger = get_logger(__name__)

# files: field name -> (filename, content bytes, content type), or None.
Files = dict[str, tuple[str, bytes, str]] | None
Transport = Callable[..., tuple[int, Any]]


def _urllib_transport(
    url: str, *, files: Files = None, data: dict | None = None, timeout: float = 30.0
) -> tuple[int, Any]:
    """Default transport: POST via urllib, return (http_status, parsed_json)."""
    data = data or {}
    if files:
        boundary = uuid.uuid4().hex
        body, content_type = _encode_multipart(files, data, boundary)
        headers = {"Content-Type": content_type}
    else:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    context = ssl.create_default_context()  # TLS verification always on.
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # HTTP error statuses still carry a Telegram JSON envelope; read it.
        try:
            raw = exc.read()
        except Exception:
            raise TransientTelegramError(
                f"Telegram request failed with HTTP {exc.code}"
            ) from None
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        raise TransientTelegramError(
            f"Telegram request failed: {redact(type(exc).__name__)}"
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PermanentTelegramError("Telegram returned malformed JSON") from exc
    return status, payload


def _encode_multipart(
    files: dict[str, tuple[str, bytes, str]], data: dict, boundary: str
) -> tuple[bytes, str]:
    """Encode form fields + files as multipart/form-data."""
    buf = bytearray()

    def _field(name: str, value: Any) -> None:
        buf.extend(f"--{boundary}\r\n".encode("utf-8"))
        buf.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        buf.extend(str(value).encode("utf-8"))
        buf.extend(b"\r\n")

    for key, value in data.items():
        _field(key, value)
    for field, (filename, content, content_type) in files.items():
        buf.extend(f"--{boundary}\r\n".encode("utf-8"))
        buf.extend(
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(
                "utf-8"
            )
        )
        buf.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        buf.extend(content)
        buf.extend(b"\r\n")
    buf.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _retry_after(payload: Any) -> int | None:
    """Extract parameters.retry_after from a Telegram envelope, if present."""
    if isinstance(payload, dict):
        params = payload.get("parameters")
        if isinstance(params, dict):
            value = params.get("retry_after")
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return int(value)
    return None


def _describe(payload: Any) -> str:
    """Human-readable description from an envelope, without secrets."""
    if isinstance(payload, dict):
        desc = payload.get("description")
        if isinstance(desc, str) and desc.strip():
            return redact(desc.strip())
    return "Telegram request failed"


class TelegramClient:
    """Thin wrapper around the Telegram Bot API."""

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
        base_url: str = API_BASE,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        from .errors import ConfigurationError

        if not isinstance(token, str) or not token.strip():
            raise ConfigurationError("Telegram bot token is missing or empty")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigurationError("Telegram API base_url must be a non-empty string")
        # Tokens travel in the URL path: plaintext HTTP is only acceptable
        # for loopback test servers (T024). Refuse anything else loudly.
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme == "http" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise ConfigurationError(
                "refusing to send the bot token over plaintext HTTP "
                "to a non-loopback host"
            )
        self._token = token
        self._timeout = float(timeout)
        self._base_url = base_url.rstrip("/")
        self._transport: Transport = transport or _urllib_transport
        # None (default) = single attempt, preserving historic behavior.
        self._retry_policy = retry_policy

    def _url(self, method: str) -> str:
        # Token goes only into the URL path.
        return f"{self._base_url}/bot{self._token}/{method}"

    def _call(
        self,
        method: str,
        *,
        files: Files = None,
        data: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Invoke a Bot API method and return the ``result`` payload.

        ``timeout`` overrides the client default for this call only;
        ``None`` falls back to ``self._timeout``. A non-positive timeout
        raises ``ValueError`` before any transport call.

        When a retry policy is set, the whole transport invocation
        (request plus envelope-to-typed-error mapping) is retried;
        caller-side validation and result-model mapping stay outside.
        """
        if timeout is None:
            effective_timeout = self._timeout
        else:
            try:
                effective_timeout = float(timeout)
            except (TypeError, ValueError):
                raise ValueError(
                    "timeout must be a positive number of seconds"
                ) from None
            if not effective_timeout > 0:
                raise ValueError("timeout must be a positive number of seconds")
        url = self._url(method)
        start = time.perf_counter()
        _logger.debug("telegram call attempt method=%s", method)

        def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            _logger.warning(
                "telegram retry method=%s attempt=%s error=%s delay=%.2fs",
                method,
                attempt,
                exc,
                delay,
            )

        def _do_call() -> Any:
            try:
                status, payload = self._transport(
                    url, files=files, data=data or {}, timeout=effective_timeout
                )
            except TelegramPublisherError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                raise TransientTelegramError("Telegram request timed out") from exc
            except (ConnectionError, OSError) as exc:
                raise TransientTelegramError("Telegram connection failed") from exc
            except (ValueError, TypeError) as exc:
                # A fake transport signalling a broken body (e.g. malformed JSON).
                raise PermanentTelegramError("Telegram returned malformed data") from exc
            return self._handle_response(method, status, payload)

        if self._retry_policy is None:
            try:
                result = _do_call()
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                _logger.warning(
                    "telegram call failed method=%s error_type=%s error=%s elapsed_ms=%.1f",
                    method,
                    type(exc).__name__,
                    exc,
                    elapsed_ms,
                )
                raise
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            _logger.debug(
                "telegram call succeeded method=%s elapsed_ms=%.1f",
                method,
                elapsed_ms,
            )
            return result
        try:
            result = self._retry_policy.run(_do_call, on_retry=_on_retry)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            _logger.warning(
                "telegram call failed method=%s error_type=%s error=%s elapsed_ms=%.1f",
                method,
                type(exc).__name__,
                exc,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _logger.debug(
            "telegram call succeeded method=%s elapsed_ms=%.1f",
            method,
            elapsed_ms,
        )
        return result

    @staticmethod
    def _handle_response(method: str, status: Any, payload: Any) -> Any:
        """Map (http_status, envelope) to result or a typed exception."""
        if not isinstance(payload, dict):
            raise PermanentTelegramError("Telegram returned malformed data")
        ok = payload.get("ok")
        if isinstance(status, int):
            if status == 401:
                raise AuthenticationError(f"{method}: invalid bot token")
            if status == 429:
                raise RateLimitError(
                    f"{method}: rate limited",
                    retry_after=_retry_after(payload),
                )
            if status in (400, 403, 404):
                raise DestinationError(f"{method}: {_describe(payload)}")
            if 500 <= status <= 599:
                raise TransientTelegramError(f"{method}: {_describe(payload)}")
            if status != 200:
                raise PermanentTelegramError(f"{method}: {_describe(payload)}")
        # HTTP 200 (or unknown status): fall back to the envelope.
        if ok is True:
            return payload.get("result")
        error_code = payload.get("error_code")
        if error_code == 401:
            raise AuthenticationError(f"{method}: invalid bot token")
        if error_code == 429:
            raise RateLimitError(
                f"{method}: rate limited", retry_after=_retry_after(payload)
            )
        if error_code in (400, 403, 404):
            raise DestinationError(f"{method}: {_describe(payload)}")
        if isinstance(error_code, int) and 500 <= error_code <= 599:
            raise TransientTelegramError(f"{method}: {_describe(payload)}")
        raise PermanentTelegramError(f"{method}: {_describe(payload)}")

    @staticmethod
    def _require_chat_id(chat_id: str) -> str:
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise DestinationError("chat_id must be a non-empty string")
        return chat_id

    # -- public API ----------------------------------------------------

    def validate_token(self, *, timeout: float | None = None) -> BotInfo:
        """Call getMe; HTTP 401 / invalid token -> AuthenticationError."""
        result = self._call("getMe", timeout=timeout)
        if not isinstance(result, dict):
            raise PermanentTelegramError("getMe returned malformed data")
        bot_id = result.get("id")
        if not isinstance(bot_id, int):
            raise PermanentTelegramError("getMe returned malformed data")
        username = result.get("username")
        first_name = result.get("first_name")
        return BotInfo(
            id=bot_id,
            username=username if isinstance(username, str) else None,
            first_name=first_name if isinstance(first_name, str) else None,
        )

    def send_photo(
        self,
        chat_id: str,
        image_bytes: bytes,
        filename: str,
        caption: str | None = None,
        *,
        protect_content: bool = False,
        disable_notification: bool = False,
        timeout: float | None = None,
    ) -> SendPhotoResult:
        """Upload one image via sendPhoto (multipart/form-data)."""
        chat_id = self._require_chat_id(chat_id)
        if not isinstance(image_bytes, (bytes, bytearray)) or len(image_bytes) == 0:
            raise EncodingError("image_bytes must be non-empty bytes")
        if not isinstance(filename, str) or not filename.strip():
            raise EncodingError("filename must be a non-empty string")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files: Files = {"photo": (filename, bytes(image_bytes), content_type)}
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "protect_content": "true" if protect_content else "false",
            "disable_notification": "true" if disable_notification else "false",
        }
        if caption is not None:
            data["caption"] = caption
        result = self._call("sendPhoto", files=files, data=data, timeout=timeout)
        if not isinstance(result, dict):
            raise PermanentTelegramError("sendPhoto returned malformed data")
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise PermanentTelegramError("sendPhoto returned malformed data")
        chat = result.get("chat")
        resolved = chat_id
        if isinstance(chat, dict) and isinstance(chat.get("id"), (int, str)):
            resolved = str(chat["id"])
        return SendPhotoResult(message_id=message_id, chat_id=resolved)

    def send_media_group(
        self,
        chat_id: str,
        media_items: list[tuple[bytes, str]],
        caption: str | None = None,
        *,
        timeout: float | None = None,
    ) -> list[int]:
        """Upload an album via sendMediaGroup. Returns message ids."""
        chat_id = self._require_chat_id(chat_id)
        if not isinstance(media_items, list) or not media_items:
            raise EncodingError("media_items must be a non-empty list")
        files: dict[str, tuple[str, bytes, str]] = {}
        media: list[dict[str, Any]] = []
        for index, item in enumerate(media_items):
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], (bytes, bytearray))
                or len(item[0]) == 0
                or not isinstance(item[1], str)
                or not item[1].strip()
            ):
                raise EncodingError(
                    "each media item must be (non-empty image bytes, filename)"
                )
            content, filename = bytes(item[0]), item[1]
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            field = f"photo{index}"
            files[field] = (filename, content, content_type)
            entry: dict[str, Any] = {"type": "photo", "media": f"attach://{field}"}
            if index == 0 and caption is not None:
                entry["caption"] = caption
            media.append(entry)
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "media": json.dumps(media),
        }
        result = self._call("sendMediaGroup", files=files, data=data, timeout=timeout)
        if not isinstance(result, list) or not all(
            isinstance(m, dict) and isinstance(m.get("message_id"), int)
            for m in result
        ):
            raise PermanentTelegramError("sendMediaGroup returned malformed data")
        return [int(m["message_id"]) for m in result]  # type: ignore[index]

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int | float = 30,
        *,
        client_timeout: float | None = None,
    ) -> list[dict]:
        """Long-poll ``getUpdates``; return the RAW ``result`` list.

        ``offset``/``timeout`` are passed through as Bot API params
        (``timeout`` is the poll window in seconds, 0..60). Entries are
        returned unparsed -- the receiver parses via
        :func:`telegram.models.parse_update` and skips malformed entries
        downstream.

        HTTP timeout rule: long-polling holds the connection open for the
        whole poll window, so the HTTP read timeout must be LONGER than
        ``timeout``. The effective per-call HTTP timeout is the explicit
        ``client_timeout`` override when given, else
        ``max(self._timeout, timeout + 10)``.

        Raises:
            ValueError: If ``timeout`` is not in 0..60, ``offset`` is not
                None/a non-negative int, or ``client_timeout`` is not a
                positive number.
        """
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 <= float(timeout) <= 60
        ):
            raise ValueError("timeout must be a number in 0..60 seconds")
        if offset is not None and (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ValueError("offset must be None or a non-negative int")
        if client_timeout is not None:
            try:
                override = float(client_timeout)
            except (TypeError, ValueError):
                raise ValueError(
                    "client_timeout must be a positive number of seconds"
                ) from None
            if isinstance(client_timeout, bool) or not override > 0:
                raise ValueError(
                    "client_timeout must be a positive number of seconds"
                )
            effective_timeout = override
        else:
            effective_timeout = max(self._timeout, float(timeout) + 10.0)
        data: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            data["offset"] = offset
        result = self._call(
            "getUpdates", data=data, timeout=effective_timeout
        )
        if not isinstance(result, list):
            raise PermanentTelegramError("getUpdates returned malformed data")
        return result

    def send_message(
        self, chat_id: str, text: str, *, timeout: float | None = None
    ) -> SendMessageResult:
        """Send a text message via sendMessage."""
        chat_id = self._require_chat_id(chat_id)
        if not isinstance(text, str) or not text.strip():
            raise EncodingError("text must be a non-empty string")
        result = self._call(
            "sendMessage", data={"chat_id": chat_id, "text": text}, timeout=timeout
        )
        if not isinstance(result, dict):
            raise PermanentTelegramError("sendMessage returned malformed data")
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise PermanentTelegramError("sendMessage returned malformed data")
        chat = result.get("chat")
        resolved = chat_id
        if isinstance(chat, dict) and isinstance(chat.get("id"), (int, str)):
            resolved = str(chat["id"])
        return SendMessageResult(message_id=message_id, chat_id=resolved)
