"""Tests for the FeishuCliAdapter (lark-cli based) — Phase 1 pure logic."""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from gateway.platforms.feishu_cli import (
    FeishuCliAdapter,
    check_feishu_cli_requirements,
    _run_cli,
    _parse_cli_json,
    _MESSAGE_TYPE_MAP,
)
from gateway.platforms.base import MessageType, SendResult
from gateway.config import PlatformConfig, Platform


@pytest.fixture
def adapter():
    cfg = PlatformConfig(enabled=True, extra={"bot_open_id": "ou_bot123"})
    a = FeishuCliAdapter(cfg)
    a._running = True
    return a


# ---------------------------------------------------------------------------
# check_feishu_cli_requirements
# ---------------------------------------------------------------------------

def test_requirements_missing():
    with patch("shutil.which", return_value=None):
        assert check_feishu_cli_requirements() is False


def test_requirements_present():
    with patch("shutil.which", return_value="/usr/local/bin/lark-cli"):
        assert check_feishu_cli_requirements() is True


# ---------------------------------------------------------------------------
# _parse_cli_json
# ---------------------------------------------------------------------------

def test_parse_cli_json_valid():
    assert _parse_cli_json('{"message_id":"om_123"}') == {"message_id": "om_123"}


def test_parse_cli_json_unwraps_data():
    """lark-cli wraps responses as {"ok": true, "data": {...}} — unwrap it."""
    raw = '{"ok":true,"identity":"bot","data":{"chat_id":"oc_xxx","message_id":"om_resp"}}'
    result = _parse_cli_json(raw)
    assert result == {"chat_id": "oc_xxx", "message_id": "om_resp"}


def test_parse_cli_json_empty():
    assert _parse_cli_json("") is None
    assert _parse_cli_json("  ") is None


def test_parse_cli_json_with_trailing_junk():
    result = _parse_cli_json('{"ok":true}\nsome log line')
    assert result == {"ok": True}


def test_parse_cli_json_invalid():
    assert _parse_cli_json("not json at all") is None


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

def test_extract_text_plain():
    assert FeishuCliAdapter._extract_text("text", "Hello world") == "Hello world"


def test_extract_text_json_wrapped():
    assert FeishuCliAdapter._extract_text("text", '{"text":"Hi there"}') == "Hi there"


def test_extract_text_post_zhcn():
    post = json.dumps({
        "zh_cn": {
            "title": "Title",
            "content": [[{"tag": "text", "text": "body text"}]],
        }
    })
    result = FeishuCliAdapter._extract_text("post", post)
    assert "Title" in result
    assert "body text" in result


def test_extract_text_interactive_title():
    card = json.dumps({
        "header": {"title": {"content": "Card Title"}},
        "elements": [],
    })
    assert FeishuCliAdapter._extract_text("interactive", card) == "Card Title"


def test_extract_text_interactive_fallback():
    assert FeishuCliAdapter._extract_text("interactive", "not json") == "[Interactive message]"


def test_extract_text_empty():
    assert FeishuCliAdapter._extract_text("text", "") == ""


# ---------------------------------------------------------------------------
# _extract_file_key / _extract_field
# ---------------------------------------------------------------------------

def test_extract_file_key():
    content = json.dumps({"image_key": "img_abc123"})
    assert FeishuCliAdapter._extract_file_key(content, "image_key") == "img_abc123"


def test_extract_file_key_missing():
    assert FeishuCliAdapter._extract_file_key("not json", "image_key") == ""
    assert FeishuCliAdapter._extract_file_key('{"other":"val"}', "image_key") == ""


def test_extract_field():
    content = json.dumps({"file_name": "report.pdf"})
    assert FeishuCliAdapter._extract_field(content, "file_name") == "report.pdf"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_dedup(adapter):
    assert adapter._is_duplicate("msg_1") is False
    assert adapter._is_duplicate("msg_1") is True
    assert adapter._is_duplicate("msg_2") is False


# ---------------------------------------------------------------------------
# Self-message filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_message_filtered(adapter):
    """Bot's own messages should be silently dropped."""
    event = {
        "type": "im.message.receive_v1",
        "message_id": "om_self",
        "chat_id": "oc_chat",
        "chat_type": "p2p",
        "message_type": "text",
        "content": "echo",
        "sender_id": "ou_bot123",  # matches bot_open_id
        "create_time": "1700000000000",
    }
    adapter.handle_message = AsyncMock()
    await adapter._on_event(event)
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_normal_message_dispatched(adapter):
    """Non-bot messages should be dispatched to handle_message."""
    event = {
        "type": "im.message.receive_v1",
        "message_id": "om_user1",
        "chat_id": "oc_chat",
        "chat_type": "p2p",
        "message_type": "text",
        "content": "Hello bot",
        "sender_id": "ou_user456",
        "create_time": "1700000000000",
    }
    adapter.handle_message = AsyncMock()
    await adapter._on_event(event)
    adapter.handle_message.assert_called_once()
    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.text == "Hello bot"
    assert msg_event.message_id == "om_user1"
    assert msg_event.source.chat_type == "dm"


