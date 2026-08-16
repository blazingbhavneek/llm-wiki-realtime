"""Voxtral Mini Realtime on vLLM.

**Verified against a live server (2026-08-16).** The batch path
(``openai.STT(use_realtime=False)``, one ``POST /v1/audio/transcriptions`` per
VAD segment) is what this project actually ran, and against a vLLM box
serving ``mistralai/Voxtral-Mini-4B-Realtime-2602`` it fails hard: every
request comes back ``400 Invalid or unsupported audio file``, even for a
trivially valid WAV. That is not this project's bug - it is architectural.
vLLM ships two distinct model classes for Voxtral
(``vllm/model_executor/models/voxtral.py`` vs. ``voxtral_realtime.py``, model
name ``VoxtralRealtimeGeneration``); the "-Realtime-" checkpoint is the
realtime one, and the batch transcription code path is not the one it is
built to be served through. Point the batch path (``STT_USE_REALTIME``
unset/false) at vLLM serving the *non-realtime* checkpoint
(``mistralai/Voxtral-Mini-4B-...`` without ``-Realtime-``) instead.

**The realtime path speaks vLLM's own protocol, not the OpenAI Realtime API.**
The previous version of this file delegated realtime mode to
``livekit.plugins.openai.STT(use_realtime=True)``, which sends OpenAI's nested
session shape (``session.audio.input.format.rate``, server-side
``turn_detection``) - a dialect vLLM's ``/v1/realtime`` endpoint
(``vllm/entrypoints/speech_to_text/realtime/``) does not speak at all. Reading
that endpoint's own source gives the real, flat, bespoke protocol:

* connect to ``ws://<host>/v1/realtime``; the server immediately sends
  ``{"type": "session.created", ...}``.
* send ``{"type": "session.update", "model": "<model id>"}`` once - required
  before the first commit, or the server answers ``model_not_validated``.
* send ``{"type": "input_audio_buffer.append", "audio": "<base64 PCM16 @
  16kHz mono>"}`` per chunk.
* send ``{"type": "input_audio_buffer.commit", "final": false}`` to start
  generation on whatever is buffered so far, then
  ``{"type": "input_audio_buffer.commit", "final": true}`` to signal this
  utterance's audio is complete - the pair together is what closes one
  generation and produces a ``transcription.done``. The connection is meant to
  be reused for the next utterance after that (append again, commit again),
  which is exactly the one-WS-per-session shape ``NemotronSTT`` already uses.
* the server streams ``{"type": "transcription.delta", "delta": "..."}`` while
  generating and ``{"type": "transcription.done", "text": "...", "usage":
  {...}}`` when done. There is no per-request language field anywhere in this
  protocol - ``STT_LANGUAGE`` has no effect on the realtime path; whatever the
  model does is whatever it does.

So this is the same situation ``NemotronSTT`` exists for: no
OpenAI-compatible plugin speaks this server's real dialect, and something has
to decide when an utterance ends since the protocol itself has no server-side
endpointing. ``VoxtralRealtimeSTT`` below is that client, and - because *we*
control every commit - ``finals_are_utterances`` is True for it the same way
it is for Nemotron: one VAD end-of-speech is one ``commit(final=true)`` pair
is one ``transcription.done``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    stt,
    utils,
    vad,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer, is_given
from livekit.plugins import openai

from app.stt.base import STTProvider, STTSettings

# The batch/realtime choice, made explicit. Default false = what this project
# has actually run: LiveKit's own StreamAdapter segments on VAD and calls
# recognize() once per segment, against the non-realtime Voxtral checkpoint.
USE_REALTIME_ENV = "STT_USE_REALTIME"

NUM_CHANNELS = 1
DEFAULT_SAMPLE_RATE = 16000  # fixed by vLLM's realtime protocol, not configurable per-request


def _use_realtime() -> bool:
    raw = os.getenv(USE_REALTIME_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _realtime_ws_url(base_url: str) -> str:
    """``http://host:port/v1`` -> ``ws://host:port/v1/realtime``."""
    parts = urlsplit(base_url.rstrip("/") + "/realtime")
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


@dataclass
class _STTOptions:
    model: str
    sample_rate: int


