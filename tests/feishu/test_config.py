"""Tests for gateway/platforms/feishu/config.py — Step 1 of DDD refactor."""

import os
import pytest
from pydantic import ValidationError

from gateway.platforms.feishu.config import (
    BotConfig,
    CredentialsConfig,
    FeishuAdapterConfig,
)
from gateway.platforms.feishu_dedup import FeishuDedupConfig


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_default_config_has_empty_credentials(self):
        cfg = FeishuAdapterConfig.from_platform(None)
        assert cfg.credentials.app_id == ""
        assert cfg.credentials.app_secret == ""

    def test_default_bot_identity_empty(self):
        cfg = FeishuAdapterConfig.from_platform(None)
        assert cfg.bot.open_id == ""
        assert cfg.bot.name == ""

    def test_default_dedup_values(self):
        cfg = FeishuAdapterConfig.from_platform(None)
        assert cfg.dedup.stale_threshold_seconds == 300
        assert cfg.dedup.cache_ttl_seconds == 3600
        assert cfg.dedup.max_cache_entries == 2000


# ---------------------------------------------------------------------------
# From extra dict (config.yaml values)
# ---------------------------------------------------------------------------

class TestFromExtra:
    def test_bot_identity_from_extra(self):
        extra = {"bot_open_id": "ou_bot123", "bot_name": "TestBot"}
        cfg = FeishuAdapterConfig.from_platform(extra)
        assert cfg.bot.open_id == "ou_bot123"
        assert cfg.bot.name == "TestBot"

    def test_credentials_from_extra(self):
        extra = {"app_id": "cli_abc", "app_secret": "secret_xyz"}
        cfg = FeishuAdapterConfig.from_platform(extra)
        assert cfg.credentials.app_id == "cli_abc"
        assert cfg.credentials.app_secret == "secret_xyz"

    def test_dedup_from_extra(self):
        extra = {
            "message_stale_after_seconds": 600,
            "dedup_cache_ttl_seconds": 7200,
            "dedup_cache_max_size": 5000,
        }
        cfg = FeishuAdapterConfig.from_platform(extra)
        assert cfg.dedup.stale_threshold_seconds == 600
        assert cfg.dedup.cache_ttl_seconds == 7200
        assert cfg.dedup.max_cache_entries == 5000

    def test_partial_extra_fills_defaults(self):
        extra = {"bot_open_id": "ou_x"}
        cfg = FeishuAdapterConfig.from_platform(extra)
        assert cfg.bot.open_id == "ou_x"
        assert cfg.bot.name == ""  # default
        assert cfg.dedup.stale_threshold_seconds == 300  # default


# ---------------------------------------------------------------------------
# From environment variables (fallback)
# ---------------------------------------------------------------------------

class TestFromEnv:
    def test_env_fallback_for_bot(self, monkeypatch):
        monkeypatch.setenv("FEISHU_BOT_OPEN_ID", "ou_env")
        monkeypatch.setenv("FEISHU_BOT_NAME", "EnvBot")
        cfg = FeishuAdapterConfig.from_platform({})
        assert cfg.bot.open_id == "ou_env"
        assert cfg.bot.name == "EnvBot"

    def test_env_fallback_for_credentials(self, monkeypatch):
        monkeypatch.setenv("FEISHU_APP_ID", "cli_env")
        monkeypatch.setenv("FEISHU_APP_SECRET", "secret_env")
        cfg = FeishuAdapterConfig.from_platform({})
        assert cfg.credentials.app_id == "cli_env"
        assert cfg.credentials.app_secret == "secret_env"

    def test_extra_takes_priority_over_env(self, monkeypatch):
        monkeypatch.setenv("FEISHU_BOT_OPEN_ID", "ou_env")
        extra = {"bot_open_id": "ou_yaml"}
        cfg = FeishuAdapterConfig.from_platform(extra)
        assert cfg.bot.open_id == "ou_yaml"


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_config_is_frozen(self):
        cfg = FeishuAdapterConfig.from_platform({"bot_open_id": "ou_x"})
        with pytest.raises(ValidationError):
            cfg.bot = BotConfig(open_id="ou_y")

    def test_sub_models_are_frozen(self):
        cfg = FeishuAdapterConfig.from_platform(None)
        with pytest.raises(ValidationError):
            cfg.dedup.stale_threshold_seconds = 999


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_negative_stale_threshold_rejected(self):
        with pytest.raises(ValidationError):
            FeishuDedupConfig(stale_threshold_seconds=-1)

    def test_zero_max_entries_rejected(self):
        with pytest.raises(ValidationError):
            FeishuDedupConfig(max_cache_entries=0)

    def test_zero_stale_threshold_allowed(self):
        """stale_threshold_seconds=0 means disabled."""
        cfg = FeishuDedupConfig(stale_threshold_seconds=0)
        assert cfg.stale_threshold_seconds == 0
