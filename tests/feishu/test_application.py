"""Tests for Feishu application layer — Step 6 of DDD refactor.

Covers FeishuInboundService (orchestrator) and gateway_mapper.
"""

import time
import pytest

from gateway.platforms.base import MessageType
from gateway.platforms.feishu.acl.cli_mapper import CliToDomainMapper
from gateway.platforms.feishu.application.gateway_mapper import (
    domain_to_message_event,
    domain_to_message_type,
)
from gateway.platforms.feishu.application.service import (
    FeishuInboundService,
    InboundResult,
)
from gateway.platforms.feishu.domain.content import (
    ImageContent,
    TextContent,
    FileContent,
    StickerContent,
)
from gateway.platforms.feishu.domain.models import FeishuMessage
from gateway.platforms.feishu.domain.services import InboundMessagePolicy
from gateway.platforms.feishu.domain.value_objects import (
    BotIdentity,
    ChatId,
    ConversationRef,
    MessageId,
    Sender,
    SenderId,
)
from gateway.platforms.feishu_dedup import FeishuDedupConfig, MessageDeduplicator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recent_ts() -> str:
    return str(int(time.time() * 1000))


def _make_service(
    bot_open_id: str = "ou_bot",
    bot_name: str = "TestBot",
    stale_seconds: int = 300,
) -> FeishuInboundService:
    bot = BotIdentity(open_id=bot_open_id, name=bot_name)
    return FeishuInboundService(
        mapper=CliToDomainMapper(),
        policy=InboundMessagePolicy(bot),
        deduplicator=MessageDeduplicator(
            FeishuDedupConfig(stale_threshold_seconds=stale_seconds),
        ),
    )


def _raw_event(
    message_id: str = "om_1",
    sender_id: str = "ou_user1",
    content: str = "Hello",
    chat_type: str = "p2p",
    message_type: str = "text",
    create_time: str | None = None,
    event_type: str = "im.message.receive_v1",
    **extra,
) -> dict:
    ev = {
        "type": event_type,
        "message_id": message_id,
        "chat_id": "oc_chat1",
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content,
        "sender_id": sender_id,
        "create_time": create_time or _recent_ts(),
    }
    ev.update(extra)
    return ev


# ===========================================================================
# FeishuInboundService — happy path
# ===========================================================================

class TestInboundServiceAccept:
    def test_text_message_accepted(self):
        svc = _make_service()
        result = svc.process_raw_event(_raw_event())
        assert not result.filtered
        assert result.message is not None
        assert result.message.content.kind == "text"
        assert result.message.content.text == "Hello"

    def test_image_message_accepted(self):
        import json
        svc = _make_service()
        result = svc.process_raw_event(_raw_event(
            message_type="image",
            content=json.dumps({"image_key": "img_k"}),
        ))
        assert result.message is not None
        assert result.message.content.kind == "image"

    def test_message_fields_populated(self):
        svc = _make_service()
        result = svc.process_raw_event(_raw_event(
            message_id="om_xyz", sender_id="ou_alice",
        ))
        msg = result.message
        assert str(msg.message_id) == "om_xyz"
        assert msg.sender.open_id == "ou_alice"
        assert msg.created_at is not None


# ===========================================================================
# FeishuInboundService — filtering
# ===========================================================================

class TestInboundServiceFilter:
    def test_wrong_event_type(self):
        svc = _make_service()
        result = svc.process_raw_event(_raw_event(event_type="other.event"))
        assert result.filtered
        assert "wrong_event_type" in result.filter_reason

    def test_missing_message_id(self):
        svc = _make_service()
        raw = _raw_event()
        del raw["message_id"]
        result = svc.process_raw_event(raw)
        assert result.filtered
        assert "missing_message_id" in result.filter_reason

    def test_self_message_filtered(self):
        svc = _make_service(bot_open_id="ou_bot")
        result = svc.process_raw_event(_raw_event(sender_id="ou_bot"))
        assert result.filtered
        assert "self_message" in result.filter_reason

    def test_group_no_mention_filtered(self):
        svc = _make_service(bot_open_id="ou_bot", bot_name="Bot")
        result = svc.process_raw_event(_raw_event(
            chat_type="group", content="no mention here",
        ))
        assert result.filtered
        assert "group_no_mention" in result.filter_reason

    def test_group_with_mention_accepted(self):
        svc = _make_service(bot_open_id="ou_bot")
        result = svc.process_raw_event(_raw_event(
            chat_type="group", content="hey ou_bot check this",
        ))
        assert not result.filtered
        assert result.message is not None


