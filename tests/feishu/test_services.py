"""Tests for Feishu domain services — Step 5 of DDD refactor.

Covers InboundMessagePolicy and dedup_identity_from_message.
"""

import pytest

from gateway.platforms.feishu.domain.content import TextContent, ImageContent
from gateway.platforms.feishu.domain.models import FeishuMessage
from gateway.platforms.feishu.domain.services import (
    InboundMessagePolicy,
    PolicyResult,
    RejectReason,
    dedup_identity_from_message,
)
from gateway.platforms.feishu.domain.value_objects import (
    BotIdentity,
    ChatId,
    ConversationRef,
    Mention,
    MessageId,
    Sender,
    SenderId,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bot(open_id: str = "ou_bot", name: str = "TestBot") -> BotIdentity:
    return BotIdentity(open_id=open_id, name=name)


def _msg(
    sender_id: str = "ou_user1",
    chat_type: str = "dm",
    content_text: str = "Hello",
    raw_content: str = "Hello",
    mentions: tuple = (),
    create_time_ms: int | None = 1700000000000,
) -> FeishuMessage:
    return FeishuMessage(
        message_id=MessageId(value="om_1"),
        conversation=ConversationRef(
            chat_id=ChatId(value="oc_c"),
            chat_type=chat_type,
        ),
        sender=Sender(sender_id=SenderId(value=sender_id)),
        content=TextContent(text=content_text),
        raw_content=raw_content,
        mentions=mentions,
        create_time_ms=create_time_ms,
    )


# ===========================================================================
# InboundMessagePolicy — self-message filtering
# ===========================================================================

class TestSelfMessageFilter:
    def test_bot_message_rejected(self):
        policy = InboundMessagePolicy(_bot(open_id="ou_bot"))
        result = policy.evaluate(_msg(sender_id="ou_bot"))
        assert not result.should_process
        assert result.reject_reason == RejectReason.SELF_MESSAGE

    def test_user_message_accepted(self):
        policy = InboundMessagePolicy(_bot(open_id="ou_bot"))
        result = policy.evaluate(_msg(sender_id="ou_user1"))
        assert result.should_process
        assert result.reject_reason is None

    def test_empty_bot_id_never_matches(self):
        policy = InboundMessagePolicy(_bot(open_id=""))
        result = policy.evaluate(_msg(sender_id="ou_anyone"))
        assert result.should_process


# ===========================================================================
# InboundMessagePolicy — group mention gating
# ===========================================================================

class TestGroupMentionGating:
    def test_dm_always_passes(self):
        policy = InboundMessagePolicy(_bot())
        result = policy.evaluate(_msg(chat_type="dm", raw_content="no mention"))
        assert result.should_process

    def test_group_without_mention_rejected(self):
        policy = InboundMessagePolicy(_bot(open_id="ou_bot", name="Bot"))
        result = policy.evaluate(_msg(
            chat_type="group",
            raw_content="just a regular message",
        ))
        assert not result.should_process
        assert result.reject_reason == RejectReason.GROUP_NO_MENTION

    def test_group_with_open_id_in_content(self):
        policy = InboundMessagePolicy(_bot(open_id="ou_bot"))
        result = policy.evaluate(_msg(
            chat_type="group",
            raw_content="Hey ou_bot check this",
        ))
        assert result.should_process

    def test_group_with_at_bot_name(self):
        policy = InboundMessagePolicy(_bot(name="MyBot"))
        result = policy.evaluate(_msg(
            chat_type="group",
            raw_content="Hey @MyBot what's up",
        ))
        assert result.should_process

    def test_group_with_structured_mention(self):
        policy = InboundMessagePolicy(_bot(open_id="ou_bot"))
        result = policy.evaluate(_msg(
            chat_type="group",
            raw_content="plain text",
            mentions=(Mention(user_id="ou_bot", name="Bot"),),
        ))
        assert result.should_process

    def test_group_with_is_bot_mention(self):
        policy = InboundMessagePolicy(_bot(open_id="ou_bot"))
        result = policy.evaluate(_msg(
            chat_type="group",
            raw_content="plain text",
            mentions=(Mention(user_id="ou_other", is_bot=True),),
        ))
        assert result.should_process

    def test_group_empty_bot_identity(self):
        """With no bot identity configured, group messages without mentions are rejected."""
        policy = InboundMessagePolicy(_bot(open_id="", name=""))
        result = policy.evaluate(_msg(chat_type="group", raw_content="hello"))
        assert not result.should_process


# ===========================================================================
# PolicyResult model
# ===========================================================================

class TestPolicyResult:
    def test_accepted_result(self):
        r = PolicyResult(should_process=True)
        assert r.should_process
        assert r.reject_reason is None

    def test_rejected_result_serializes(self):
        r = PolicyResult(
            should_process=False,
            reject_reason=RejectReason.SELF_MESSAGE,
        )
        data = r.model_dump()
        assert data["reject_reason"] == "self_message"

    def test_frozen(self):
        from pydantic import ValidationError
        r = PolicyResult(should_process=True)
        with pytest.raises(ValidationError):
            r.should_process = False


# ===========================================================================
# dedup_identity_from_message
# ===========================================================================

class TestDedupIdentityBridge:
    def test_extracts_correct_fields(self):
        msg = _msg(
            sender_id="ou_alice",
            raw_content='{"text": "hi"}',
            create_time_ms=1700000000000,
        )
        identity = dedup_identity_from_message(msg)
        assert identity.message_id == "om_1"
        assert identity.sender_id == "ou_alice"
        assert identity.content == '{"text": "hi"}'
        assert identity.create_time_ms == 1700000000000

    def test_none_create_time(self):
        msg = _msg(create_time_ms=None)
        identity = dedup_identity_from_message(msg)
        assert identity.create_time_ms is None

    def test_image_message_uses_raw_content(self):
        """Dedup should use raw_content, not parsed text, for fingerprinting."""
        msg = FeishuMessage(
            message_id=MessageId(value="om_img"),
            conversation=ConversationRef(chat_id=ChatId(value="oc_c")),
            sender=Sender(sender_id=SenderId(value="ou_u")),
            content=ImageContent(image_key="img_k"),
            raw_content='{"image_key": "img_k"}',
        )
        identity = dedup_identity_from_message(msg)
        assert identity.content == '{"image_key": "img_k"}'
