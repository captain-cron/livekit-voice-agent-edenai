"""HTTP client for portals.cx's /api/voice-agent/internal/* callbacks.

The worker uses this to:
  - GET the {session, user, config, funnel} bundle for a session id
  - POST one transcript line per utterance during the call
  - POST an end-of-call sweep that runs action extraction in portals.cx

All calls share a static Bearer token (VOICE_AGENT_INTERNAL_TOKEN) that
must match the value configured on the Vercel side.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("voice-agent.roc-client")


class RocClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("PORTALS_CX_BASE_URL", "")).rstrip("/")
        self.token = token or os.environ.get("VOICE_AGENT_INTERNAL_TOKEN", "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def fetch_session_context(self, session_id: str) -> dict[str, Any] | None:
        if not self.configured:
            logger.warning("RocClient.fetch_session_context: not configured")
            return None
        url = f"{self.base_url}/api/voice-agent/internal/session-context?sessionId={session_id}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        logger.warning(
                            "session-context %s -> %s: %s",
                            session_id, resp.status, body[:200],
                        )
                        return None
                    return await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.warning("session-context %s network error: %s", session_id, e)
            return None

    async def post_transcript(
        self,
        session_id: str,
        speaker: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not self.configured:
            return
        if not text or not text.strip():
            return
        body = {
            "sessionId": session_id,
            "speaker": speaker,
            "text": text.strip()[:5000],
            "spokenAt": datetime.now(timezone.utc).isoformat(),
            "meta": meta or {},
        }
        url = f"{self.base_url}/api/voice-agent/internal/transcript"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    headers=self._headers(),
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status >= 400:
                        body_text = await resp.text()
                        logger.warning(
                            "transcript %s/%s -> %s: %s",
                            session_id, speaker, resp.status, body_text[:200],
                        )
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            # We never block the conversation on transcript persistence —
            # losing a line is annoying but not catastrophic.
            logger.warning("transcript %s network error: %s", session_id, e)

    async def post_end_of_call(self, session_id: str) -> None:
        if not self.configured:
            return
        url = f"{self.base_url}/api/voice-agent/internal/end-of-call"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    headers=self._headers(),
                    json={"sessionId": session_id},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status >= 400:
                        body_text = await resp.text()
                        logger.warning(
                            "end-of-call %s -> %s: %s",
                            session_id, resp.status, body_text[:200],
                        )
                    else:
                        logger.info(
                            "end-of-call %s -> %s", session_id, resp.status,
                        )
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.warning("end-of-call %s network error: %s", session_id, e)
