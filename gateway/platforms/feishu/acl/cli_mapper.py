"""
Anti-corruption layer mapper — lark-cli compact DTO → domain models.

This is the single place that understands the lark-cli compact format
and converts it into clean domain objects.  All quirks of the external
format (JSON-wrapped text, locale-keyed posts, card structures) are
handled here, never in the domain layer.

If a future webhook transport is added, a parallel ``WebhookMapper``
would produce the same ``FeishuMessage`` domain objects.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional

from gateway.platforms.feishu.acl.cli_dtos import CliCompactEventDTO
from gateway.platforms.feishu.domain.content import (
    AudioContent,
    FileContent,
    ImageContent,
    InteractiveContent,
    MergeForwardContent,
    MessageContent,
    StickerContent,
    TextContent,
    UnsupportedContent,
    VideoContent,
)
from gateway.platforms.feishu.domain.models import FeishuMessage
from gateway.platforms.feishu.domain.value_objects import (
    ChatId,
    ConversationRef,
    Mention,
    MessageId,
    Sender,
    SenderId,
    ms_epoch_to_datetime,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public mapper
# ---------------------------------------------------------------------------

class CliToDomainMapper:
    """Converts raw lark-cli compact event data into domain models.

    Stateless and safe to share across calls.
    """

    def parse_event(self, raw: Mapping[str, Any]) -> CliCompactEventDTO:
        """Parse a raw dict into a validated DTO."""
        return CliCompactEventDTO.model_validate(raw)

    def to_domain_message(self, dto: CliCompactEventDTO) -> FeishuMessage | None:
        """Convert a CLI DTO into a ``FeishuMessage``, or ``None`` if invalid.

        Returns ``None`` when the DTO lacks a ``message_id`` (the minimum
        required field for a processable message).
        """
        msg_id = dto.effective_message_id
        if not msg_id:
            return None

        chat_type = "group" if dto.chat_type == "group" else "dm"
        content = self._build_content(dto.message_type, dto.content)
        create_time_ms = dto.create_time_ms

        return FeishuMessage(
            message_id=MessageId(value=msg_id),
            conversation=ConversationRef(
                chat_id=ChatId(value=dto.chat_id),
                chat_type=chat_type,
                thread_id=dto.thread_id,
            ),
            sender=Sender(sender_id=SenderId(value=dto.sender_id)),
            content=content,
            created_at=ms_epoch_to_datetime(create_time_ms),
            create_time_ms=create_time_ms,
            mentions=self._build_mentions(dto.mentions),
            raw_content=dto.content,
        )

    # -- Mention building --------------------------------------------------

    @staticmethod
    def _build_mentions(
        raw_mentions: Optional[list[dict[str, Any]]],
    ) -> tuple[Mention, ...]:
        """Convert raw mention dicts to domain ``Mention`` objects."""
        if not raw_mentions:
            return ()
        mentions: list[Mention] = []
        for m in raw_mentions:
            if not isinstance(m, dict):
                continue
            # lark-cli compact format may nest id under "id" dict or flat
            mid = m.get("id", {})
            user_id = (
                (mid.get("open_id") if isinstance(mid, dict) else "")
                or m.get("open_id")
                or m.get("user_id")
                or ""
            )
            mentions.append(Mention(
                user_id=user_id,
                name=m.get("name") or m.get("user_name") or "",
                is_bot=bool(m.get("is_bot", False)),
            ))
        return tuple(mentions)

    # -- Content building ---------------------------------------------------

    def _build_content(self, message_type: str, raw_content: str) -> MessageContent:
        """Route to the appropriate content builder based on message_type."""
        builders = {
            "text": self._build_text,
            "post": self._build_post,
            "image": self._build_image,
            "file": self._build_file,
            "audio": self._build_audio,
            "video": self._build_video,
            "sticker": self._build_sticker,
            "interactive": self._build_interactive,
            "merge_forward": self._build_merge_forward,
        }
        builder = builders.get(message_type)
        if builder is None:
            return UnsupportedContent(raw_type=message_type, text=raw_content.strip())
        return builder(raw_content)

    @staticmethod
    def _build_text(content: str) -> TextContent:
        """Parse text content — may be plain string or JSON ``{"text": "..."}``."""
        if not content:
            return TextContent(text="")
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return TextContent(text=parsed.get("text", content).strip())
        except (json.JSONDecodeError, AttributeError):
            pass
        return TextContent(text=content.strip())

    @staticmethod
    def _build_post(content: str) -> TextContent:
        """Parse rich-text post content with locale keys and nested blocks."""
        if not content:
            return TextContent(text="")
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                for locale in ("zh_cn", "en_us", "ja_jp"):
                    locale_data = parsed.get(locale)
                    if not locale_data:
                        continue
                    parts: List[str] = []
                    title = locale_data.get("title", "")
                    if title:
                        parts.append(title)
                    for paragraph in locale_data.get("content", []):
                        for element in paragraph:
                            tag = element.get("tag", "")
                            if tag == "text":
                                parts.append(element.get("text", ""))
                            elif tag == "a":
                                parts.append(
                                    element.get("text", element.get("href", ""))
                                )
                            elif tag == "at":
                                parts.append(
                                    f"@{element.get('user_name', element.get('user_id', ''))}"
                                )
                    if parts:
                        return TextContent(text="\n".join(parts).strip())
        except (json.JSONDecodeError, AttributeError):
            pass
        return TextContent(text=content.strip())

    @staticmethod
    def _build_image(content: str) -> ImageContent:
        """Parse image content — extract ``image_key`` from JSON."""
        image_key = _extract_json_field(content, "image_key")
        return ImageContent(image_key=image_key or "")

    @staticmethod
    def _build_file(content: str) -> FileContent:
        """Parse file content — extract ``file_key`` and ``file_name``."""
        file_key = _extract_json_field(content, "file_key")
        file_name = _extract_json_field(content, "file_name") or "attachment"
        return FileContent(file_key=file_key or "", file_name=file_name)

    @staticmethod
    def _build_audio(content: str) -> AudioContent:
        file_key = _extract_json_field(content, "file_key")
        return AudioContent(file_key=file_key or "")

    @staticmethod
    def _build_video(content: str) -> VideoContent:
        file_key = _extract_json_field(content, "file_key")
        return VideoContent(file_key=file_key or "")

    @staticmethod
    def _build_sticker(_content: str) -> StickerContent:
        return StickerContent()

    @staticmethod
    def _build_interactive(content: str) -> InteractiveContent:
        """Parse interactive card — try title, then first text element."""
        if not content:
            return InteractiveContent()
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                title = (
                    parsed.get("header", {})
                    .get("title", {})
                    .get("content", "")
                )
                if title:
                    return InteractiveContent(text=title.strip())
                for el in parsed.get("elements", []):
                    text_val = (
                        el.get("text", {}).get("content", "")
                        if isinstance(el.get("text"), dict)
                        else el.get("content", "")
                    )
                    if text_val:
                        return InteractiveContent(text=text_val.strip())
        except (json.JSONDecodeError, AttributeError):
            pass
        return InteractiveContent()

    @staticmethod
    def _build_merge_forward(content: str) -> MergeForwardContent:
        """Pass-through — lark-cli already renders forwarded messages as readable text."""
        text = content.strip()
        return MergeForwardContent(text=text) if text else MergeForwardContent()


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _extract_json_field(content: str, field_name: str) -> str:
    """Extract a single field from a JSON content string.

    Returns empty string if content is not valid JSON or field is missing.
    """
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return str(parsed.get(field_name, ""))
    except (json.JSONDecodeError, AttributeError):
        pass
    return ""
