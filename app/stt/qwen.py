"""Japanese-pinned batch STT for a vLLM-hosted Qwen3-ASR model.

This vLLM deployment accepts the ISO 639-1 language code ``ja`` on the OpenAI
transcription endpoint.  The provider sends that value directly on every
request, disabling language identification.

The STT is non-streaming by design.  LiveKit's existing StreamAdapter uses the
session VAD to split the microphone into utterances, then calls ``recognize``
once for each one.  That gives one final transcript per VAD turn while keeping
Qwen's Japanese decoder constraint intact.
"""

from __future__ import annotations

import asyncio

import aiohttp
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    stt,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

from app.stt.base import STTProvider, STTSettings

QWEN_LANGUAGE = "ja"
DEFAULT_SAMPLE_RATE = 16000


class QwenJapaneseSTT(stt.STT):
    """Qwen3-ASR batch client that always requests Japanese recognition."""

    def __init__(self, *, model: str, base_url: str, api_key: str) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "qwen3-asr"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "LiveKit Agents"}
        if self._api_key and self._api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        # Do not use the caller-provided language: this provider exists
        # specifically to pin Qwen's vLLM endpoint to Japanese.
        del language
        form = aiohttp.FormData()
        form.add_field(
            "file",
            rtc.combine_audio_frames(buffer).to_wav_bytes(),
            filename="audio.wav",
            content_type="audio/wav",
        )
        form.add_field("model", self._model)
        form.add_field("language", QWEN_LANGUAGE)

        try:
            async with self._ensure_session().post(
                f"{self._base_url}/audio/transcriptions",
                data=form,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30, connect=conn_options.timeout),
            ) as response:
                if response.status != 200:
                    raise APIStatusError(await response.text(), status_code=response.status)
                payload = await response.json()
        except asyncio.TimeoutError as exc:
            raise APIConnectionError("Qwen3-ASR transcription timed out") from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError("Qwen3-ASR transcription failed") from exc

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(text=str(payload.get("text") or ""), language=QWEN_LANGUAGE)
            ],
        )


class QwenProvider(STTProvider):
    """Qwen3-ASR served by vLLM, with Japanese forced for every request."""

    name = "qwen"
    hosted_by = "vllm"

    default_model = "Qwen/Qwen3-ASR-1.7B"
    default_base_url = "http://10.160.144.101:51027/v1"
    # Kept in the shared settings for a consistent provider contract. The
    # request itself always uses QWEN_LANGUAGE, not an environment override.
    default_language = QWEN_LANGUAGE
    native_sample_rate = DEFAULT_SAMPLE_RATE

    requires_vad = True
    emits_interim = False
    finals_are_utterances = True
    language_is_locale = False

    def build(self, settings: STTSettings, *, vad: object | None = None) -> stt.STT:
        if vad is None:
            raise ValueError("qwen is segmented by LiveKit VAD; pass the session's vad")
        return QwenJapaneseSTT(
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
        )
