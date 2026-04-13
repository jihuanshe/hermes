"""
Anti-corruption layer DTOs for lark-cli compact NDJSON events.

These models are **tolerant** (``extra="ignore"``) because lark-cli may
add or change fields between versions.  We only parse the fields we need,
and let everything else pass through silently.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class _CliDTO(BaseModel):
    """Base for all CLI DTOs — tolerant of unknown fields."""

    model_config = ConfigDict(extra="ignore")


class CliCompactEventDTO(_CliDTO):
    """One NDJSON line from ``lark-cli event +subscribe --compact``.

    Field names match the lark-cli compact output format exactly.
    All fields are optional because the compact format is not guaranteed
    to include every field for every event type.
    """

    # Event metadata
    type: str = ""
    id: Optional[str] = None  # event ID (fallback for message_id)

    # Message fields
    message_id: Optional[str] = None
    chat_id: str = ""
    chat_type: str = "p2p"
    message_type: str = "text"
    content: str = ""
    sender_id: str = ""

    # Timestamps (string-encoded millisecond epoch)
    create_time: Optional[str] = None
    timestamp: Optional[str] = None

    # Optional fields
    thread_id: Optional[str] = None
    mentions: Optional[list[dict[str, Any]]] = None

    @property
    def effective_message_id(self) -> str:
        """Return ``message_id`` if present, fall back to ``id``."""
        return self.message_id or self.id or ""

    @property
    def create_time_ms(self) -> int | None:
        """Parse the create_time string to an integer (ms epoch).

        Returns ``None`` if missing or unparseable.
        """
        raw = self.create_time or self.timestamp
        if not raw:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None
