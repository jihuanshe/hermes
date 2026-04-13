"""
Feishu application service — the single orchestrator for inbound messages.

``FeishuInboundService`` wires together the ACL, domain policy, and dedup
into a clean pipeline.  It replaces the 160-line ``_on_event()`` god method.

The service is **not** responsible for IO operations (media download,
sender name resolution, sending replies).  Those remain in the adapter
facade and infrastructure layer.

Usage in the adapter::

    result = self._inbound_service.process_raw_event(raw_data)
    if result is None:
        return  # filtered out
    # result.message is a FeishuMessage — proceed with IO
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict

from gateway.platforms.feishu.acl.cli_dtos import CliCompactEventDTO
from gateway.platforms.feishu.acl.cli_mapper import CliToDomainMapper
from gateway.platforms.feishu.domain.models import FeishuMessage
from gateway.platforms.feishu.domain.services import (
    InboundMessagePolicy,
    RejectReason,
    dedup_identity_from_message,
)
from gateway.platforms.feishu_dedup import DedupResult, MessageDeduplicator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class InboundResult(BaseModel):
    """Outcome of processing a raw inbound event.

    When ``message`` is populated, the event passed all checks and
    is ready for media enrichment and dispatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: Optional[FeishuMessage] = None
    filtered: bool = False
    filter_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Application service
# ---------------------------------------------------------------------------

class FeishuInboundService:
    """Orchestrates inbound message processing: parse → policy → dedup.

    Stateless except for the deduplicator cache.  All dependencies are
    injected via constructor.
    """

    def __init__(
        self,
        mapper: CliToDomainMapper,
        policy: InboundMessagePolicy,
        deduplicator: MessageDeduplicator,
    ) -> None:
        self._mapper = mapper
        self._policy = policy
        self._deduplicator = deduplicator

    def process_raw_event(
        self, raw: Mapping[str, Any],
    ) -> InboundResult:
        """Process a raw NDJSON event dict through the full inbound pipeline.

        Returns an ``InboundResult`` with either:
        - ``message`` populated (accepted, ready for IO enrichment)
        - ``filtered=True`` with a reason (rejected at some stage)
        """
        # 1. Parse raw dict → DTO
        dto = self._mapper.parse_event(raw)

        # 2. Event type filter
        if dto.type != "im.message.receive_v1":
            return InboundResult(
                filtered=True,
                filter_reason=f"wrong_event_type:{dto.type}",
            )

        # 3. Map DTO → domain message
        message = self._mapper.to_domain_message(dto)
        if message is None:
            return InboundResult(
                filtered=True,
                filter_reason="missing_message_id",
            )

        # 4. Inbound policy (self-message + mention gate)
        policy_result = self._policy.evaluate(message)
        if not policy_result.should_process:
            return InboundResult(
                filtered=True,
                filter_reason=policy_result.reject_reason.value
                if policy_result.reject_reason
                else "policy_rejected",
            )

        # 5. Dedup (staleness + message-ID + fingerprint)
        identity = dedup_identity_from_message(message)
        dedup_result = self._deduplicator.check_and_record(identity)
        if dedup_result.should_drop:
            return InboundResult(
                filtered=True,
                filter_reason=f"dedup:{dedup_result.verdict.value}"
                + (f"(age={dedup_result.age_seconds:.0f}s)"
                   if dedup_result.age_seconds else ""),
            )

        # 6. Accepted
        return InboundResult(message=message)
