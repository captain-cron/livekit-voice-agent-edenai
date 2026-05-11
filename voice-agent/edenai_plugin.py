"""Eden AI v3 STT and TTS plugins for LiveKit voice agents.

Eden AI v3 (`api.edenai.run/v3`) has a unified universal-ai endpoint that
accepts a ``model`` like ``audio/speech_to_text_async/<provider>/<model>`` or
``audio/tts/<provider>/<model>``. The chat completions endpoint at
``/v3/chat/completions`` is OpenAI-compatible (used by the LLM via the standard
openai plugin with a base_url override).

STT and TTS aren't OpenAI-compatible, so this module wraps them in the
livekit-agents STT/TTS abstract base classes.

STT flow:
  1. POST /v3/upload (multipart with the WAV bytes) → returns ``file_id`` UUID
  2. POST /v3/universal-ai/async with ``input.file = file_id`` → returns the
     job. In practice the response already includes ``output.text`` because
     small clips finish synchronously, but we still poll if status != success.

TTS flow:
  POST /v3/universal-ai/ with ``input.text`` → returns
  ``output.audio_resource_url``. We then HTTP-GET the mp3 bytes and hand them
  to the LiveKit AudioEmitter (mime_type="audio/mp3"), which decodes to PCM
  internally.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp
from livekit import rtc
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    DEFAULT_API_CONNECT_OPTIONS,
    stt,
    tts,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

logger = logging.getLogger("edenai")

EDENAI_BASE_URL = "https://api.edenai.run/v3"


@dataclass
class _STTOptions:
    model: str
    language: str


class EdenAISTT(stt.STT):
    """Eden AI speech-to-text via /v3/upload + /v3/universal-ai/async."""

    def __init__(
        self,
        *,
        model: str = "audio/speech_to_text_async/deepgram/nova-3",
        language: str = "en-US",
        api_key: str | None = None,
        poll_interval: float = 0.5,
        poll_timeout: float = 30.0,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        self._opts = _STTOptions(model=model, language=language)
        self._api_key = api_key or os.environ.get("EDENAI_API_KEY")
        if not self._api_key:
            raise ValueError("EDENAI_API_KEY is required for EdenAISTT")
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "edenai"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def _upload(self, session: aiohttp.ClientSession, wav_bytes: bytes, timeout: float) -> str:
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
        async with session.post(
            f"{EDENAI_BASE_URL}/upload",
            data=form,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise APIStatusError(message=f"Eden AI upload failed: {body}", status_code=resp.status)
            payload = await resp.json()
        file_id = payload.get("file_id")
        if not file_id:
            raise APIStatusError(message=f"Eden AI upload: missing file_id ({payload})", status_code=502)
        return file_id

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        lang = language if language and language is not NOT_GIVEN else self._opts.language
        wav_bytes = rtc.combine_audio_frames(buffer).to_wav_bytes()

        session = utils.http_context.http_session()
        timeout = conn_options.timeout

        try:
            file_id = await self._upload(session, wav_bytes, timeout)
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        body = {
            "model": self._opts.model,
            "input": {"file": file_id, "language": lang},
            "show_original_response": False,
        }

        try:
            async with session.post(
                f"{EDENAI_BASE_URL}/universal-ai/async",
                json=body,
                headers={**self._headers(), "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status >= 400:
                    err = await resp.text()
                    raise APIStatusError(message=f"Eden AI STT launch failed: {err}", status_code=resp.status)
                payload = await resp.json()
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        # Many short clips return final output synchronously.
        text = self._extract_text(payload)
        if text is not None:
            return self._event(lang, text)

        job_id = payload.get("public_id") or payload.get("id")
        if not job_id:
            raise APIStatusError(
                message=f"Eden AI STT: no public_id and no output ({payload})",
                status_code=502,
            )

        deadline = asyncio.get_event_loop().time() + self._poll_timeout
        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise APITimeoutError()
            await asyncio.sleep(self._poll_interval)
            try:
                async with session.get(
                    f"{EDENAI_BASE_URL}/universal-ai/async/{job_id}",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status >= 400:
                        err = await resp.text()
                        raise APIStatusError(
                            message=f"Eden AI STT poll failed: {err}", status_code=resp.status
                        )
                    payload = await resp.json()
            except asyncio.TimeoutError as e:
                raise APITimeoutError() from e
            except aiohttp.ClientError as e:
                raise APIConnectionError() from e

            text = self._extract_text(payload)
            if text is not None:
                return self._event(lang, text)
            if payload.get("status") == "fail" or payload.get("error"):
                raise APIStatusError(
                    message=f"Eden AI STT job failed: {payload.get('error') or payload}",
                    status_code=502,
                )

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str | None:
        status = payload.get("status")
        output = payload.get("output") or {}
        if status == "success" and isinstance(output, dict) and "text" in output:
            return (output.get("text") or "").strip()
        return None

    @staticmethod
    def _event(lang: str, text: str) -> stt.SpeechEvent:
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang, text=text)],
        )


@dataclass
class _TTSOptions:
    model: str
    language: str
    sample_rate: int


class EdenAITTS(tts.TTS):
    """Eden AI text-to-speech via /v3/universal-ai/."""

    def __init__(
        self,
        *,
        model: str = "audio/tts/elevenlabs/eleven_flash_v2_5",
        language: str = "en-US",
        api_key: str | None = None,
        sample_rate: int = 24000,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._opts = _TTSOptions(model=model, language=language, sample_rate=sample_rate)
        self._api_key = api_key or os.environ.get("EDENAI_API_KEY")
        if not self._api_key:
            raise ValueError("EDENAI_API_KEY is required for EdenAITTS")

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "edenai"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "_EdenAIChunkedStream":
        return _EdenAIChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class _EdenAIChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: EdenAITTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._edenai_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._edenai_tts._opts
        session = utils.http_context.http_session()
        headers = {
            "Authorization": f"Bearer {self._edenai_tts._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": opts.model,
            "input": {"text": self._input_text, "language": opts.language},
            "show_original_response": False,
        }

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=opts.sample_rate,
            num_channels=1,
            mime_type="audio/mp3",
            stream=False,
        )

        try:
            async with session.post(
                f"{EDENAI_BASE_URL}/universal-ai/",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._conn_options.timeout),
            ) as resp:
                if resp.status >= 400:
                    err = await resp.text()
                    raise APIStatusError(
                        message=f"Eden AI TTS failed: {err}", status_code=resp.status
                    )
                payload = await resp.json()
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        if payload.get("status") != "success":
            raise APIStatusError(
                message=f"Eden AI TTS error: {payload.get('error') or payload}",
                status_code=502,
            )

        output = payload.get("output") or {}
        audio_url = output.get("audio_resource_url") or output.get("audio_resource") or output.get("audio")
        if not audio_url or not isinstance(audio_url, str):
            raise APIStatusError(
                message=f"Eden AI TTS: no audio in response ({payload})",
                status_code=502,
            )

        try:
            async with session.get(
                audio_url,
                timeout=aiohttp.ClientTimeout(total=self._conn_options.timeout),
            ) as resp:
                if resp.status >= 400:
                    raise APIStatusError(
                        message=f"Eden AI TTS audio fetch failed (status {resp.status})",
                        status_code=resp.status,
                    )
                audio_bytes = await resp.read()
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        output_emitter.push(audio_bytes)
        output_emitter.flush()