class VoxtralRealtimeSTT(stt.STT):
    """Streaming STT against vLLM's own ``/v1/realtime`` WebSocket protocol."""

    def __init__(
        self,
        *,
        # No server-side endpointing exists in this protocol - see the module
        # docstring - so, exactly like NemotronSTT, something has to decide
        # when an utterance ends. That's this VAD, committing on end-of-speech.
        vad: vad.VAD,
        base_url: str = "http://127.0.0.1:8001/v1",
        api_key: str = "EMPTY",
        model: str = "mistralai/Voxtral-Mini-4B-Realtime-2602",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))
        if vad is None:
            raise ValueError("VoxtralRealtimeSTT requires a VAD to mark the end of a turn")
        self._vad = vad
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._opts = _STTOptions(model=model, sample_rate=sample_rate)
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "vllm-realtime"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "LiveKit Agents"}
        if self._api_key and self._api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> SpeechStream:
        # No language field exists in this protocol (see module docstring),
        # so a caller-supplied language is silently a no-op here.
        return SpeechStream(stt=self, conn_options=conn_options)

    def update_options(self, **_kwargs: object) -> None:
        pass

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError(
            "voxtral realtime has no batch endpoint; use STT_USE_REALTIME=false "
            "against a non-realtime Voxtral checkpoint for one-shot recognize()"
        )


