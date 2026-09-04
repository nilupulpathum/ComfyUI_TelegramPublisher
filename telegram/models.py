"""Pure data models for the Telegram integration layer. No I/O here."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BotInfo:
    id: int
    username: str | None
    first_name: str | None


@dataclass(frozen=True)
class SendPhotoResult:
    message_id: int
    chat_id: str


@dataclass(frozen=True)
class SendMessageResult:
    message_id: int
    chat_id: str


@dataclass(frozen=True)
class Update:
    """One parsed incoming ``getUpdates`` message update.

    ``chat_id``/``from_id`` are strings end-to-end (FR-002); ``from_id``
    is None when the message carries no sender id.
    """

    update_id: int
    chat_id: str
    text: str
    from_id: str | None


def parse_update(raw: object) -> Update | None:
    """Parse a raw ``getUpdates`` entry into an :class:`Update`.

    Accepts only dicts shaped like ``{"update_id": int, "message":
    {"chat": {"id": ...}, "text": str}}``. Anything else (edited
    messages, callbacks, channel posts, missing/non-string text, ...)
    returns None. Never raises: malformed input yields None.
    """
    try:
        if not isinstance(raw, dict):
            return None
        update_id = raw.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return None
        message = raw.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_id_raw = chat.get("id")
        if isinstance(chat_id_raw, bool) or not isinstance(
            chat_id_raw, (int, str)
        ):
            return None
        chat_id = str(chat_id_raw)
        if not chat_id.strip():
            return None
        from_id: str | None = None
        sender = message.get("from")
        if isinstance(sender, dict) and "id" in sender:
            sender_id = sender.get("id")
            if isinstance(sender_id, bool) or not isinstance(
                sender_id, (int, str)
            ):
                return None
            from_id = str(sender_id)
        return Update(
            update_id=update_id,
            chat_id=chat_id,
            text=text,
            from_id=from_id,
        )
    except Exception:
        return None
