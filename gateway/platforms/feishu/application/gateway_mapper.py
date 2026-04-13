"""
Gateway mapper — domain ↔ framework boundary translation.

Converts ``FeishuMessage`` domain objects into the framework's
``MessageEvent`` dataclass, and maps content types to ``MessageType``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.feishu.domain.content import (
    AudioContent,
    FileContent,
    ImageContent,
    VideoContent,
)
from gateway.platforms.feishu.domain.models import FeishuMessage


# Map content kind → framework MessageType
_CONTENT_KIND_TO_MESSAGE_TYPE = {
    "text": MessageType.TEXT,
    "image": MessageType.PHOTO,
    "file": MessageType.DOCUMENT,
    "audio": MessageType.AUDIO,
    "video": MessageType.VIDEO,
    "sticker": MessageType.STICKER,
    "interactive": MessageType.TEXT,
    "merge_forward": MessageType.TEXT,
    "unsupported": MessageType.TEXT,
}


def domain_to_message_type(message: FeishuMessage) -> MessageType:
    """Map a domain message's content kind to the framework ``MessageType``."""
    return _CONTENT_KIND_TO_MESSAGE_TYPE.get(
        message.content.kind, MessageType.TEXT,
    )


def domain_to_message_event(
    message: FeishuMessage,
    *,
    text: str,
    message_type: MessageType,
    source: object,
    sender_name: Optional[str] = None,
    media_urls: Optional[List[str]] = None,
    media_types: Optional[List[str]] = None,
    raw_message: object = None,
) -> MessageEvent:
    """Build a framework ``MessageEvent`` from a domain ``FeishuMessage``.

    Parameters that require IO (``sender_name``, ``media_urls``) are passed
    in from the caller — the mapper itself is pure.
    """
    timestamp = message.created_at or datetime.now(tz=timezone.utc)

    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source,
        message_id=str(message.message_id),
        raw_message=raw_message if raw_message is not None else (
            message.model_dump(mode="json") if hasattr(message, "model_dump") else None
        ),
        media_urls=media_urls or [],
        media_types=media_types or [],
        timestamp=timestamp,
    )
