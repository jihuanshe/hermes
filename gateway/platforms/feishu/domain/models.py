"""
Feishu domain aggregate — the core ``FeishuMessage`` model.

This is the canonical representation of an inbound Feishu message after
it has passed through the anti-corruption layer.  All domain services
operate on this model, never on raw dicts or CLI DTOs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from gateway.platforms.feishu.domain.content import MessageContent
from gateway.platforms.feishu.domain.value_objects import (
    ConversationRef,
    Mention,
    MessageId,
    Sender,
)


class FeishuMessage(BaseModel):
    """A fully parsed, domain-level Feishu inbound message.

    Constructed by the ACL mapper from raw CLI event data.
    Consumed by domain services (dedup, policy) and the application service.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: MessageId
    conversation: ConversationRef
    sender: Sender
    content: MessageContent
    created_at: Optional[datetime] = None
    create_time_ms: Optional[int] = None
    mentions: tuple[Mention, ...] = ()
    raw_content: str = ""
