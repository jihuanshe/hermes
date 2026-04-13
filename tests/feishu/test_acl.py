"""Tests for the ACL layer — Step 4 of DDD refactor.

Covers CliCompactEventDTO parsing and CliToDomainMapper conversion.
"""

import json
import time
import pytest

from gateway.platforms.feishu.acl.cli_dtos import CliCompactEventDTO
from gateway.platforms.feishu.acl.cli_mapper import CliToDomainMapper, _extract_json_field


def _recent_ts() -> str:
    return str(int(time.time() * 1000))


# ===========================================================================
# CliCompactEventDTO
# ===========================================================================

class TestCliCompactEventDTO:
    def test_parse_minimal_event(self):
        dto = CliCompactEventDTO.model_validate({
            "type": "im.message.receive_v1",
            "message_id": "om_abc",
        })
        assert dto.type == "im.message.receive_v1"
        assert dto.effective_message_id == "om_abc"

    def test_fallback_to_id_when_no_message_id(self):
        dto = CliCompactEventDTO.model_validate({"id": "evt_123"})
        assert dto.effective_message_id == "evt_123"

    def test_message_id_takes_priority(self):
        dto = CliCompactEventDTO.model_validate({
            "id": "evt_1",
            "message_id": "om_1",
        })
        assert dto.effective_message_id == "om_1"

    def test_missing_both_ids(self):
        dto = CliCompactEventDTO.model_validate({})
        assert dto.effective_message_id == ""

    def test_create_time_ms_parsing(self):
        dto = CliCompactEventDTO.model_validate({
            "create_time": "1700000000000",
        })
        assert dto.create_time_ms == 1700000000000

    def test_timestamp_fallback(self):
        dto = CliCompactEventDTO.model_validate({
            "timestamp": "1700000000000",
        })
        assert dto.create_time_ms == 1700000000000

    def test_invalid_create_time(self):
        dto = CliCompactEventDTO.model_validate({
            "create_time": "not_a_number",
        })
        assert dto.create_time_ms is None

    def test_missing_create_time(self):
        dto = CliCompactEventDTO.model_validate({})
        assert dto.create_time_ms is None

    def test_ignores_extra_fields(self):
        dto = CliCompactEventDTO.model_validate({
            "type": "im.message.receive_v1",
            "unknown_field": "should_not_crash",
            "another": 42,
        })
        assert dto.type == "im.message.receive_v1"

    def test_full_event(self):
        dto = CliCompactEventDTO.model_validate({
            "type": "im.message.receive_v1",
            "message_id": "om_full",
            "chat_id": "oc_chat1",
            "chat_type": "group",
            "message_type": "text",
            "content": "Hello",
            "sender_id": "ou_user1",
            "create_time": _recent_ts(),
            "thread_id": "t_123",
        })
        assert dto.chat_type == "group"
        assert dto.sender_id == "ou_user1"
        assert dto.thread_id == "t_123"


# ===========================================================================
# CliToDomainMapper — basic conversion
# ===========================================================================

class TestMapperBasic:
    def setup_method(self):
        self.mapper = CliToDomainMapper()

    def test_text_message(self):
        dto = CliCompactEventDTO(
            type="im.message.receive_v1",
            message_id="om_1",
            chat_id="oc_c",
            sender_id="ou_u",
            message_type="text",
            content="Hello world",
            create_time=_recent_ts(),
        )
        msg = self.mapper.to_domain_message(dto)
        assert msg is not None
        assert str(msg.message_id) == "om_1"
        assert msg.content.kind == "text"
        assert msg.content.text == "Hello world"
        assert msg.conversation.chat_type == "dm"

    def test_group_chat_type(self):
        dto = CliCompactEventDTO(
            message_id="om_1", chat_id="oc_g", chat_type="group",
            sender_id="ou_u", content="Hi",
        )
        msg = self.mapper.to_domain_message(dto)
        assert msg.conversation.chat_type == "group"

    def test_thread_id_passthrough(self):
        dto = CliCompactEventDTO(
            message_id="om_1", chat_id="oc_g", chat_type="group",
            sender_id="ou_u", content="Hi", thread_id="t_abc",
        )
        msg = self.mapper.to_domain_message(dto)
        assert msg.conversation.thread_id == "t_abc"

    def test_missing_message_id_returns_none(self):
        dto = CliCompactEventDTO(chat_id="oc_c", sender_id="ou_u")
        assert self.mapper.to_domain_message(dto) is None

    def test_created_at_populated(self):
        ts = _recent_ts()
        dto = CliCompactEventDTO(
            message_id="om_1", sender_id="ou_u", create_time=ts,
        )
        msg = self.mapper.to_domain_message(dto)
        assert msg.created_at is not None
        assert msg.create_time_ms == int(ts)

    def test_raw_content_preserved(self):
        dto = CliCompactEventDTO(
            message_id="om_1", sender_id="ou_u",
            content='{"text": "parsed"}', message_type="text",
        )
        msg = self.mapper.to_domain_message(dto)
        assert msg.raw_content == '{"text": "parsed"}'
        assert msg.content.text == "parsed"


