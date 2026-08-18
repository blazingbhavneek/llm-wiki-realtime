"""Client for Irodori's OpenAI-compatible, non-streaming TTS endpoint.

Irodori returns a WAV body from ``POST /v1/audio/speech``.  It is almost
OpenAI compatible, but it rejects OpenAI's ``stream_format="audio"`` and only
accepts ``stream_format="sse"`` when that optional field is present.  The
stock LiveKit OpenAI TTS client therefore selects its SSE parser for the custom
``irodori-tts`` model and never sees the returned WAV bytes.  This adapter
omits that extension and hands the WAV response directly to LiveKit.
"""

from __future__ import annotations

import asyncio
import re

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    tts as lk_tts,
    utils,
)

from app.tts.base import TTSProvider, TTSSettings

# The endpoint's Japanese greeting measured about 3.5 seconds for 20
# characters.  150 gives slow/expressive speech headroom below its 30-second
# maximum while still keeping requests large enough to avoid needless latency.
MAX_REQUEST_CHARS = 150

# The model id is fixed; the voice is now ``settings.voice`` (TTS_VOICE, or a
# per-session override carried in the LiveKit dispatch metadata) like every
# other provider. The remaining knobs stay fixed until we deliberately expose
# controls for them - they are not read from TTS_* environment variables.
IRODORI_MODEL = "irodori-tts"
IRODORI_SEED = 20260818
IRODORI_SPEED = 1.0
IRODORI_CFG_SCALE_TEXT = 7.0
IRODORI_CFG_SCALE_SPEAKER = 9.0


def _split_text(text: str, *, max_chars: int = MAX_REQUEST_CHARS) -> list[str]:
    """Split Japanese text below Irodori's 30-second synthesis limit.

    The model's duration varies with punctuation and delivery style, so this is
    deliberately a conservative character cap.  Preserve full sentences where
    possible, then Japanese clause boundaries, and only hard-split unbroken
    text as a last resort.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    sentences = re.findall(r"[^。！？!?]+[。！？!?]*", text) or [text]
    chunks: list[str] = []
    current = ""

    def add(piece: str) -> None:
        nonlocal current
        if not piece:
            return
        if current and len(current) + len(piece) > max_chars:
            chunks.append(current)
            current = ""
        if len(piece) <= max_chars:
            current += piece
            return
        if current:
            chunks.append(current)
            current = ""
        # Keep clauses together before falling back to a fixed-width split.
        for clause in re.findall(r"[^、，,]+[、，,]*", piece) or [piece]:
            while len(clause) > max_chars:
                chunks.append(clause[:max_chars])
                clause = clause[max_chars:]
            if current and len(current) + len(clause) > max_chars:
                chunks.append(current)
                current = ""
            current += clause

    for sentence in sentences:
        add(sentence)
    if current:
        chunks.append(current)
    return chunks


class _IrodoriTTS(lk_tts.TTS):
    """Non-streaming WAV client for Irodori's ``/audio/speech`` route."""

    def __init__(self, settings: TTSSettings) -> None:
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=IrodoriTTS.native_sample_rate,
            num_channels=1,
        )
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return IRODORI_MODEL

    @property
    def provider(self) -> str:
        return self._base_url.removeprefix("http://").removeprefix("https://")

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "LiveKit Agents"}
        if self._settings.api_key and self._settings.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> lk_tts.ChunkedStream:
        return _IrodoriChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class _IrodoriChunkedStream(lk_tts.ChunkedStream):
    def __init__(self, *, tts: _IrodoriTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._irodori_tts = tts

    async def _run(self, output_emitter: lk_tts.AudioEmitter) -> None:
        # Each child decodes one WAV response.  The parent forwards its decoded
        # PCM into one stream, so a second WAV header can never be mistaken for
        # audio.  Once the first child has emitted frames, LiveKit can play them
        # while this loop begins the next HTTP synthesis request.
        output_emitter.initialize(
            request_id="irodori-batch",
            sample_rate=IrodoriTTS.native_sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        for chunk in _split_text(self.input_text):
            async with _IrodoriRequestStream(
                tts=self._irodori_tts,
                input_text=chunk,
                conn_options=self._conn_options,
            ) as request_stream:
                async for audio in request_stream:
                    output_emitter.push(audio.frame.data.tobytes())
        output_emitter.flush()


class _IrodoriRequestStream(lk_tts.ChunkedStream):
    """One OpenAI-compatible request, which always returns exactly one WAV."""

    def __init__(self, *, tts: _IrodoriTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._irodori_tts = tts

    async def _run(self, output_emitter: lk_tts.AudioEmitter) -> None:
        payload = {
            "model": IRODORI_MODEL,
            "voice": self._irodori_tts._settings.voice,
            "input": self.input_text,
            "response_format": "wav",
            "speed": IRODORI_SPEED,
            # This is the deployment's one style source.  It is sent for both
            # LLM-generated replies and fixed notices spoken via session.say().
            # "instructions": self._irodori_tts._settings.instructions,
            "instructions": "自然で落ち着いた、親しみやすい日本語で話してください。聞き取りやすい穏やかな抑揚を保ち、明るさは控えめにしてください。早口にならず、英語やローマ字、その他の言語には切り替えないでください",
            # Irodori-specific controls stay nested, as required by its
            # OpenAI-compatible endpoint.  Stronger text and speaker guidance
            # keeps the generated voice closer to the selected reference.
            "irodori": {
                "seed": IRODORI_SEED,
                "cfg_scale_text": IRODORI_CFG_SCALE_TEXT,
                "cfg_scale_speaker": IRODORI_CFG_SCALE_SPEAKER,
            },
        }

        try:
            async with self._irodori_tts._ensure_session().post(
                f"{self._irodori_tts._base_url}/audio/speech",
                json=payload,
                headers=self._irodori_tts._headers(),
                timeout=aiohttp.ClientTimeout(total=60, connect=self._conn_options.timeout),
            ) as response:
                if response.status != 200:
                    raise APIStatusError(
                        await response.text(),
                        status_code=response.status,
                        request_id=response.headers.get("x-request-id"),
                    )

                # Irodori provides a per-request seed rather than OpenAI's request
                # id.  It is still useful for correlating an agent error with its
                # server log, and avoids an opaque "unknown" request in LiveKit.
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "x-irodori-seed", ""
                )
                output_emitter.initialize(
                    request_id=request_id,
                    sample_rate=IrodoriTTS.native_sample_rate,
                    num_channels=1,
                    mime_type="audio/wav",
                )
                async for data in response.content.iter_chunked(16 * 1024):
                    output_emitter.push(data)
                output_emitter.flush()
        except asyncio.TimeoutError as exc:
            raise APIConnectionError("Irodori TTS request timed out") from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError("Irodori TTS request failed") from exc


class IrodoriTTS(TTSProvider):
    name = "irodori"
    hosted_by = "remote"

    default_model = "irodori-tts"
    default_voice = "clone_ref1"
    default_base_url = "http://10.160.144.101:51026/v1"
    default_response_format = "wav"
    native_sample_rate = 48000
    streams_audio = False
    honors_instructions = True
    supports_voice_listing = True

    default_reply_min_chars = 30
    default_report_min_chars = 180

    def build(self, settings: TTSSettings) -> lk_tts.TTS:
        return _IrodoriTTS(settings)