class SpeechStream(stt.SpeechStream):
    def __init__(self, *, stt: VoxtralRealtimeSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=stt._opts.sample_rate)
        self._opts = stt._opts
        self._vad = stt._vad
        self._speaking = False

    def _start_speaking(self) -> None:
        if self._speaking:
            return
        self._speaking = True
        self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

    def _stop_speaking(self) -> None:
        if not self._speaking:
            return
        self._speaking = False
        self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        url = _realtime_ws_url(self._stt._base_url)
        try:
            ws = await asyncio.wait_for(
                self._stt._ensure_session().ws_connect(url, headers=self._stt._headers()),
                self._conn_options.timeout,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError) as e:
            raise APIConnectionError(f"cannot reach vllm realtime at {url}") from e

        # Required before the first commit - see handle_event() in
        # vllm/entrypoints/speech_to_text/realtime/connection.py, which
        # answers "model_not_validated" to a commit sent before this.
        await ws.send_str(json.dumps({"type": "session.update", "model": self._opts.model}))
        return ws

    @utils.log_exceptions()
    async def _run(self) -> None:
        ws = await self._connect()
        vad_stream = self._vad.stream()
        closing = False

        async def commit() -> None:
            """Finalize the utterance.

            vLLM's realtime protocol splits this into two messages: ``final:
            false`` starts generation on whatever audio is already buffered,
            ``final: true`` signals no more audio is coming for this
            utterance so the generation can finish and emit
            ``transcription.done``. Sending both is what one Nemotron-style
            "commit" needs to mean here.
            """
            await ws.send_str(
                json.dumps({"type": "input_audio_buffer.commit", "final": False})
            )
            await ws.send_str(json.dumps({"type": "input_audio_buffer.commit", "final": True}))

        @utils.log_exceptions()
        async def send_task() -> None:
            nonlocal closing
            # 50ms per message: small enough that a partial is never waiting
            # on the packetizer, large enough to keep the socket quiet -
            # matches NemotronSTT's own framing.
            bstream = utils.audio.AudioByteStream(
                sample_rate=self._opts.sample_rate,
                num_channels=NUM_CHANNELS,
                samples_per_channel=self._opts.sample_rate // 20,
            )

            async def send_chunk(frame: rtc.AudioFrame) -> None:
                payload = base64.b64encode(frame.data.tobytes()).decode("ascii")
                await ws.send_str(
                    json.dumps({"type": "input_audio_buffer.append", "audio": payload})
                )

            try:
                async for data in self._input_ch:
                    frames: list[rtc.AudioFrame] = []
                    if isinstance(data, rtc.AudioFrame):
                        vad_stream.push_frame(data)
                        frames.extend(bstream.write(data.data.tobytes()))
                    elif isinstance(data, self._FlushSentinel):
                        frames.extend(bstream.flush())
                    for frame in frames:
                        await send_chunk(frame)
            except (aiohttp.ClientError, ConnectionError) as e:
                if closing:
                    return
                raise APIConnectionError("vllm realtime connection closed") from e
            finally:
                vad_stream.end_input()

            # Input is done; finalize whatever the last utterance was, the
            # same way NemotronSTT commits the tail instead of dropping it.
            closing = True
            await commit()

        @utils.log_exceptions()
        async def vad_task() -> None:
            try:
                async for ev in vad_stream:
                    if ev.type == vad.VADEventType.START_OF_SPEECH:
                        self._start_speaking()
                    elif ev.type == vad.VADEventType.END_OF_SPEECH:
                        self._stop_speaking()
                        await commit()
            except (aiohttp.ClientError, ConnectionError) as e:
                if closing:
                    return
                raise APIConnectionError("vllm realtime connection closed") from e

        @utils.log_exceptions()
        async def recv_task() -> None:
            # Deltas are incremental suffixes (protocol.py:
            # "Incremental transcription text"), so they accumulate the same
            # way Nemotron's do; transcription.done carries the full text and
            # resets the buffer for the next utterance on this connection.
            partial = ""
            while True:
                msg = await ws.receive()
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    if closing:
                        return
                    raise APIStatusError("vllm realtime connection closed unexpectedly")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                event = json.loads(msg.data)
                etype = event.get("type")

                if etype == "transcription.delta":
                    partial += event.get("delta") or ""
                    if partial:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                                alternatives=[stt.SpeechData(text=partial, language="")],
                            )
                        )
                elif etype == "transcription.done":
                    partial = ""
                    text = (event.get("text") or "").strip()
                    if text:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(text=text, language="")],
                            )
                        )
                elif etype == "error":
                    message = event.get("error") or "unknown error"
                    raise APIStatusError(f"vllm realtime error: {message}")
                # "session.created" / "session.update" acks: nothing to do.

        tasks = [
            asyncio.create_task(send_task(), name="voxtral_rt.send"),
            asyncio.create_task(recv_task(), name="voxtral_rt.recv"),
            asyncio.create_task(vad_task(), name="voxtral_rt.vad"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            await utils.aio.cancel_and_wait(*tasks)
            await vad_stream.aclose()
            await ws.close()


class VoxtralProvider(STTProvider):
    """Mistral's Voxtral Mini, served by vLLM.

    Which checkpoint you point this at decides the mode: the "-Realtime-"
    checkpoint only works with ``STT_USE_REALTIME=true`` (see the module
    docstring - its batch endpoint answers 400 to every request); the plain
    checkpoint is what ``STT_USE_REALTIME=false`` (the default) expects.
    """

    name = "voxtral"
    hosted_by = "vllm"

    default_model = "mistralai/Voxtral-Mini-4B-Realtime-2602"
    default_base_url = "http://127.0.0.1:8001/v1"
    default_language = "ja"
    native_sample_rate = 16000

    # Declared for the default (batch) mode. The realtime mode's trio differs,
    # so ask capabilities_for() instead of reading these when STT_USE_REALTIME
    # is on.
    requires_vad = True
    emits_interim = False
    finals_are_utterances = True

    # ``openai.STT`` sends ``LanguageCode(language).language`` - the base tag -
    # so a locale would be truncated anyway. Unlike nemotron there is no
    # exact-locale lookup that turns a wrong key into silent auto-detection.
    language_is_locale = False

    @classmethod
    def capabilities_for(cls, use_realtime: bool) -> dict[str, bool]:
        """The capability trio for a mode, so the wiring layer can ask."""
        if use_realtime:
            return {
                # VoxtralRealtimeSTT commits on VAD end-of-speech itself -
                # there is no server-side endpointing to fall back on.
                "requires_vad": True,
                "emits_interim": True,
                "finals_are_utterances": True,
            }
        return {
            "requires_vad": True,
            "emits_interim": False,
            "finals_are_utterances": True,
        }

    @classmethod
    def use_realtime(cls) -> bool:
        return _use_realtime()

    def build(self, settings: STTSettings, *, vad: object | None = None) -> stt.STT:
        use_realtime = _use_realtime()
        if vad is None:
            raise ValueError("voxtral is segmented by VAD in both modes; pass the session's vad")
        if use_realtime:
            return VoxtralRealtimeSTT(
                model=settings.model,
                base_url=settings.base_url,
                api_key=settings.api_key,
                sample_rate=settings.sample_rate,
                vad=vad,
            )
        return openai.STT(
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            language=settings.language,
            use_realtime=False,
        )
