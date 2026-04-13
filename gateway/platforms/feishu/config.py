"""
Feishu adapter configuration — Pydantic v2 validated settings.

Replaces scattered ``extra.get()`` / ``os.getenv()`` calls with a single
validated model.  Constructed once in ``FeishuCliAdapter.__init__`` from
``PlatformConfig.extra`` and environment variables.

Usage::

    cfg = FeishuAdapterConfig.from_platform(platform_config)
    print(cfg.bot.open_id)
    print(cfg.dedup.stale_threshold_seconds)
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from gateway.platforms.feishu_dedup import FeishuDedupConfig


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class BotConfig(BaseModel):
    """Bot identity — used for self-message filtering and @mention detection."""

    model_config = ConfigDict(frozen=True)

    open_id: str = ""
    name: str = ""


class CredentialsConfig(BaseModel):
    """App credentials — used by lark-cli for API auth."""

    model_config = ConfigDict(frozen=True)

    app_id: str = ""
    app_secret: str = ""


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class FeishuAdapterConfig(BaseModel):
    """Complete Feishu adapter configuration.

    Merges values from three sources (in priority order):

    1. ``PlatformConfig.extra`` dict (config.yaml)
    2. Environment variables (``FEISHU_*``)
    3. Defaults defined here
    """

    model_config = ConfigDict(frozen=True)

    credentials: CredentialsConfig = CredentialsConfig()
    bot: BotConfig = BotConfig()
    dedup: FeishuDedupConfig = FeishuDedupConfig()

    @classmethod
    def from_platform(
        cls,
        extra: Mapping[str, Any] | None = None,
    ) -> FeishuAdapterConfig:
        """Build config by merging ``extra`` dict with env vars.

        Priority: extra dict > env var > default.
        """
        raw = dict(extra or {})

        credentials = CredentialsConfig(
            app_id=raw.get("app_id") or os.getenv("FEISHU_APP_ID", ""),
            app_secret=raw.get("app_secret") or os.getenv("FEISHU_APP_SECRET", ""),
        )

        bot = BotConfig(
            open_id=raw.get("bot_open_id") or os.getenv("FEISHU_BOT_OPEN_ID", ""),
            name=raw.get("bot_name") or os.getenv("FEISHU_BOT_NAME", ""),
        )

        dedup = FeishuDedupConfig(
            stale_threshold_seconds=int(
                raw.get("message_stale_after_seconds", 300),
            ),
            cache_ttl_seconds=int(
                raw.get("dedup_cache_ttl_seconds", 3600),
            ),
            max_cache_entries=int(
                raw.get("dedup_cache_max_size", 2000),
            ),
        )

        return cls(credentials=credentials, bot=bot, dedup=dedup)