@pytest.mark.asyncio
async def test_group_message_chat_type(adapter):
    """Group messages should have chat_type='group'."""
    event = {
        "type": "im.message.receive_v1",
        "message_id": "om_grp1",
        "chat_id": "oc_group",
        "chat_type": "group",
        "message_type": "text",
        "content": "Hey",
        "sender_id": "ou_user789",
        "create_time": "1700000000000",
    }
    adapter.handle_message = AsyncMock()
    await adapter._on_event(event)
    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.chat_type == "group"


# ---------------------------------------------------------------------------
# Duplicate filtering in _on_event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_event_filtered(adapter):
    event = {
        "type": "im.message.receive_v1",
        "message_id": "om_dup",
        "chat_id": "oc_chat",
        "chat_type": "p2p",
        "message_type": "text",
        "content": "First",
        "sender_id": "ou_user",
        "create_time": "1700000000000",
    }
    adapter.handle_message = AsyncMock()
    await adapter._on_event(event)
    await adapter._on_event(event)  # duplicate
    assert adapter.handle_message.call_count == 1


# ---------------------------------------------------------------------------
# Non-IM events ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_im_event_ignored(adapter):
    event = {"type": "some.other.event", "id": "evt_1"}
    adapter.handle_message = AsyncMock()
    await adapter._on_event(event)
    adapter.handle_message.assert_not_called()


# ---------------------------------------------------------------------------
# send() builds correct CLI args
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_text(adapter):
    with patch("gateway.platforms.feishu_cli._run_cli", new_callable=AsyncMock) as mock_cli:
        mock_cli.return_value = (0, '{"message_id":"om_resp"}', "")
        result = await adapter.send("oc_chat", "Hello!")
        assert result.success is True
        assert result.message_id == "om_resp"
        args = mock_cli.call_args[0][0]
        assert args == ["im", "+messages-send", "--chat-id", "oc_chat", "--text", "Hello!"]


@pytest.mark.asyncio
async def test_send_reply(adapter):
    with patch("gateway.platforms.feishu_cli._run_cli", new_callable=AsyncMock) as mock_cli:
        mock_cli.return_value = (0, '{"message_id":"om_resp2"}', "")
        result = await adapter.send("oc_chat", "Reply!", reply_to="om_original")
        assert result.success is True
        args = mock_cli.call_args[0][0]
        assert args == ["im", "+messages-reply", "--message-id", "om_original", "--text", "Reply!"]


@pytest.mark.asyncio
async def test_send_failure(adapter):
    with patch("gateway.platforms.feishu_cli._run_cli", new_callable=AsyncMock) as mock_cli:
        mock_cli.return_value = (1, "", "permission denied")
        result = await adapter.send("oc_chat", "Fail")
        assert result.success is False
        assert "permission denied" in result.error


@pytest.mark.asyncio
async def test_send_empty_content(adapter):
    result = await adapter.send("oc_chat", "")
    assert result.success is False


# ---------------------------------------------------------------------------
# Message type mapping
# ---------------------------------------------------------------------------

def test_message_type_map_coverage():
    """All expected lark-cli message types should be mapped."""
    expected = {"text", "post", "image", "file", "audio", "video", "sticker", "interactive"}
    assert set(_MESSAGE_TYPE_MAP.keys()) == expected


# ---------------------------------------------------------------------------
# Image message with media download
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_message_downloads(adapter):
    """Image messages should attempt resource download."""
    event = {
        "type": "im.message.receive_v1",
        "message_id": "om_img",
        "chat_id": "oc_chat",
        "chat_type": "p2p",
        "message_type": "image",
        "content": json.dumps({"image_key": "img_key_abc"}),
        "sender_id": "ou_user",
        "create_time": "1700000000000",
    }
    adapter.handle_message = AsyncMock()
    with patch.object(adapter, "_download_resource", new_callable=AsyncMock) as mock_dl:
        mock_dl.return_value = "/tmp/cached_img.jpg"
        await adapter._on_event(event)
        mock_dl.assert_called_once_with("om_img", "img_key_abc", ext=".jpg")
        msg_event = adapter.handle_message.call_args[0][0]
        assert msg_event.message_type == MessageType.PHOTO
        assert "/tmp/cached_img.jpg" in msg_event.media_urls
