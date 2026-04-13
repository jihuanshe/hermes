"""
Feishu domain value objects — immutable, validated identity types.

These wrap raw string identifiers with semantic meaning and type safety.
They are the shared vocabulary of the Feishu bounded context.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _DomainModel(BaseModel):
    """Base for all domain value objects — frozen + strict."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Identity types
# ---------------------------------------------------------------------------

class MessageId(_DomainModel):
    """Feishu message identifier (e.g. ``om_xxx``)."""

    value: str

    def __str__(self) -> str:
        return self.value

    def __bool__(self) -> bool:
        return bool(self.value)


class ChatId(_DomainModel):
    """Feishu chat/conversation identifier (e.g. ``oc_xxx``)."""

    value: str

    def __str__(self) -> str:
        return self.value

    def __bool__(self) -> bool:
        return bool(self.value)


class SenderId(_DomainModel):
    """Feishu user identifier (open_id by default)."""

    value: str
    kind: Literal["open_id", "user_id", "union_id"] = "open_id"

    def __str__(self) -> str:
        return self.value

    def __bool__(self) -> bool:
        return bool(self.value)


# ---------------------------------------------------------------------------
# Composite value objects
# ---------------------------------------------------------------------------

class BotIdentity(_DomainModel):
    """The bot's own identity — used for self-message filtering and @mention detection."""

    open_id: str = ""
    name: str = ""

    def is_sender(self, sender_id: str) -> bool:
        """Check if a sender_id matches this bot."""
        return bool(self.open_id) and sender_id == self.open_id


class ConversationRef(_DomainModel):
    """Reference to a conversation — enough context for routing and session isolation.

    ``thread_id`` supports future topic/thread-based session isolation.
    """

    chat_id: ChatId
    chat_type: Literal["dm", "group"] = "dm"
    thread_id: Optional[str] = None


class Mention(_DomainModel):
    """A user @mentioned in a message."""

    user_id: str = ""
    name: str = ""
    is_bot: bool = False


class Sender(_DomainModel):
    """Who sent the message."""

    sender_id: SenderId
    display_name: Optional[str] = None

    @property
    def open_id(self) -> str:
        return self.sender_id.value


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def ms_epoch_to_datetime(ms: int | None) -> datetime | None:
    """Convert a millisecond-epoch timestamp to a UTC datetime.

    Returns ``None`` if the input is ``None`` or unparseable.
    """
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
