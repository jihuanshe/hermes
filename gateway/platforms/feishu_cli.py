"""
Feishu/Lark platform adapter using lark-cli (github.com/larksuite/cli).

Instead of the lark-oapi Python SDK, this adapter shells out to the official
``lark-cli`` command-line tool for all Feishu API operations:

- Receiving messages: ``lark-cli event +subscribe --event-types im.message.receive_v1 --compact --quiet``
  outputs NDJSON to stdout (one event per line).
- Sending messages: ``lark-cli im +messages-send --chat-id … --text "…"``
- Replying: ``lark-cli im +messages-reply --message-id … --text "…"``
- Media: ``--image``, ``--file``, ``--audio``, ``--video`` flags (auto-uploads)
- Download: ``lark-cli im +messages-resources-download --message-id … --file-key … --output …``

Auth uses bot identity (App ID + App Secret) via ``lark-cli config init``.
No ``auth login`` needed — that's for user identity only.

Requires:
    lark-cli installed and on $PATH (https://github.com/larksuite/cli)
    lark-cli config init (configures App ID + App Secret)
    Bot capability enabled in Feishu Open Platform console
    im:message:receive_as_bot scope enabled

Configuration in config.yaml:
    platforms:
      feishu_cli:
        enabled: true
        extra:
          bot_open_id: "ou_xxx"        # bot's own open_id (for self-message filtering)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_url,
)
from gateway.platforms.feishu.acl.cli_mapper import CliToDomainMapper
from gateway.platforms.feishu.application.gateway_mapper import (
    domain_to_message_event,
    domain_to_message_type,
)
from gateway.platforms.feishu.application.service import FeishuInboundService
from gateway.platforms.feishu.config import FeishuAdapterConfig
from gateway.platforms.feishu.domain.content import (
    AudioContent,
    FileContent,
    ImageContent,
    VideoContent,
)
from gateway.platforms.feishu.domain.services import InboundMessagePolicy
from gateway.platforms.feishu.domain.value_objects import BotIdentity
from gateway.platforms.feishu.infrastructure.lark_cli_client import LarkCliClient
from gateway.platforms.feishu_dedup import MessageDeduplicator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = 4000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
SENDER_NAME_TTL_SECONDS = 24 * 60 * 60  # 24 hours — names rarely change
_ACK_EMOJIS = ["OnIt", "Get", "OneSecond", "Typing", "GLANCE"]

# Map lark-cli message_type to our MessageType enum
_MESSAGE_TYPE_MAP: Dict[str, MessageType] = {
    "text": MessageType.TEXT,
    "post": MessageType.TEXT,
    "image": MessageType.PHOTO,
    "file": MessageType.DOCUMENT,
    "audio": MessageType.AUDIO,
    "video": MessageType.VIDEO,
    "sticker": MessageType.STICKER,
    "interactive": MessageType.TEXT,
    "merge_forward": MessageType.TEXT,
}


def check_feishu_cli_requirements() -> bool:
    """Check if lark-cli is installed and optionally authenticated.

    Returns True if lark-cli is on $PATH. Does NOT verify authentication
    (that happens lazily on first connect to avoid slow startup checks).
    """
    if shutil.which("lark-cli") is None:
        logger.debug("lark-cli not found on $PATH")
        return False
    return True


async def _run_cli(
    args: List[str],
    *,
    timeout: float = 30.0,
    input_data: Optional[bytes] = None,
) -> tuple[int, str, str]:
    """Run a lark-cli command and return (returncode, stdout, stderr)."""
    cmd = ["lark-cli"] + args
    logger.debug("[FeishuCli] Running: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data),
            timeout=timeout,
        )
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        logger.warning("[FeishuCli] Command timed out: %s", " ".join(cmd[:6]))
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "lark-cli not found"
    except Exception as e:
        return -1, "", str(e)


def _parse_cli_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from lark-cli stdout, tolerating trailing junk.

    lark-cli wraps responses as ``{"ok": true, "data": {...}}``.
    This function unwraps the ``data`` envelope when present so callers
    can access ``message_id`` etc. directly.
    """
    stdout = stdout.strip()
    if not stdout:
        return None
    parsed = None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        # Try first line only (some commands emit extra logging)
        first_line = stdout.split("\n", 1)[0].strip()
        try:
            parsed = json.loads(first_line)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict) and "data" in parsed and isinstance(parsed["data"], dict):
        return parsed["data"]
    return parsed


