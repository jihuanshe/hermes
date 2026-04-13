"""Tests for Feishu infrastructure layer — Step 7 of DDD refactor.

Covers the typed CLI client helpers (parse_cli_json, check_cli_available).
Async CLI operations are tested via the adapter integration tests.
"""

import pytest
from unittest.mock import patch, AsyncMock

from gateway.platforms.feishu.infrastructure.lark_cli_client import (
    check_cli_available,
    parse_cli_json,
    LarkCliClient,
)


# ===========================================================================
# check_cli_available
# ===========================================================================

class TestCheckCliAvailable:
    def test_available(self):
        with patch("shutil.which", return_value="/usr/local/bin/lark-cli"):
            assert check_cli_available() is True

    def test_not_available(self):
        with patch("shutil.which", return_value=None):
            assert check_cli_available() is False


# ===========================================================================
# parse_cli_json
# ===========================================================================

class TestParseCliJson:
    def test_valid_json(self):
        assert parse_cli_json('{"message_id":"om_1"}') == {"message_id": "om_1"}

    def test_unwraps_data_envelope(self):
        raw = '{"ok":true,"data":{"chat_id":"oc_x","message_id":"om_r"}}'
        result = parse_cli_json(raw)
        assert result == {"chat_id": "oc_x", "message_id": "om_r"}

    def test_empty_string(self):
        assert parse_cli_json("") is None
        assert parse_cli_json("  ") is None

    def test_trailing_junk(self):
        result = parse_cli_json('{"ok":true}\nsome log line')
        assert result == {"ok": True}

    def test_invalid_json(self):
        assert parse_cli_json("not json at all") is None

    def test_non_dict_data_not_unwrapped(self):
        result = parse_cli_json('{"data": [1, 2, 3]}')
        assert result == {"data": [1, 2, 3]}


# ===========================================================================
# LarkCliClient — unit tests with mocked run_cli
# ===========================================================================

class TestLarkCliClientBotInfo:
    @pytest.mark.asyncio
    async def test_fetch_bot_info_success(self):
        client = LarkCliClient()
        import json
        bot_response = json.dumps({"bot": {"open_id": "ou_bot1", "app_name": "TestBot"}})
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(0, bot_response, ""),
        ):
            result = await client.fetch_bot_info()
            assert result["open_id"] == "ou_bot1"
            assert result["app_name"] == "TestBot"

    @pytest.mark.asyncio
    async def test_fetch_bot_info_failure(self):
        client = LarkCliClient()
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(1, "", "error"),
        ):
            result = await client.fetch_bot_info()
            assert result == {}


class TestLarkCliClientReactions:
    @pytest.mark.asyncio
    async def test_add_reaction_success(self):
        client = LarkCliClient()
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(0, '{"reaction_id":"r_123"}', ""),
        ):
            reaction_id = await client.add_reaction("om_1", "OnIt")
            assert reaction_id == "r_123"

    @pytest.mark.asyncio
    async def test_add_reaction_failure(self):
        client = LarkCliClient()
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(1, "", "forbidden"),
        ):
            reaction_id = await client.add_reaction("om_1", "OnIt")
            assert reaction_id == ""

    @pytest.mark.asyncio
    async def test_remove_reaction(self):
        client = LarkCliClient()
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ):
            assert await client.remove_reaction("om_1", "r_123") is True


class TestLarkCliClientContact:
    @pytest.mark.asyncio
    async def test_resolve_sender_name_success(self):
        import json
        client = LarkCliClient()
        response = json.dumps({"ok": True, "data": {"user": {"name": "张三"}}})
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(0, response, ""),
        ):
            name = await client.resolve_sender_name("ou_user1")
            assert name == "张三"

    @pytest.mark.asyncio
    async def test_resolve_sender_name_failure(self):
        client = LarkCliClient()
        with patch(
            "gateway.platforms.feishu.infrastructure.lark_cli_client.run_cli",
            new_callable=AsyncMock,
            return_value=(1, "", "permission denied"),
        ):
            name = await client.resolve_sender_name("ou_unknown")
            assert name is None
