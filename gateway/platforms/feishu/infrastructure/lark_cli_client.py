"""
Infrastructure layer — typed lark-cli subprocess client.

Wraps raw subprocess calls behind typed async methods.  All IO with
the ``lark-cli`` binary happens here.  The rest of the codebase never
touches ``asyncio.create_subprocess_exec`` for lark-cli directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.platforms.base import (
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level CLI runner
# ---------------------------------------------------------------------------

def check_cli_available() -> bool:
    """Check if ``lark-cli`` is on ``$PATH``."""
    return shutil.which("lark-cli") is not None


async def run_cli(
    args: List[str],
    *,
    timeout: float = 30.0,
    input_data: Optional[bytes] = None,
) -> tuple[int, str, str]:
    """Run a lark-cli command and return ``(returncode, stdout, stderr)``."""
    cmd = ["lark-cli"] + args
    logger.debug("[LarkCli] Running: %s", " ".join(cmd))
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
        logger.warning("[LarkCli] Command timed out: %s", " ".join(cmd[:6]))
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "lark-cli not found"
    except Exception as e:
        return -1, "", str(e)


def parse_cli_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from lark-cli stdout, tolerating trailing junk.

    lark-cli wraps responses as ``{"ok": true, "data": {...}}``.
    This function unwraps the ``data`` envelope when present.
    """
    stdout = stdout.strip()
    if not stdout:
        return None
    parsed = None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        first_line = stdout.split("\n", 1)[0].strip()
        try:
            parsed = json.loads(first_line)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict) and "data" in parsed and isinstance(parsed["data"], dict):
        return parsed["data"]
    return parsed


# ---------------------------------------------------------------------------
# Typed CLI client
# ---------------------------------------------------------------------------

class LarkCliClient:
    """High-level async client for lark-cli operations.

    Groups related CLI calls into typed methods.  All methods return
    structured results instead of raw ``(rc, stdout, stderr)`` tuples.
    """

    def __init__(self, *, name: str = "FeishuCli") -> None:
        self._name = name

    # -- Bot info -----------------------------------------------------------

    async def fetch_bot_info(self) -> Dict[str, str]:
        """Fetch bot name and open_id via ``/bot/v3/info``.

        Returns ``{"open_id": "...", "app_name": "..."}`` or empty dict.
        """
        rc, stdout, _ = await run_cli([
            "api", "GET", "/open-apis/bot/v3/info", "--as", "bot",
        ], timeout=10.0)
        if rc != 0:
            return {}
        data = parse_cli_json(stdout)
        if not data:
            return {}
        bot = data.get("bot", data)
        return {
            "open_id": bot.get("open_id", ""),
            "app_name": bot.get("app_name", ""),
        }

    # -- Media download -----------------------------------------------------

    async def download_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        ext: str = ".bin",
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """Download a message resource and cache it locally.

        Returns the cached file path, or ``None`` on failure.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / f"resource{ext}"
            rc, _, stderr = await run_cli(
                [
                    "im", "+messages-resources-download",
                    "--message-id", message_id,
                    "--file-key", file_key,
                    "--output", str(out_path),
                    "--as", "bot",
                ],
                timeout=60.0,
            )
            if rc != 0 or not out_path.exists():
                logger.warning(
                    "[%s] Failed to download resource %s: %s",
                    self._name, file_key, stderr.strip()[:200],
                )
                return None

            data = out_path.read_bytes()
            if not data:
                return None

            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                return cache_image_from_bytes(data, ext)
            elif ext in (".ogg", ".mp3", ".wav", ".m4a", ".opus"):
                return cache_audio_from_bytes(data, ext)
            else:
                return cache_document_from_bytes(data, filename or f"resource{ext}")

    # -- Reactions ----------------------------------------------------------

    async def add_reaction(self, message_id: str, emoji: str) -> str:
        """Add an emoji reaction. Returns reaction_id or empty string."""
        rc, stdout, stderr = await run_cli([
            "api", "POST",
            f"/open-apis/im/v1/messages/{message_id}/reactions",
            "--data", json.dumps({"reaction_type": {"emoji_type": emoji}}),
            "--as", "bot",
        ], timeout=10.0)
        if rc != 0:
            logger.warning("[%s] ACK reaction failed: %s", self._name, stderr.strip()[:200])
            return ""
        result = parse_cli_json(stdout)
        return result.get("reaction_id", "") if result else ""

    async def remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        """Remove a previously added reaction. Returns True on success."""
        rc, _, stderr = await run_cli([
            "api", "DELETE",
            f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
            "--as", "bot",
        ], timeout=10.0)
        if rc != 0:
            logger.warning("[%s] Remove reaction failed: %s", self._name, stderr.strip()[:200])
        return rc == 0

    # -- Contact resolution -------------------------------------------------

    async def resolve_sender_name(self, open_id: str) -> Optional[str]:
        """Resolve an open_id to a display name via lark-cli contact.

        Returns ``None`` if the lookup fails.
        """
        rc, stdout, _ = await run_cli([
            "contact", "+get-user",
            "--user-id", open_id,
            "--user-id-type", "open_id",
            "--as", "bot",
        ], timeout=5.0)
        if rc != 0:
            return None
        data = parse_cli_json(stdout)
        if data:
            user = data.get("user", data)
            name = user.get("name") or user.get("display_name") or user.get("en_name")
            if name and isinstance(name, str):
                return name.strip() or None
        return None
