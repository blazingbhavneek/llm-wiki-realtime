"""LiveKit STT bound to NeMo-Speech.cpp's realtime WebSocket (``/v1/realtime``).

``livekit.plugins.openai.STT`` cannot drive this server. Both mismatches fail
silently rather than erroring, which is why the pipeline looked like a bad ASR
model rather than a bad integration:

* Its batch path sends ``LanguageCode.language`` - the base tag only - so a
  configured ``ja-JP`` arrives as ``ja``. Nemotron 3.5 selects its language
  prompt by exact-match lookup and the GGUF ships ``ja-JP`` and ``ja-JA`` but
  no bare ``ja``, so an unknown key falls back to ``auto`` language detection.
  Confirmed against the running server: ``language=ja`` returns output
  byte-identical to sending no language at all, while ``ja-JP`` is applied.
* Its ``use_realtime=True`` path speaks OpenAI's nested session shape
  (``session.audio.input.format.rate``, ``...transcription.language``) while
  this server reads a flat one (``session.sample_rate``, ``session.language``).
  Neither field would land: the server would hold its 16 kHz default while
  being fed the plugin's 24 kHz PCM.

Speaking the server's own dialect is what makes the model behave like the
streaming model it is: interim deltas arrive while the user is still talking,
and the final no longer costs a from-scratch decode of the whole utterance.

**Finalization is driven by VAD, not by the server's endpointing.** LiveKit
never flushes an STT stream - ``audio_recognition`` only ever calls
``push_frame``, so one stream stays open for the entire session - which means
something has to decide when an utterance ends. The two candidates are not
equivalent:

* The server's ``--endpointing`` fires on trailing token-silence and emits a
  final *per pause*. That is the normal contract for a streaming STT, and
  LiveKit handles it by accumulating finals into one turn
  (``audio_recognition._audio_transcript += ...``). But server.py deliberately
  bypasses turn completion and treats every final as a complete user
  utterance, so mid-sentence pauses would fragment one question into several -
  jfk.wav splits into four finals this way.
* A VAD stream inside this plugin commits the buffer on end-of-speech, so
  there is exactly one final per speech segment. That is the same turn
  boundary the old batch pipeline had (``StreamAdapter`` segments on VAD and
  calls ``recognize()`` once per segment), so the Conductor's semantics are
  unchanged - and unlike session-level turn detection it keeps working while
  an uninterruptible speech plays, which is what server.py needs for barge-in.

So ``asr_server.py`` leaves endpointing off and this drives the commits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlencode

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

import timing

logger = logging.getLogger("nemo_stt")

# LiveKit runs the agent in a subprocess whose logging config is its own, so
# trace on stdout the way server.py's dbg() does. Off unless NEMO_STT_DEBUG=1.
_DEBUG = os.getenv("NEMO_STT_DEBUG") == "1"


def _dbg(label: str, **fields: object) -> None:
    if _DEBUG:
        print("[NEMO_STT]", label, fields, flush=True)

NUM_CHANNELS = 1

# The shipped models are 16 kHz. Declaring the model's native rate in
# session.update means LiveKit resamples once on our side and the server
# resamples not at all.
DEFAULT_SAMPLE_RATE = 16000

_DELTA = "conversation.item.input_audio_transcription.delta"
_COMPLETED = "conversation.item.input_audio_transcription.completed"


@dataclass
class _STTOptions:
    language: str
    model: str
    sample_rate: int
    automatic_punctuation: bool


class NemotronSTT(stt.STT):
    """Streaming STT against a local NeMo-Speech.cpp server."""

    def __init__(
        self,
        *,
        # Not optional: the server has no turn detection of its own here and
        # LiveKit never flushes the stream, so without a VAD to commit on
        # end-of-speech an utterance would never finalize. See the module
        # docstring.
        vad: vad.VAD,
        base_url: str = "http://127.0.0.1:8003/v1",
        api_key: str = "EMPTY",
        model: str = "nvidia/nemotron-3.5-asr-streaming-0.6b",
        # Full locale, deliberately not normalized: see the module docstring.
        language: str = "ja-JP",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        automatic_punctuation: bool = True,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=True, interim_results=True)
        )
        if vad is None:
            raise ValueError("NemotronSTT requires a VAD to mark the end of a turn")
        self._vad = vad
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._opts = _STTOptions(
            language=language,
            model=model,
            sample_rate=sample_rate,
            automatic_punctuation=automatic_punctuation,
        )
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "nemo-speech.cpp"

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
        if is_given(language):
            self._opts.language = language
        _dbg("stream_requested", language=self._opts.language)
        return SpeechStream(stt=self, conn_options=conn_options)

    def update_options(self, *, language: NotGivenOr[str] = NOT_GIVEN) -> None:
        if is_given(language):
            self._opts.language = language

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        """Batch fallback over ``POST /v1/audio/transcriptions``.

        The session never takes this path - it streams - but ``recognize()``
        stays usable for one-off transcription and scripted checks.
        """
        lang = language if is_given(language) else self._opts.language
        form = aiohttp.FormData()
        form.add_field(
            "file",
            rtc.combine_audio_frames(buffer).to_wav_bytes(),
            filename="audio.wav",
            content_type="audio/wav",
        )
        form.add_field("language", lang)
        form.add_field("model", self._opts.model)
        form.add_field(
            "automatic_punctuation", "true" if self._opts.automatic_punctuation else "false"
        )
        try:
            async with self._ensure_session().post(
                f"{self._base_url}/audio/transcriptions",
                data=form,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30, connect=conn_options.timeout),
            ) as resp:
                if resp.status != 200:
                    raise APIStatusError(await resp.text(), status_code=resp.status)
                payload = await resp.json()
        except asyncio.TimeoutError as e:
            raise APIConnectionError("nemo-speech transcription timed out") from e
        except aiohttp.ClientError as e:
            raise APIConnectionError("nemo-speech transcription failed") from e

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(text=payload.get("text", ""), language=lang)
            ],
        )


class SpeechStream(stt.SpeechStream):
    def __init__(self, *, stt: NemotronSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(
            stt=stt, conn_options=conn_options, sample_rate=stt._opts.sample_rate
        )
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
        params = {"model": self._opts.model}
        url = f"{self._stt._base_url}/realtime?{urlencode(params)}"
        if url.startswith("http"):
            url = url.replace("http", "ws", 1)

        try:
            ws = await asyncio.wait_for(
                self._stt._ensure_session().ws_connect(url, headers=self._stt._headers()),
                self._conn_options.timeout,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError) as e:
            # As an APIConnectionError this is retryable, so the agent survives
            # being started before `uv run asr_server.py` is listening.
            raise APIConnectionError(f"cannot reach nemo-speech at {url}") from e

        # Flat session shape, and it has to go out before the first audio byte:
        # the server rejects any session.update once a stream exists.
        session: dict[str, object] = {
            "sample_rate": self._opts.sample_rate,
            "language": self._opts.language,
            "automatic_punctuation": self._opts.automatic_punctuation,
        }
        await ws.send_str(json.dumps({"type": "session.update", "session": session}))
        _dbg("connected", url=url, session=session)
        return ws

    @utils.log_exceptions(logger=logger)
    async def _run(self) -> None:
        ws = await self._connect()
        vad_stream = self._vad.stream()
        closing = False

        sent_frames = 0

        async def commit() -> None:
            """Finalize the utterance: the server decodes the tail, emits a
            final, and resets its cache-aware state for the next turn."""
            _dbg("commit", frames_sent=sent_frames)
            await ws.send_str(json.dumps({"type": "input_audio_buffer.commit"}))

        @utils.log_exceptions(logger=logger)
        async def send_task() -> None:
            nonlocal closing, sent_frames
            # 50 ms per frame: small enough that a partial is never waiting on
            # the packetizer, large enough to keep the socket quiet.
            bstream = utils.audio.AudioByteStream(
                sample_rate=self._opts.sample_rate,
                num_channels=NUM_CHANNELS,
                samples_per_channel=self._opts.sample_rate // 20,
            )
            try:
                async for data in self._input_ch:
                    frames: list[rtc.AudioFrame] = []
                    if isinstance(data, rtc.AudioFrame):
                        # The VAD sees the frame before the packetizer buffers
                        # it, but its silence threshold is an order of
                        # magnitude longer than one 50 ms frame, so a commit
                        # can never overtake the audio it should cover.
                        vad_stream.push_frame(data)
                        frames.extend(bstream.write(data.data.tobytes()))
                    elif isinstance(data, self._FlushSentinel):
                        frames.extend(bstream.flush())
                    for frame in frames:
                        await ws.send_bytes(frame.data.tobytes())
                        sent_frames += 1
                        if sent_frames in (1, 20, 100, 400):
                            _dbg("audio_flowing", frames_sent=sent_frames)
            except (aiohttp.ClientError, ConnectionError) as e:
                if closing:
                    return
                raise APIConnectionError("nemo-speech realtime connection closed") from e
            finally:
                vad_stream.end_input()

            # Input is done; ask for the tail of the last utterance rather
            # than dropping whatever is still buffered behind the decoder.
            closing = True
            await commit()

        @utils.log_exceptions(logger=logger)
        async def vad_task() -> None:
            try:
                async for ev in vad_stream:
                    if ev.type == vad.VADEventType.START_OF_SPEECH:
                        _dbg("vad_start_of_speech")
                        self._start_speaking()
                    elif ev.type == vad.VADEventType.END_OF_SPEECH:
                        _dbg("vad_end_of_speech")
                        timing.start("eos")
                        self._stop_speaking()
                        await commit()
            except (aiohttp.ClientError, ConnectionError) as e:
                if closing:
                    return
                raise APIConnectionError("nemo-speech realtime connection closed") from e

        @utils.log_exceptions(logger=logger)
        async def recv_task() -> None:
            # Deltas are incremental suffixes: the server only sends a whole
            # replacement transcript when the new partial does not extend the
            # previous one, which a monotonic RNNT decoder does not do. Finals
            # carry the full transcript regardless, so a mis-accumulated
            # interim would self-correct within the utterance either way.
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
                    raise APIStatusError("nemo-speech realtime connection closed unexpectedly")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                event = json.loads(msg.data)
                etype = event.get("type")
                if etype != _DELTA:
                    _dbg("ws_event", type=etype, body=str(event)[:200])

                if etype == _DELTA:
                    partial += event.get("delta") or ""
                    if partial:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                                alternatives=[
                                    stt.SpeechData(
                                        text=partial, language=self._opts.language
                                    )
                                ],
                            )
                        )
                elif etype == _COMPLETED:
                    partial = ""
                    text = (event.get("transcript") or "").strip()
                    _dbg("final", text=text)
                    timing.mark("stt_final", chars=len(text))
                    if text:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[
                                    stt.SpeechData(text=text, language=self._opts.language)
                                ],
                            )
                        )
                elif etype == "input_audio_buffer.committed":
                    # One of these per turn now, so only a commit we sent
                    # because the input ended means the stream is done.
                    if closing:
                        return
                elif etype == "error":
                    message = (event.get("error") or {}).get("message", "unknown error")
                    raise APIStatusError(f"nemo-speech realtime error: {message}")

        tasks = [
            asyncio.create_task(send_task(), name="nemo_stt.send"),
            asyncio.create_task(recv_task(), name="nemo_stt.recv"),
            asyncio.create_task(vad_task(), name="nemo_stt.vad"),
        ]
        try:
            # vad_task only ends when the VAD stream is exhausted, which
            # send_task triggers, so waiting on the first completion (rather
            # than gather) lets a recv failure surface without hanging.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            await utils.aio.cancel_and_wait(*tasks)
            await vad_stream.aclose()
            await ws.close()
