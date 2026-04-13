"""Tests for Feishu domain layer — Step 3 of DDD refactor.

Covers value objects, content types, and the FeishuMessage model.
"""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from gateway.platforms.feishu.domain.value_objects import (
    BotIdentity,
    ChatId,
    ConversationRef,
    Mention,
    MessageId,
    Sender,
    SenderId,
    ms_epoch_to_datetime,
)
from gateway.platforms.feishu.domain.content import (
    AudioContent,
    FileContent,
    ImageContent,
    InteractiveContent,
    MergeForwardContent,
    StickerContent,
    TextContent,
    UnsupportedContent,
    VideoContent,
    FEISHU_TYPE_TO_KIND,
)
from gateway.platforms.feishu.domain.models import FeishuMessage


# ===========================================================================
# Value Objects
# ===========================================================================

class TestMessageId:
    def test_str(self):
        mid = MessageId(value="om_abc123")
        assert str(mid) == "om_abc123"

    def test_bool_truthy(self):
        assert bool(MessageId(value="om_x"))

    def test_bool_falsy(self):
        assert not bool(MessageId(value=""))

    def test_frozen(self):
        mid = MessageId(value="om_x")
        with pytest.raises(ValidationError):
            mid.value = "om_y"


class TestChatId:
    def test_str(self):
        assert str(ChatId(value="oc_chat1")) == "oc_chat1"

    def test_strips_whitespace(self):
        cid = ChatId(value="  oc_x  ")
        assert cid.value == "oc_x"


class TestSenderId:
    def test_defaults_to_open_id(self):
        sid = SenderId(value="ou_user1")
        assert sid.kind == "open_id"

    def test_custom_kind(self):
        sid = SenderId(value="uid_123", kind="user_id")
        assert sid.kind == "user_id"

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            SenderId(value="x", kind="email")


class TestBotIdentity:
    def test_is_sender_match(self):
        bot = BotIdentity(open_id="ou_bot1", name="MyBot")
        assert bot.is_sender("ou_bot1")

    def test_is_sender_no_match(self):
        bot = BotIdentity(open_id="ou_bot1")
        assert not bot.is_sender("ou_user1")

    def test_is_sender_empty_open_id(self):
        bot = BotIdentity()
        assert not bot.is_sender("ou_anything")


class TestConversationRef:
    def test_dm_default(self):
        conv = ConversationRef(chat_id=ChatId(value="oc_x"))
        assert conv.chat_type == "dm"
        assert conv.thread_id is None

    def test_group_with_thread(self):
        conv = ConversationRef(
            chat_id=ChatId(value="oc_g"),
            chat_type="group",
            thread_id="t_123",
        )
        assert conv.chat_type == "group"
        assert conv.thread_id == "t_123"

    def test_invalid_chat_type(self):
        with pytest.raises(ValidationError):
            ConversationRef(chat_id=ChatId(value="oc_x"), chat_type="channel")


class TestSender:
    def test_open_id_property(self):
        s = Sender(sender_id=SenderId(value="ou_abc"))
        assert s.open_id == "ou_abc"

    def test_display_name_optional(self):
        s = Sender(sender_id=SenderId(value="ou_abc"))
        assert s.display_name is None


class TestMsEpochToDatetime:
    def test_valid_conversion(self):
        dt = ms_epoch_to_datetime(1700000000000)
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2023

    def test_none_input(self):
        assert ms_epoch_to_datetime(None) is None

    def test_overflow_returns_none(self):
        assert ms_epoch_to_datetime(999_999_999_999_999_999) is None


# ===========================================================================
# Content Types
# ===========================================================================

class TestTextContent:
    def test_basic(self):
        c = TextContent(text="Hello world")
        assert c.kind == "text"
        assert c.text == "Hello world"

    def test_frozen(self):
        c = TextContent(text="x")
        with pytest.raises(ValidationError):
            c.text = "y"


class TestImageContent:
    def test_defaults(self):
        c = ImageContent(image_key="img_abc")
        assert c.kind == "image"
        assert c.text == "[Image]"
        assert c.image_key == "img_abc"