# ===========================================================================
# FeishuInboundService — dedup
# ===========================================================================

class TestInboundServiceDedup:
    def test_duplicate_message_filtered(self):
        svc = _make_service()
        raw = _raw_event(message_id="om_dup")
        result1 = svc.process_raw_event(raw)
        result2 = svc.process_raw_event(raw)
        assert not result1.filtered
        assert result2.filtered
        assert "dedup" in result2.filter_reason

    def test_stale_message_filtered(self):
        svc = _make_service(stale_seconds=300)
        result = svc.process_raw_event(_raw_event(
            create_time=str(int((time.time() - 600) * 1000)),
        ))
        assert result.filtered
        assert "stale" in result.filter_reason


# ===========================================================================
# InboundResult model
# ===========================================================================

class TestInboundResult:
    def test_accepted_result(self):
        r = InboundResult(message=None, filtered=False)
        assert not r.filtered

    def test_filtered_result(self):
        r = InboundResult(filtered=True, filter_reason="test")
        assert r.filtered
        assert r.filter_reason == "test"

    def test_frozen(self):
        from pydantic import ValidationError
        r = InboundResult(filtered=False)
        with pytest.raises(ValidationError):
            r.filtered = True


# ===========================================================================
# Gateway mapper
# ===========================================================================

class TestGatewayMapper:
    def _make_msg(self, content_kind="text", **kw):
        defaults = dict(
            message_id=MessageId(value="om_1"),
            conversation=ConversationRef(chat_id=ChatId(value="oc_c")),
            sender=Sender(sender_id=SenderId(value="ou_u")),
            content=TextContent(text="Hi"),
        )
        defaults.update(kw)
        return FeishuMessage(**defaults)

    def test_text_maps_to_text_type(self):
        msg = self._make_msg()
        assert domain_to_message_type(msg) == MessageType.TEXT

    def test_image_maps_to_photo_type(self):
        msg = self._make_msg(content=ImageContent(image_key="k"))
        assert domain_to_message_type(msg) == MessageType.PHOTO

    def test_file_maps_to_document_type(self):
        msg = self._make_msg(content=FileContent(file_key="k"))
        assert domain_to_message_type(msg) == MessageType.DOCUMENT

    def test_sticker_maps_to_sticker_type(self):
        msg = self._make_msg(content=StickerContent())
        assert domain_to_message_type(msg) == MessageType.STICKER

    def test_domain_to_message_event(self):
        msg = self._make_msg()
        event = domain_to_message_event(
            msg,
            text="Hi",
            message_type=MessageType.TEXT,
            source=None,
            media_urls=["/tmp/img.jpg"],
            media_types=["image"],
        )
        assert event.text == "Hi"
        assert event.message_id == "om_1"
        assert event.media_urls == ["/tmp/img.jpg"]
        assert event.message_type == MessageType.TEXT

    def test_event_uses_domain_timestamp(self):
        from datetime import datetime, timezone
        from gateway.platforms.feishu.domain.value_objects import ms_epoch_to_datetime
        ts = 1700000000000
        msg = self._make_msg(
            created_at=ms_epoch_to_datetime(ts),
            create_time_ms=ts,
        )
        event = domain_to_message_event(
            msg, text="x", message_type=MessageType.TEXT, source=None,
        )
        assert event.timestamp.year == 2023
