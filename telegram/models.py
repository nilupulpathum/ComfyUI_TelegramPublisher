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
