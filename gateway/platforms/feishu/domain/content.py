"""
Feishu message content types — discriminated union via Pydantic.

Each Feishu message type (text, post, image, file, audio, video, sticker,
interactive, merge_forward) maps to a typed content model.  Unknown types
fall back to ``UnsupportedContent``.

The ``MessageContent`` union uses Pydantic's ``discriminator="kind"`` for
zero-branch-overhead deserialization.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _ContentModel(BaseModel):
    """Base for all content value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Concrete content types
# ---------------------------------------------------------------------------

class TextContent(_ContentModel):
    """Plain text or parsed rich-text (post) content."""

    kind: Literal["text"] = "text"
    text: str


class ImageContent(_ContentModel):
    """An image attachment with a download key."""

    kind: Literal["image"] = "image"
    image_key: str
    text: str = "[Image]"


class FileContent(_ContentModel):
    """A file/document attachment."""

    kind: Literal["file"] = "file"
    file_key: str
    file_name: str = "attachment"
    text: str = ""

    def __init__(self, **data):
        if not data.get("text") and data.get("file_name"):
            data["text"] = f"[File: {data['file_name']}]"
        elif not data.get("text"):
            data["text"] = "[File: attachment]"
        super().__init__(**data)


class AudioContent(_ContentModel):
    """An audio attachment."""

    kind: Literal["audio"] = "audio"
    file_key: str
    text: str = "[Audio]"


class VideoContent(_ContentModel):
    """A video attachment."""

    kind: Literal["video"] = "video"
    file_key: str
    text: str = "[Video]"


class StickerContent(_ContentModel):
    """A sticker — no meaningful text payload."""

    kind: Literal["sticker"] = "sticker"
    text: str = "[Sticker]"


class InteractiveContent(_ContentModel):
    """An interactive card message."""

    kind: Literal["interactive"] = "interactive"
    text: str = "[Interactive message]"


class MergeForwardContent(_ContentModel):
    """Forwarded/merged messages — lark-cli renders these as readable text."""

    kind: Literal["merge_forward"] = "merge_forward"
    text: str = "[Forwarded messages]"


class UnsupportedContent(_ContentModel):
    """Catch-all for unknown or future message types."""

    kind: Literal["unsupported"] = "unsupported"
    raw_type: str = ""
    text: str = ""


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

MessageContent = Annotated[
    TextContent
    | ImageContent
    | FileContent
    | AudioContent
    | VideoContent
    | StickerContent
    | InteractiveContent
    | MergeForwardContent
    | UnsupportedContent,
    Field(discriminator="kind"),
]

# Map from Feishu raw message_type to content kind literal
FEISHU_TYPE_TO_KIND: dict[str, str] = {
    "text": "text",
    "post": "text",  # post is parsed to plain text
    "image": "image",
    "file": "file",
    "audio": "audio",
    "video": "video",
    "sticker": "sticker",
    "interactive": "interactive",
    "merge_forward": "merge_forward",
}