class TestFileContent:
    def test_auto_text_from_filename(self):
        c = FileContent(file_key="fk_1", file_name="report.pdf")
        assert c.text == "[File: report.pdf]"

    def test_default_text(self):
        c = FileContent(file_key="fk_1")
        assert c.text == "[File: attachment]"

    def test_explicit_text_preserved(self):
        c = FileContent(file_key="fk_1", file_name="x.txt", text="Custom")
        assert c.text == "Custom"


class TestAudioContent:
    def test_defaults(self):
        c = AudioContent(file_key="fk_a")
        assert c.text == "[Audio]"


class TestVideoContent:
    def test_defaults(self):
        c = VideoContent(file_key="fk_v")
        assert c.text == "[Video]"


class TestStickerContent:
    def test_defaults(self):
        c = StickerContent()
        assert c.text == "[Sticker]"


class TestInteractiveContent:
    def test_defaults(self):
        c = InteractiveContent()
        assert c.text == "[Interactive message]"

    def test_custom_text(self):
        c = InteractiveContent(text="Card Title")
        assert c.text == "Card Title"


class TestMergeForwardContent:
    def test_defaults(self):
        c = MergeForwardContent()
        assert c.text == "[Forwarded messages]"

    def test_custom_text(self):
        c = MergeForwardContent(text="Alice: hi\nBob: hello")
        assert "Alice" in c.text


class TestUnsupportedContent:
    def test_captures_raw_type(self):
        c = UnsupportedContent(raw_type="location")
        assert c.raw_type == "location"
        assert c.text == ""


class TestFeishuTypeToKindMap:
    def test_all_known_types_mapped(self):
        expected = {"text", "post", "image", "file", "audio", "video",
                    "sticker", "interactive", "merge_forward"}
        assert set(FEISHU_TYPE_TO_KIND.keys()) == expected

    def test_post_maps_to_text(self):
        assert FEISHU_TYPE_TO_KIND["post"] == "text"


# ===========================================================================
# FeishuMessage (domain aggregate)
# ===========================================================================

class TestFeishuMessage:
    def _make_msg(self, **overrides):
        defaults = dict(
            message_id=MessageId(value="om_1"),
            conversation=ConversationRef(chat_id=ChatId(value="oc_1")),
            sender=Sender(sender_id=SenderId(value="ou_1")),
            content=TextContent(text="Hello"),
        )
        defaults.update(overrides)
        return FeishuMessage(**defaults)

    def test_basic_construction(self):
        msg = self._make_msg()
        assert str(msg.message_id) == "om_1"
        assert msg.content.kind == "text"
        assert msg.content.text == "Hello"

    def test_with_image_content(self):
        msg = self._make_msg(content=ImageContent(image_key="img_k"))
        assert msg.content.kind == "image"
        assert msg.content.image_key == "img_k"

    def test_with_file_content(self):
        msg = self._make_msg(content=FileContent(file_key="fk_1", file_name="data.csv"))
        assert msg.content.kind == "file"
        assert msg.content.text == "[File: data.csv]"

    def test_frozen(self):
        msg = self._make_msg()
        with pytest.raises(ValidationError):
            msg.content = TextContent(text="changed")

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            self._make_msg(bogus="nope")

    def test_with_mentions(self):
        msg = self._make_msg(
            mentions=(
                Mention(user_id="ou_bot", name="Bot", is_bot=True),
                Mention(user_id="ou_user2", name="Alice"),
            ),
        )
        assert len(msg.mentions) == 2
        assert msg.mentions[0].is_bot

    def test_with_conversation_context(self):
        msg = self._make_msg(
            conversation=ConversationRef(
                chat_id=ChatId(value="oc_group1"),
                chat_type="group",
                thread_id="thread_abc",
            ),
        )
        assert msg.conversation.chat_type == "group"
        assert msg.conversation.thread_id == "thread_abc"

    def test_created_at_and_raw_content(self):
        msg = self._make_msg(
            created_at=ms_epoch_to_datetime(1700000000000),
            create_time_ms=1700000000000,
            raw_content='{"text": "Hello"}',
        )
        assert msg.created_at is not None
        assert msg.create_time_ms == 1700000000000
        assert msg.raw_content == '{"text": "Hello"}'