class FeishuCliAdapter(BasePlatformAdapter):
    """Feishu adapter powered by lark-cli subprocess calls.

    Uses ``lark-cli event +subscribe`` for inbound messages (NDJSON over
    stdout) and ``lark-cli im +messages-*`` commands for outbound messages.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.FEISHU_CLI)

        self._cfg = FeishuAdapterConfig.from_platform(config.extra)
        self._bot_open_id: str = self._cfg.bot.open_id
        self._bot_name: str = self._cfg.bot.name

        self._subscribe_proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None

        # Message deduplication (staleness + message-ID + content fingerprint)
        self._deduplicator = MessageDeduplicator(self._cfg.dedup)
        # ACK reaction tracking: message_id -> reaction_id
        self._pending_reactions: Dict[str, str] = {}
        # Sender name cache: open_id -> (display_name, expire_at)
        self._sender_name_cache: Dict[str, tuple[str, float]] = {}

        # DDD pipeline: inbound service + CLI client
        bot_identity = BotIdentity(
            open_id=self._cfg.bot.open_id,
            name=self._cfg.bot.name,
        )
        self._cli_client = LarkCliClient(name=self.name)
        self._inbound_service = FeishuInboundService(
            mapper=CliToDomainMapper(),
            policy=InboundMessagePolicy(bot_identity),
            deduplicator=self._deduplicator,
        )

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self) -> bool:
        """Start the lark-cli event subscribe subprocess."""
        if not check_feishu_cli_requirements():
            logger.warning(
                "[%s] lark-cli not found. Install from https://github.com/larksuite/cli",
                self.name,
            )
            return False

        # Quick config check — bot identity only needs config init (App ID + Secret),
        # NOT auth login (that's for user identity).
        rc, stdout, stderr = await _run_cli(["config", "init", "--dry-run"], timeout=10.0)
        if rc != 0:
            logger.warning(
                "[%s] lark-cli not configured (run 'lark-cli config init'): %s",
                self.name,
                stderr.strip()[:200],
            )
            # Don't hard-fail — the subscribe command itself will error if config is bad

        # Auto-detect bot name and open_id from /bot/v3/info if not configured
        if not self._bot_name or not self._bot_open_id:
            await self._fetch_bot_info()

        try:
            self._reader_task = asyncio.create_task(self._subscribe_loop())
            self._mark_connected()
            logger.info("[%s] Connected via lark-cli event subscribe (bot=%s)",
                        self.name, self._bot_name or "unknown")
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _fetch_bot_info(self) -> None:
        """Fetch bot name and open_id via /bot/v3/info API."""
        info = await self._cli_client.fetch_bot_info()
        if not info:
            logger.warning("[%s] Failed to fetch bot info", self.name)
            return
        if not self._bot_open_id and info.get("open_id"):
            self._bot_open_id = info["open_id"]
        if not self._bot_name and info.get("app_name"):
            self._bot_name = info["app_name"]
        logger.info("[%s] Bot info: name=%s, open_id=%s",
                    self.name, self._bot_name, self._bot_open_id)

        # Refresh inbound service with updated bot identity
        bot_identity = BotIdentity(
            open_id=self._bot_open_id,
            name=self._bot_name,
        )
        self._inbound_service = FeishuInboundService(
            mapper=CliToDomainMapper(),
            policy=InboundMessagePolicy(bot_identity),
            deduplicator=self._deduplicator,
        )

    async def _subscribe_loop(self) -> None:
        """Spawn the subscribe subprocess with auto-reconnection on failure."""
        backoff_idx = 0
        while self._running:
            try:
                await self._run_subscribe()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Subscribe process error: %s", self.name, e)

            if not self._running:
                return

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def _run_subscribe(self) -> None:
        """Run a single subscribe subprocess session, reading NDJSON lines."""
        cmd = [
            "lark-cli", "event", "+subscribe",
            "--event-types", "im.message.receive_v1",
            "--compact", "--quiet",
            "--as", "bot",
        ]
        logger.debug("[%s] Starting subscribe: %s", self.name, " ".join(cmd))

        self._subscribe_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )

        try:
            assert self._subscribe_proc.stdout is not None
            while self._running:
                line = await self._subscribe_proc.stdout.readline()
                if not line:
                    # EOF — subprocess exited
                    break
                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("[%s] Non-JSON line from subscribe: %s", self.name, line_str[:200])
                    continue
                await self._on_event(data)
        finally:
            if self._subscribe_proc and self._subscribe_proc.returncode is None:
                try:
                    self._subscribe_proc.terminate()
                    await asyncio.wait_for(self._subscribe_proc.wait(), timeout=5.0)
                except Exception:
                    try:
                        self._subscribe_proc.kill()
                    except Exception:
                        pass
            rc = self._subscribe_proc.returncode if self._subscribe_proc else -1
            if rc and rc != 0 and self._running:
                # Read stderr for diagnostics
                stderr_text = ""
                if self._subscribe_proc and self._subscribe_proc.stderr:
                    try:
                        stderr_bytes = await asyncio.wait_for(
                            self._subscribe_proc.stderr.read(), timeout=2.0
                        )
                        stderr_text = stderr_bytes.decode(errors="replace").strip()[:300]
                    except Exception:
                        pass
                logger.warning("[%s] Subscribe exited with code %d: %s", self.name, rc, stderr_text)
            self._subscribe_proc = None

    async def disconnect(self) -> None:
        """Stop the subscribe subprocess and clean up."""
        self._running = False
        self._mark_disconnected()

        if self._subscribe_proc and self._subscribe_proc.returncode is None:
            try:
                self._subscribe_proc.terminate()
            except Exception:
                pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        self._subscribe_proc = None
        self._deduplicator.clear()
        logger.info("[%s] Disconnected", self.name)

    # -- Inbound message processing -----------------------------------------

    async def _on_event(self, data: Dict[str, Any]) -> None:
        """Process a single NDJSON event from lark-cli subscribe.

        Delegates parsing, policy, and dedup to the DDD inbound service.
        Handles media download and sender name resolution (IO), then
        builds a framework ``MessageEvent`` and dispatches it.
        """
        result = self._inbound_service.process_raw_event(data)
        if result.filtered:
            logger.debug("[%s] Filtered: %s", self.name, result.filter_reason)
            return

        message = result.message
        assert message is not None  # guaranteed when not filtered

        msg_id = str(message.message_id)
        text = message.content.text
        media_urls: List[str] = []
        media_types: List[str] = []

        # Media download based on content kind
        if isinstance(message.content, ImageContent):
            cached = await self._download_resource(msg_id, message.content.image_key, ext=".jpg")
            if cached:
                media_urls.append(cached)
                media_types.append("image")
        elif isinstance(message.content, FileContent):
            fn = message.content.file_name
            ext = Path(fn).suffix or ".bin"
            cached = await self._download_resource(msg_id, message.content.file_key, ext=ext, filename=fn)
            if cached:
                media_urls.append(cached)
                media_types.append("document")
        elif isinstance(message.content, AudioContent):
            cached = await self._download_resource(msg_id, message.content.file_key, ext=".ogg")
            if cached:
                media_urls.append(cached)
                media_types.append("audio")
        elif isinstance(message.content, VideoContent):
            cached = await self._download_resource(msg_id, message.content.file_key, ext=".mp4")
            if cached:
                media_urls.append(cached)
                media_types.append("video")

        if not text and not media_urls:
            logger.debug("[%s] Empty message %s, skipping", self.name, msg_id)
            return

        sender_name = await self._resolve_sender_name(message.sender.open_id)
        message_type = domain_to_message_type(message)

        source = self.build_source(
            chat_id=str(message.conversation.chat_id),
            chat_type=message.conversation.chat_type,
            user_id=message.sender.open_id,
            user_name=sender_name,
            thread_id=message.conversation.thread_id,
        )

        event = domain_to_message_event(
            message,
            text=text,
            message_type=message_type,
            source=source,
            sender_name=sender_name,
            media_urls=media_urls,
            media_types=media_types,
            raw_message=data,
        )

        logger.debug(
            "[%s] Message from %s in %s (%s): %s",
            self.name, message.sender.open_id[:12],
            str(message.conversation.chat_id)[:12],
            message.content.kind, text[:50],
        )
        await self.handle_message(event)

    # -- Backward-compat static helpers (used by tests) ---------------------

    @staticmethod
    def _extract_text(raw_type: str, content: str) -> str:
        """Extract plain text from lark-cli compact event content.

        Retained for backward compatibility — delegates to ``CliToDomainMapper``.
        """
        mapper = CliToDomainMapper()
        built = mapper._build_content(raw_type, content)
        return built.text

    @staticmethod
    def _extract_file_key(content: str, key_name: str) -> str:
        """Extract a file_key/image_key from content JSON."""
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed.get(key_name, "")
        except (json.JSONDecodeError, AttributeError):
            pass
        return ""

    @staticmethod
    def _extract_field(content: str, field_name: str) -> str:
        """Extract an arbitrary field from content JSON."""
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return str(parsed.get(field_name, ""))
        except (json.JSONDecodeError, AttributeError):
            pass
        return ""

    async def _download_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        ext: str = ".bin",
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """Download a message resource via lark-cli and cache it locally."""
        return await self._cli_client.download_resource(
            message_id, file_key, ext=ext, filename=filename,
        )

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message via lark-cli."""
        if not content:
            return SendResult(success=False, error="Empty content")

        # Truncate to platform limit
        content = content[:self.MAX_MESSAGE_LENGTH]

        if reply_to:
            args = [
                "im", "+messages-reply",
                "--message-id", reply_to,
                "--text", content,
                "--as", "bot",
            ]
        else:
            args = [
                "im", "+messages-send",
                "--chat-id", chat_id,
                "--text", content,
                "--as", "bot",
            ]

        rc, stdout, stderr = await _run_cli(args)
        if rc != 0:
            error_msg = stderr.strip()[:200] or f"lark-cli exit code {rc}"
            logger.warning("[%s] Send failed: %s", self.name, error_msg)
            return SendResult(success=False, error=error_msg, retryable=rc == -1)

        result = _parse_cli_json(stdout)
        msg_id = result.get("message_id", "") if result else ""
        return SendResult(success=True, message_id=msg_id, raw_response=result)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Feishu does not support typing indicators for bots."""
        pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image by downloading the URL to a temp file, then uploading via lark-cli."""
        try:
            cached_path = await cache_image_from_url(image_url)
        except Exception as e:
            logger.warning("[%s] Failed to download image %s: %s", self.name, image_url[:80], e)
            # Fallback to sending URL as text
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self.send(chat_id=chat_id, content=text, reply_to=reply_to)

        result = await self._send_media(chat_id, "--image", cached_path, reply_to=reply_to)

        if caption and result.success:
            await self.send(chat_id=chat_id, content=caption, reply_to=reply_to)

        return result

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file via lark-cli."""
        result = await self._send_media(chat_id, "--image", image_path, reply_to=reply_to)
        if caption and result.success:
            await self.send(chat_id=chat_id, content=caption, reply_to=reply_to)
        return result

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file via lark-cli."""
        result = await self._send_media(chat_id, "--audio", audio_path, reply_to=reply_to)
        if caption and result.success:
            await self.send(chat_id=chat_id, content=caption, reply_to=reply_to)
        return result

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file/document via lark-cli."""
        result = await self._send_media(chat_id, "--file", file_path, reply_to=reply_to)
        if caption and result.success:
            await self.send(chat_id=chat_id, content=caption, reply_to=reply_to)
        return result

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video via lark-cli."""
        result = await self._send_media(chat_id, "--video", video_path, reply_to=reply_to)
        if caption and result.success:
            await self.send(chat_id=chat_id, content=caption, reply_to=reply_to)
        return result

    async def _send_media(
        self,
        chat_id: str,
        flag: str,
        file_path: str,
        *,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a media file via lark-cli im +messages-send --<type> <path>."""
        if not Path(file_path).exists():
            return SendResult(success=False, error=f"File not found: {file_path}")

        if reply_to:
            args = [
                "im", "+messages-reply",
                "--message-id", reply_to,
                flag, file_path,
                "--as", "bot",
            ]
        else:
            args = [
                "im", "+messages-send",
                "--chat-id", chat_id,
                flag, file_path,
                "--as", "bot",
            ]

        rc, stdout, stderr = await _run_cli(args, timeout=120.0)
        if rc != 0:
            error_msg = stderr.strip()[:200] or f"lark-cli exit code {rc}"
            logger.warning("[%s] Media send failed (%s): %s", self.name, flag, error_msg)
            return SendResult(success=False, error=error_msg, retryable=rc == -1)

        result = _parse_cli_json(stdout)
        msg_id = result.get("message_id", "") if result else ""
        return SendResult(success=True, message_id=msg_id, raw_response=result)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Edit a previously sent message via lark-cli API."""
        rc, stdout, stderr = await _run_cli([
            "api", "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            "--data", json.dumps({
                "msg_type": "text",
                "content": json.dumps({"text": content[:self.MAX_MESSAGE_LENGTH]}),
            }),
            "--as", "bot",
        ])
        if rc != 0:
            error_msg = stderr.strip()[:200] or f"lark-cli exit code {rc}"
            return SendResult(success=False, error=error_msg)
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get basic info about a Feishu chat via lark-cli."""
        rc, stdout, stderr = await _run_cli([
            "api", "GET",
            f"/open-apis/im/v1/chats/{chat_id}",
            "--as", "bot",
        ], timeout=15.0)

        if rc == 0:
            result = _parse_cli_json(stdout)
            if result and isinstance(result, dict):
                data = result.get("data", result)
                return {
                    "name": data.get("name", chat_id),
                    "type": data.get("chat_mode", "dm"),
                    "chat_id": chat_id,
                }

        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    def format_message(self, content: str) -> str:
        """Feishu supports markdown natively — pass through."""
        return content

    # -- Sender name resolution --------------------------------------------

    async def _resolve_sender_name(self, sender_id: str) -> Optional[str]:
        """Resolve an open_id to a display name via lark-cli contact, with 24h cache."""
        if not sender_id:
            return None

        now = time.time()
        cached = self._sender_name_cache.get(sender_id)
        if cached is not None:
            name, expire_at = cached
            if now < expire_at:
                return name

        name = await self._cli_client.resolve_sender_name(sender_id)
        if name:
            self._sender_name_cache[sender_id] = (name, now + SENDER_NAME_TTL_SECONDS)
            return name

        logger.debug("[%s] Could not resolve name for %s", self.name, sender_id[:16])
        return None

    # -- Processing lifecycle hooks -----------------------------------------

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add a random ACK emoji reaction when processing begins."""
        if event.message_id:
            await self._add_ack_reaction(event.message_id)

    async def on_processing_complete(self, event: MessageEvent, success: bool) -> None:
        """Remove the ACK emoji reaction when processing finishes."""
        if event.message_id:
            await self._remove_ack_reaction(event.message_id)

    async def _add_ack_reaction(self, message_id: str) -> None:
        """Add a random emoji reaction to signal the message was received."""
        emoji = random.choice(_ACK_EMOJIS)
        reaction_id = await self._cli_client.add_reaction(message_id, emoji)
        if reaction_id:
            self._pending_reactions[message_id] = reaction_id

    async def _remove_ack_reaction(self, message_id: str) -> None:
        """Remove a previously added ACK emoji reaction."""
        reaction_id = self._pending_reactions.pop(message_id, "")
        if not reaction_id:
            return
        await self._cli_client.remove_reaction(message_id, reaction_id)
