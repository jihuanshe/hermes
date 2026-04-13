"""
Feishu domain services — pure business rules with no IO.

- ``InboundMessagePolicy``: decides whether a message should be processed
  (self-message filtering, group @mention gating).
- ``dedup_identity_from_message()``: bridges ``FeishuMessage`` to the
  dedup service's ``FeishuMessageIdentity``.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from gateway.platforms.feishu.domain.models import FeishuMessage
from gateway.platforms.feishu.domain.value_objects import BotIdentity
from gateway.platforms.feishu_dedup import FeishuMessageIdentity


# ---------------------------------------------------------------------------
# Inbound message policy
# ---------------------------------------------------------------------------

class RejectReason(str, Enum):
    """Why a message was rejected by the inbound policy."""

    WRONG_EVENT_TYPE = "wrong_event_type"
    MISSING_MESSAGE_ID = "missing_message_id"
    SELF_MESSAGE = "self_message"
    GROUP_NO_MENTION = "group_no_mention"


class PolicyResult(BaseModel):
    """Outcome of an inbound policy check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_process: bool
    reject_reason: Optional[RejectReason] = None


class InboundMessagePolicy:
    """Decides whether an inbound message should be processed.

    Encapsulates two rules:

    1. **Self-message filter** — drop messages from the bot itself
       to prevent reply loops.
    2. **Group mention gate** — in group chats, only process messages
       that @mention the bot (by open_id or display name).

    Stateless. Safe to share across calls.
    """

    def __init__(self, bot: BotIdentity) -> None:
        self._bot = bot

    def evaluate(self, message: FeishuMessage) -> PolicyResult:
        """Check whether *message* should be processed.

        Returns a ``PolicyResult`` with ``should_process=True`` if the
        message passes all policy checks.
        """
        # Rule 1: self-message filter
        if self._bot.is_sender(message.sender.open_id):
            return PolicyResult(
                should_process=False,
                reject_reason=RejectReason.SELF_MESSAGE,
            )

        # Rule 2: group mention gate
        if message.conversation.chat_type == "group":
            if not self._is_bot_mentioned(message):
                return PolicyResult(
                    should_process=False,
                    reject_reason=RejectReason.GROUP_NO_MENTION,
                )

        return PolicyResult(should_process=True)

    def _is_bot_mentioned(self, message: FeishuMessage) -> bool:
        """Check if the bot is @mentioned in the message.

        Detection strategy (in order):

        1. Check ``mentions`` tuple for bot's open_id
        2. Check raw content for bot's open_id string
        3. Check raw content for ``@BotName`` (lark-cli compact mode)
        """
        # Check structured mentions
        for mention in message.mentions:
            if mention.is_bot:
                return True
            if self._bot.open_id and mention.user_id == self._bot.open_id:
                return True

        content = message.raw_content

        # Check for bot open_id in content text
        if self._bot.open_id and self._bot.open_id in content:
            return True

        # Fallback: @BotName in plain text (lark-cli compact mode)
        if self._bot.name and f"@{self._bot.name}" in content:
            return True

        return False


# ---------------------------------------------------------------------------
# Dedup bridge
# ---------------------------------------------------------------------------

def dedup_identity_from_message(message: FeishuMessage) -> FeishuMessageIdentity:
    """Extract the dedup identity from a domain message.

    This bridges the domain layer to the dedup service without
    leaking dedup internals into the domain model.
    """
    return FeishuMessageIdentity(
        message_id=str(message.message_id),
        sender_id=message.sender.open_id,
        content=message.raw_content,
        create_time_ms=message.create_time_ms,
    )
