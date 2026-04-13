"""Tests for Feishu message deduplication (gateway/platforms/feishu_dedup.py).

Covers the three dedup layers:
1. Staleness — messages older than threshold are dropped
2. Message-ID — same message_id seen twice is dropped
3. Content fingerprint — same (sender, content, create_time) with a new
   message_id is dropped (Feishu redelivery with regenerated IDs)

Also covers Pydantic model behavior (frozen, extra=forbid, validation).
"""

import time
import pytest
from pydantic import ValidationError

from gateway.platforms.feishu_dedup import (
    DedupVerdict,
    DedupResult,
    FeishuDedupConfig,
    FeishuMessageIdentity,
    MessageDeduplicator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(
    message_id: str = "om_abc",
    sender_id: str = "ou_user1",
    content: str = "Hello",
    create_time_ms: int | None = None,
    *,
    age_seconds: float = 0,
    clock_now: float = 1_700_000_000.0,
) -> FeishuMessageIdentity:
    """Build a FeishuMessageIdentity with an auto-computed create_time."""
    if create_time_ms is None and age_seconds is not None:
        create_time_ms = int((clock_now - age_seconds) * 1000)
    return FeishuMessageIdentity(
        message_id=message_id,
        sender_id=sender_id,
        content=content,
        create_time_ms=create_time_ms,
    )


FIXED_NOW = 1_700_000_000.0


def _dedup(config: FeishuDedupConfig | None = None) -> MessageDeduplicator:
    return MessageDeduplicator(config, clock=lambda: FIXED_NOW)


# ---------------------------------------------------------------------------
# Layer 1: Staleness
# ---------------------------------------------------------------------------

class TestStalenessFilter:
    def test_recent_message_accepted(self):
        d = _dedup()
        msg = _msg(age_seconds=10, clock_now=FIXED_NOW)
        result = d.check_and_record(msg)
        assert not result.should_drop
        assert result.verdict == DedupVerdict.ACCEPTED

    def test_stale_message_dropped(self):
        d = _dedup()
        msg = _msg(age_seconds=600, clock_now=FIXED_NOW)  # 10 minutes > 5 min threshold
        result = d.check_and_record(msg)
        assert result.should_drop
        assert result.verdict == DedupVerdict.STALE
        assert result.age_seconds is not None
        assert result.age_seconds >= 600

    def test_exactly_at_threshold_accepted(self):
        """Message exactly at the threshold boundary should pass."""
        d = _dedup(FeishuDedupConfig(stale_threshold_seconds=300))
        msg = _msg(age_seconds=300, clock_now=FIXED_NOW)
        result = d.check_and_record(msg)
        assert not result.should_drop

    def test_staleness_disabled_when_zero(self):
        d = _dedup(FeishuDedupConfig(stale_threshold_seconds=0))
        msg = _msg(age_seconds=9999, clock_now=FIXED_NOW)
        result = d.check_and_record(msg)
        assert not result.should_drop

    def test_missing_create_time_skips_staleness(self):
        """Without create_time, staleness check is skipped — message accepted."""
        d = _dedup()
        msg = FeishuMessageIdentity(
            message_id="om_no_time",
            sender_id="ou_user1",
            content="Hello",
            create_time_ms=None,
        )
        result = d.check_and_record(msg)
        assert not result.should_drop
        assert result.age_seconds is None


# ---------------------------------------------------------------------------
# Layer 2: Message-ID dedup
# ---------------------------------------------------------------------------

class TestMessageIdDedup:
    def test_same_id_twice_dropped(self):
        d = _dedup()
        msg = _msg(message_id="om_dup", age_seconds=10, clock_now=FIXED_NOW)
        assert not d.check_and_record(msg).should_drop
        result = d.check_and_record(msg)
        assert result.should_drop
        assert result.verdict == DedupVerdict.DUPLICATE_MESSAGE_ID

    def test_different_ids_accepted(self):
        d = _dedup()
        msg1 = _msg(message_id="om_1", content="A", age_seconds=10, clock_now=FIXED_NOW)
        msg2 = _msg(message_id="om_2", content="B", age_seconds=10, clock_now=FIXED_NOW)
        assert not d.check_and_record(msg1).should_drop
        assert not d.check_and_record(msg2).should_drop


# ---------------------------------------------------------------------------
# Layer 3: Fingerprint dedup
# ---------------------------------------------------------------------------

class TestFingerprintDedup:
    def test_new_message_id_same_content_dropped(self):
        """Feishu sometimes generates a new message_id for redelivered events."""
        d = _dedup()
        create_ts = int((FIXED_NOW - 10) * 1000)
        msg1 = FeishuMessageIdentity(message_id="om_orig", sender_id="ou_user1", content="Hello", create_time_ms=create_ts)
        msg2 = FeishuMessageIdentity(message_id="om_redeliver", sender_id="ou_user1", content="Hello", create_time_ms=create_ts)
        assert not d.check_and_record(msg1).should_drop
        result = d.check_and_record(msg2)
        assert result.should_drop
        assert result.verdict == DedupVerdict.DUPLICATE_FINGERPRINT

    def test_same_content_different_sender_accepted(self):
        d = _dedup()
        create_ts = int((FIXED_NOW - 10) * 1000)
        msg1 = FeishuMessageIdentity(message_id="om_1", sender_id="ou_alice", content="Hello", create_time_ms=create_ts)
        msg2 = FeishuMessageIdentity(message_id="om_2", sender_id="ou_bob", content="Hello", create_time_ms=create_ts)
        assert not d.check_and_record(msg1).should_drop
        assert not d.check_and_record(msg2).should_drop

    def test_same_sender_different_content_accepted(self):
        d = _dedup()
        create_ts = int((FIXED_NOW - 10) * 1000)
        msg1 = FeishuMessageIdentity(message_id="om_1", sender_id="ou_user1", content="Hello", create_time_ms=create_ts)
        msg2 = FeishuMessageIdentity(message_id="om_2", sender_id="ou_user1", content="Goodbye", create_time_ms=create_ts)
        assert not d.check_and_record(msg1).should_drop
        assert not d.check_and_record(msg2).should_drop

    def test_empty_content_skips_fingerprint(self):
        """Empty content shouldn't produce a fingerprint (e.g. image-only)."""
        d = _dedup()
        create_ts = int((FIXED_NOW - 10) * 1000)
        msg1 = FeishuMessageIdentity(message_id="om_1", sender_id="ou_user1", content="", create_time_ms=create_ts)
        msg2 = FeishuMessageIdentity(message_id="om_2", sender_id="ou_user1", content="", create_time_ms=create_ts)
        # Both accepted — no fingerprint dedup, and message-IDs differ
        assert not d.check_and_record(msg1).should_drop
        assert not d.check_and_record(msg2).should_drop


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

class TestCacheManagement:
    def test_clear_resets_state(self):
        d = _dedup()
        msg = _msg(age_seconds=10, clock_now=FIXED_NOW)
        d.check_and_record(msg)
        d.clear()
        # After clear, same message should be accepted again
        assert not d.check_and_record(msg).should_drop

    def test_stats_reflect_entries(self):
        d = _dedup()
        assert d.stats == {"cached_ids": 0, "cached_fingerprints": 0}
        d.check_and_record(_msg(message_id="om_1", age_seconds=10, clock_now=FIXED_NOW))
        stats = d.stats
        assert stats["cached_ids"] == 1
        assert stats["cached_fingerprints"] == 1

    def test_prune_evicts_old_entries(self):
        """When cache exceeds max, entries older than TTL are evicted."""
        config = FeishuDedupConfig(
            max_cache_entries=2,
            cache_ttl_seconds=60,
        )
        t = FIXED_NOW
        d = MessageDeduplicator(config, clock=lambda: t)

        # Add 2 entries at t=0
        d.check_and_record(_msg(message_id="om_1", age_seconds=10, clock_now=t))
        d.check_and_record(_msg(message_id="om_2", content="B", age_seconds=10, clock_now=t))
        assert d.stats["cached_ids"] == 2

        # Advance clock past TTL and add a third — prune should fire
        t = FIXED_NOW + 120
        d._clock = lambda: t
        d.check_and_record(_msg(message_id="om_3", content="C", age_seconds=10, clock_now=t))
        # Old entries should have been pruned
        assert d.stats["cached_ids"] <= 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_negative_age_treated_as_zero(self):
        """Future timestamp (clock skew) shouldn't crash."""
        d = _dedup()
        future_ts = int((FIXED_NOW + 3600) * 1000)
        msg = FeishuMessageIdentity(message_id="om_future", sender_id="ou_user1", content="Hi", create_time_ms=future_ts)
        result = d.check_and_record(msg)
        assert not result.should_drop
        assert result.age_seconds == 0.0

    def test_missing_sender_skips_fingerprint(self):
        d = _dedup()
        create_ts = int((FIXED_NOW - 10) * 1000)
        msg1 = FeishuMessageIdentity(message_id="om_1", sender_id="", content="Hello", create_time_ms=create_ts)
        msg2 = FeishuMessageIdentity(message_id="om_2", sender_id="", content="Hello", create_time_ms=create_ts)
        assert not d.check_and_record(msg1).should_drop
        # Only message-ID dedup available (fingerprint skipped)
        assert not d.check_and_record(msg2).should_drop


# ---------------------------------------------------------------------------
# Pydantic model behavior (Step 2 of DDD refactor)
# ---------------------------------------------------------------------------

class TestPydanticModels:
    def test_config_is_frozen(self):
        cfg = FeishuDedupConfig()
        with pytest.raises(ValidationError):
            cfg.stale_threshold_seconds = 999

    def test_config_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            FeishuDedupConfig(unknown_field="bad")

    def test_identity_is_frozen(self):
        msg = FeishuMessageIdentity(
            message_id="om_1", sender_id="ou_1", content="hi", create_time_ms=123,
        )
        with pytest.raises(ValidationError):
            msg.message_id = "om_2"

    def test_identity_create_time_defaults_to_none(self):
        msg = FeishuMessageIdentity(message_id="om_1", sender_id="ou_1", content="hi")
        assert msg.create_time_ms is None

    def test_result_is_frozen(self):
        result = DedupResult(should_drop=False, verdict=DedupVerdict.ACCEPTED)
        with pytest.raises(ValidationError):
            result.should_drop = True

    def test_result_serialization(self):
        result = DedupResult(
            should_drop=True, verdict=DedupVerdict.STALE, age_seconds=600.0,
        )
        data = result.model_dump()
        assert data == {
            "should_drop": True,
            "verdict": "stale",
            "age_seconds": 600.0,
        }

    def test_identity_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            FeishuMessageIdentity(
                message_id="om_1", sender_id="ou_1", content="hi",
                create_time_ms=123, bogus="nope",
            )
