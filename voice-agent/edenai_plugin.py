"""Eden AI STT and TTS plugins for LiveKit voice agents.

Eden AI's audio APIs are not OpenAI-compatible, so this module wraps them in
the livekit.agents STT/TTS abstract base classes.

- STT: POST /v2/audio/speech_to_text_async (launch) + poll
- TTS: POST /v2/audio/text_to_speech (sync, returns base64-encoded audio)

LLM is handled separately via the openai plugin with a custom base_url since
Eden AI exposes /v2/llm/chat/completions in OpenAI Chat Completions format.
"""

from __future__ import annotations

import asyncio
import base64
import io
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
from livekit.agents.types import NotGivenOr, NOT_GIVEN
from livekit.agents.utils import AudioBuffer

logger = logging.getLogger("edenai")

EDENAI_BASE_URL = "https://api.edenai.run/v2"


def _provider_and_model(spec: str) -> tuple[str, str | None]:
    """Parse "provider/model" or just "provider" into (provider, model)."""
    if "/" in spec:
        provider, model = spec.split("/", 1)
        return provider, model
    return spec, None


@dataclass
class _STTOptions:
    provider: str
    model: str | None
    language: str


class EdenAISTT(stt.STT):
    """Eden AI speech-to-text via the async launch + poll endpoints."""

    def __init__(
        self,
        *,
        provider_spec: str = "deepgram/nova-3",
        language: str = "en-US",
        api_key: str | None = None,
        poll_interval: float = 0.3,
        poll_timeout: float = 30.0,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        provider, model = _provider_and_model(provider_spec)
        self._opts = _STTOptions(provider=provider, model=model, language=language)
        self._api_key = api_key or os.environ.get("EDENAI_API_KEY")
        if not self._api_key:
            raise ValueError("EDENAI_API_KEY is required for EdenAISTT")
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._session = http_session

    @property
    def model(self) -> str:
        return f"{self._opts.provider}/{self._opts.model or 'default'}"

    @property
    def provider(self) -> str:
        return f"edenai/{self._opts.provider}"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = utils.http_context.http_session()
        return self._session

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        lang = language if language and language is not NOT_GIVEN else self._opts.language
        wav_bytes = rtc.combine_audio_frames(buffer).to_wav_bytes()

        session = self._ensure_session()
        headers = {"Authorization": f"Bearer {self._api_key}"}

        form = aiohttp.FormData()
        form.add_field("providers", self._opts.provider)
        if self._opts.model:
            form.add_field("model", self._opts.model)
        form.add_field("language", lang)
        form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")

        try:
            async with session.post(
                f"{EDENAI_BASE_URL}/audio/speech_to_text_async",
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=conn_options.timeout),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise APIStatusError(
                        message=f"Eden AI STT launch failed: {body}",
                        status_code=resp.status,
                    )
                launch = await resp.json()
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        job_id = launch.get("public_id") or launch.get("id")
        if not job_id:
            raise APIStatusError(
                message=f"Eden AI STT did not return a job id: {launch}",
                status_code=502,
            )

        # Poll until finished.
        deadline = asyncio.get_event_loop().time() + self._poll_timeout
        while True:
            await asyncio.sleep(self._poll_interval)
            if asyncio.get_event_loop().time() > deadline:
                raise APITimeoutError()
            try:
                async with session.get(
                    f"{EDENAI_BASE_URL}/audio/speech_to_text_async/{job_id}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=conn_options.timeout),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise APIStatusError(
                            message=f"Eden AI STT poll failed: {body}",
                            status_code=resp.status,
                        )
                    payload = await resp.json()
            except asyncio.TimeoutError as e:
                raise APITimeoutError() from e
            except aiohttp.ClientError as e:
                raise APIConnectionError() from e

            status = payload.get("status")
            if status == "finished":
                results = payload.get("results", {}) or {}
                provider_result = results.get(self._opts.provider) or {}
                text = (provider_result.get("text") or "").strip()
                return stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=lang, text=text)],
                )
            if status == "failed":
                raise APIStatusError(
                    message=f"Eden AI STT job failed: {payload}",
                    status_code=502,
                )


@dataclass
class _TTSOptions:
    provider: str
    model: str | None
    voice: str
    language: str
    sample_rate: int


class EdenAITTS(tts.TTS):
    """Eden AI text-to-speech via the synchronous /v2/audio/text_to_speech endpoint.

    The endpoint returns base64-encoded audio. We decode and re-emit as a chunked
    stream so the LiveKit pipeline can play it.
    """

    def __init__(
        self,
        *,
        provider_spec: str = "elevenlabs/eleven_flash_v2_5",
        voice: str = "FEMALE",
        language: str = "en-US",
        api_key: str | None = None,
        sample_rate: int = 24000,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        provider, model = _provider_and_model(provider_spec)
        self._opts = _TTSOptions(
            provider=provider,
            model=model,
            voice=voice,
            language=language,
            sample_rate=sample_rate,
        )
        self._api_key = api_key or os.environ.get("EDENAI_API_KEY")
        if not self._api_key:
            raise ValueError("EDENAI_API_KEY is required for EdenAITTS")
        self._session = http_session

    @property
    def model(self) -> str:
        return f"{self._opts.provider}/{self._opts.model or 'default'}"

    @property
    def provider(self) -> str:
        return f"edenai/{self._opts.provider}"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = utils.http_context.http_session()
        return self._session

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "_EdenAIChunkedStream":
        return _EdenAIChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )


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
        session = self._edenai_tts._ensure_session()
        headers = {
            "Authorization": f"Bearer {self._edenai_tts._api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "providers": opts.provider,
            "text": self._input_text,
            "language": opts.language,
            "option": opts.voice,
        }
        if opts.model:
            body["model"] = opts.model
        # Request MP3 since most providers default to it; we'll decode to PCM below.
        body["audio_format"] = "mp3"

        request_id = utils.shortuuid()
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=opts.sample_rate,
            num_channels=1,
            mime_type="audio/mp3",
            stream=False,
        )

        try:
            async with session.post(
                f"{EDENAI_BASE_URL}/audio/text_to_speech",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._conn_options.timeout),
            ) as resp:
                if resp.status >= 400:
                    error_body = await resp.text()
                    raise APIStatusError(
                        message=f"Eden AI TTS failed: {error_body}",
                        status_code=resp.status,
                    )
                payload = await resp.json()
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        provider_result = payload.get(opts.provider) or {}
        if provider_result.get("status") == "fail":
            raise APIStatusError(
                message=f"Eden AI TTS provider error: {provider_result.get('error')}",
                status_code=502,
            )
        audio_b64 = provider_result.get("audio")
        if not audio_b64:
            raise APIStatusError(
                message=f"Eden AI TTS returned no audio: {payload}",
                status_code=502,
            )
        audio_bytes = base64.b64decode(audio_b64)
        output_emitter.push(audio_bytes)
        output_emitter.flush()