# ===========================================================================
# CliToDomainMapper — content type routing
# ===========================================================================

class TestMapperContent:
    def setup_method(self):
        self.mapper = CliToDomainMapper()

    def _make_dto(self, message_type: str, content: str = "") -> CliCompactEventDTO:
        return CliCompactEventDTO(
            message_id="om_1", sender_id="ou_u",
            message_type=message_type, content=content,
        )

    # -- text --

    def test_text_plain(self):
        msg = self.mapper.to_domain_message(self._make_dto("text", "Hi"))
        assert msg.content.kind == "text"
        assert msg.content.text == "Hi"

    def test_text_json_wrapped(self):
        msg = self.mapper.to_domain_message(
            self._make_dto("text", '{"text": "Hello"}'),
        )
        assert msg.content.text == "Hello"

    def test_text_empty(self):
        msg = self.mapper.to_domain_message(self._make_dto("text", ""))
        assert msg.content.text == ""

    # -- post --

    def test_post_zhcn(self):
        content = json.dumps({
            "zh_cn": {
                "title": "通知",
                "content": [
                    [{"tag": "text", "text": "第一段"}],
                    [{"tag": "a", "text": "链接", "href": "https://example.com"}],
                ],
            },
        })
        msg = self.mapper.to_domain_message(self._make_dto("post", content))
        assert msg.content.kind == "text"
        assert "通知" in msg.content.text
        assert "第一段" in msg.content.text
        assert "链接" in msg.content.text

    def test_post_with_at_mention(self):
        content = json.dumps({
            "zh_cn": {
                "title": "",
                "content": [
                    [{"tag": "at", "user_name": "Alice", "user_id": "ou_a"}],
                ],
            },
        })
        msg = self.mapper.to_domain_message(self._make_dto("post", content))
        assert "@Alice" in msg.content.text

    def test_post_fallback_plain(self):
        msg = self.mapper.to_domain_message(self._make_dto("post", "plain text"))
        assert msg.content.text == "plain text"

    # -- image --

    def test_image(self):
        content = json.dumps({"image_key": "img_abc"})
        msg = self.mapper.to_domain_message(self._make_dto("image", content))
        assert msg.content.kind == "image"
        assert msg.content.image_key == "img_abc"

    # -- file --

    def test_file(self):
        content = json.dumps({"file_key": "fk_1", "file_name": "report.pdf"})
        msg = self.mapper.to_domain_message(self._make_dto("file", content))
        assert msg.content.kind == "file"
        assert msg.content.file_key == "fk_1"
        assert msg.content.file_name == "report.pdf"
        assert "report.pdf" in msg.content.text

    # -- audio / video --

    def test_audio(self):
        content = json.dumps({"file_key": "fk_a"})
        msg = self.mapper.to_domain_message(self._make_dto("audio", content))
        assert msg.content.kind == "audio"

    def test_video(self):
        content = json.dumps({"file_key": "fk_v"})
        msg = self.mapper.to_domain_message(self._make_dto("video", content))
        assert msg.content.kind == "video"

    # -- sticker --

    def test_sticker(self):
        msg = self.mapper.to_domain_message(self._make_dto("sticker"))
        assert msg.content.kind == "sticker"
        assert msg.content.text == "[Sticker]"

    # -- interactive --

    def test_interactive_with_title(self):
        content = json.dumps({
            "header": {"title": {"content": "Card Title"}},
        })
        msg = self.mapper.to_domain_message(self._make_dto("interactive", content))
        assert msg.content.kind == "interactive"
        assert msg.content.text == "Card Title"

    def test_interactive_fallback(self):
        msg = self.mapper.to_domain_message(self._make_dto("interactive", ""))
        assert msg.content.text == "[Interactive message]"

    # -- merge_forward --

    def test_merge_forward(self):
        msg = self.mapper.to_domain_message(
            self._make_dto("merge_forward", "Alice: hi\nBob: hello"),
        )
        assert msg.content.kind == "merge_forward"
        assert "Alice" in msg.content.text

    def test_merge_forward_empty(self):
        msg = self.mapper.to_domain_message(self._make_dto("merge_forward", ""))
        assert msg.content.text == "[Forwarded messages]"

    # -- unknown --

    def test_unknown_type(self):
        msg = self.mapper.to_domain_message(self._make_dto("location", "{}"))
        assert msg.content.kind == "unsupported"
        assert msg.content.raw_type == "location"


# ===========================================================================
# _extract_json_field helper
# ===========================================================================

class TestExtractJsonField:
    def test_valid(self):
        assert _extract_json_field('{"image_key": "img_x"}', "image_key") == "img_x"

    def test_missing_field(self):
        assert _extract_json_field('{"other": "val"}', "image_key") == ""

    def test_invalid_json(self):
        assert _extract_json_field("not json", "key") == ""

    def test_empty_string(self):
        assert _extract_json_field("", "key") == ""
