"""
Feishu/Lark message deduplication.

Feishu uses at-least-once delivery semantics for WebSocket events:
when a message is not acknowledged (e.g. during a reconnection or crash),
the server retries with exponential backoff (~5 min, ~1 hr, ~2 hr).

This module provides a two-layer dedup strategy:

1. **Staleness filter** — Drop messages whose ``create_time`` is older than
   a configurable threshold (default 5 minutes). This catches the majority
   of redelivered events.

2. **Fingerprint dedup** — Hash ``(sender_id, content, create_time)`` to
   detect redelivered messages even when Feishu assigns a new ``message_id``
   (a known platform behavior on reconnection).

The existing message-ID dedup is preserved as the first check (cheapest).

Usage::

    dedup = MessageDeduplicator(FeishuDedupConfig())
    result = dedup.check_and_record(FeishuMessageIdentity(...))
    if result.should_drop:
        logger.debug("Dropping: %s", result.reason.value)
"""

from __future__ import annotations

import hashlib
import time
from enum import Enum
from typing import Callable, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class FeishuDedupConfig(BaseModel):
    """Tuning knobs for the deduplicator.

    Attributes:
        stale_threshold_seconds: Messages older than this (based on
            ``create_time``) are dropped immediately.  Set to 0 to disable.
        cache_ttl_seconds: How long accepted entries stay in the cache
            before being eligible for eviction.
        max_cache_entries: Hard cap on in-memory cache size.  When exceeded,
            entries older than ``cache_ttl_seconds`` are pruned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_threshold_seconds: int = Field(default=300, ge=0)
    cache_ttl_seconds: int = Field(
        default=3600, ge=0,
        description="1 hour — covers Feishu's second retry window",
    )
    max_cache_entries: int = Field(default=2000, ge=1)


# ---------------------------------------------------------------------------
# Typed inputs / outputs
# ---------------------------------------------------------------------------

class FeishuMessageIdentity(BaseModel):
    """The minimal set of fields needed to decide whether a message is a dup.

    ``create_time_ms`` may be ``None`` when the event lacks a parseable
    timestamp.  In that case, the staleness check is skipped but fingerprint
    dedup still works (without ``create_time`` in the hash).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    sender_id: str
    content: str
    create_time_ms: Optional[int] = None


class DedupVerdict(str, Enum):
    """Why a message was accepted or dropped."""

    ACCEPTED = "accepted"
    STALE = "stale"
    DUPLICATE_MESSAGE_ID = "duplicate_message_id"
    DUPLICATE_FINGERPRINT = "duplicate_fingerprint"


class DedupResult(BaseModel):
    """Outcome of a dedup check.

    ``age_seconds`` is populated when ``create_time_ms`` is available,
    regardless of whether the message was dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_drop: bool
    verdict: DedupVerdict
    age_seconds: Optional[float] = None


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------

class MessageDeduplicator:
    """In-memory two-layer message deduplicator.

    Designed for a single-process adapter.  Not thread-safe (all callers
    are on the same asyncio event loop).

    The ``clock`` parameter is injectable for deterministic testing.
    """

    def __init__(
        self,
        config: FeishuDedupConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config or FeishuDedupConfig()
        self._clock = clock

        # message_id → recorded_at (epoch seconds)
        self._seen_ids: Dict[str, float] = {}
        # content fingerprint → recorded_at
        self._seen_fingerprints: Dict[str, float] = {}

    # -- Public API ---------------------------------------------------------

    def check_and_record(self, message: FeishuMessageIdentity) -> DedupResult:
        """Check whether *message* should be processed or dropped.

        If accepted, the message's ID and fingerprint are recorded so that
        future duplicates can be detected.

        Evaluation order:
        1. Staleness (cheapest — pure timestamp comparison)
        2. Message-ID dedup (dict lookup)
        3. Fingerprint dedup (hash + dict lookup)
        """
        now = self._clock()
        self._maybe_prune(now)

        age = self._compute_age(message.create_time_ms, now)

        # Layer 1: staleness
        if self._config.stale_threshold_seconds > 0 and age is not None:
            if age > self._config.stale_threshold_seconds:
                return DedupResult(
                    should_drop=True,
                    verdict=DedupVerdict.STALE,
                    age_seconds=age,
                )

        # Layer 2: message-ID dedup
        if message.message_id and message.message_id in self._seen_ids:
            return DedupResult(
                should_drop=True,
                verdict=DedupVerdict.DUPLICATE_MESSAGE_ID,
                age_seconds=age,
            )

        # Layer 3: fingerprint dedup
        fingerprint = self._compute_fingerprint(message)
        if fingerprint and fingerprint in self._seen_fingerprints:
            return DedupResult(
                should_drop=True,
                verdict=DedupVerdict.DUPLICATE_FINGERPRINT,
                age_seconds=age,
            )

        # Accepted — record both identifiers
        if message.message_id:
            self._seen_ids[message.message_id] = now
        if fingerprint:
            self._seen_fingerprints[fingerprint] = now

        return DedupResult(
            should_drop=False,
            verdict=DedupVerdict.ACCEPTED,
            age_seconds=age,
        )

    def clear(self) -> None:
        """Drop all cached state (called on adapter disconnect)."""
        self._seen_ids.clear()
        self._seen_fingerprints.clear()

    @property
    def stats(self) -> Dict[str, int]:
        """Diagnostic counters for logging / debugging."""
        return {
            "cached_ids": len(self._seen_ids),
            "cached_fingerprints": len(self._seen_fingerprints),
        }

    # -- Internals ----------------------------------------------------------

    @staticmethod
    def _compute_age(
        create_time_ms: Optional[int],
        now: float,
    ) -> Optional[float]:
        """Return message age in seconds, or ``None`` if timestamp is missing."""
        if create_time_ms is None:
            return None
        try:
            create_epoch = create_time_ms / 1000.0
            return max(now - create_epoch, 0.0)
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _compute_fingerprint(message: FeishuMessageIdentity) -> str:
        """SHA-256 of ``(sender_id, content, create_time_ms)``.

        Returns an empty string if there is not enough data to build a
        meaningful fingerprint (e.g. empty content).
        """
        if not message.sender_id or not message.content:
            return ""
        parts = [
            message.sender_id,
            message.content,
            str(message.create_time_ms) if message.create_time_ms is not None else "",
        ]
        raw = "\0".join(parts).encode()
        return hashlib.sha256(raw).hexdigest()

    def _maybe_prune(self, now: float) -> None:
        """Evict expired entries when cache exceeds ``max_cache_entries``."""
        total = len(self._seen_ids) + len(self._seen_fingerprints)
        if total <= self._config.max_cache_entries:
            return
        cutoff = now - self._config.cache_ttl_seconds
        self._seen_ids = {k: v for k, v in self._seen_ids.items() if v > cutoff}
        self._seen_fingerprints = {
            k: v for k, v in self._seen_fingerprints.items() if v > cutoff
        }
