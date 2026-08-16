"""Voxtral Mini Realtime on vLLM, through ``livekit.plugins.openai.STT``.

**This endpoint has never been verified.** It exists in the repository's history
as one default string (commit ``5738322``) whose comment claimed it exposed
interim transcripts; it did not. ``openai.STT`` takes ``use_realtime: bool =
False`` and declares ``STTCapabilities(streaming=use_realtime,
interim_results=use_realtime)``, and ``Agent.stt_node`` wraps any STT whose
``capabilities.streaming`` is false in ``stt.StreamAdapter(stt=...,
vad=activity.vad)``. So with ``use_realtime`` left unset - the only
configuration this project ever actually ran - every turn is a from-scratch
batch decode of the whole speech segment, one ``POST /v1/audio/transcriptions``
per VAD segment.

The mode is therefore an explicit switch here, ``STT_USE_REALTIME``, defaulting
to false so that the documented behaviour is the behaviour that was observed.

What is known about the *plugin* (read from the installed version, not from the
server): its realtime path sends OpenAI's nested session shape - 24 kHz PCM
under ``session.audio.input.format.rate``, the language as a base tag under
``...transcription.language`` - and, for any model that is not
``gpt-realtime-whisper``, it also sends ``turn_detection`` (server VAD, 350 ms
silence), i.e. it asks the server to endpoint. A ``vad`` passed in realtime mode
adds a client-side ``input_audio_buffer.commit`` on end-of-speech *on top of*
that; it does not switch the server's endpointing off.

What is unknown, and must be measured rather than assumed by whoever first
points this at a live vLLM server:

* whether the deployed server implements the realtime WebSocket at all, and
  whether it reads that nested session shape. NeMo-Speech.cpp reads a flat one
  and neither side errors when they disagree - see ``app.stt.nemotron``.
* whether its finals are whole utterances or one final per endpointed pause.
  Given that the plugin requests server-side turn detection,
  ``finals_are_utterances`` is declared False for realtime mode: the wiring
  layer must aggregate finals, or this provider must grow its own commit
  discipline the way ``NemotronSTT`` has one. Do not flip that flag without
  evidence from the running server - the lesson the first commit already paid
  for is that a comment asserting "this streams" is not evidence that it
  streams.
"""

from __future__ import annotations

import os

from livekit.agents import stt
from livekit.plugins import openai

from app.stt.base import STTProvider, STTSettings

# The batch/realtime choice, made explicit. Default false = what was actually
# run: LiveKit's own StreamAdapter segments on VAD and calls recognize() once
# per segment.
USE_REALTIME_ENV = "STT_USE_REALTIME"


def _use_realtime() -> bool:
    raw = os.getenv(USE_REALTIME_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


class VoxtralProvider(STTProvider):
    """Mistral's Voxtral Mini Realtime, served by vLLM behind an OpenAI-shaped API."""

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
        """The capability trio for a mode, so the wiring layer can ask.

        Batch is what has been observed. The realtime row is the conservative
        reading of the plugin's own behaviour (it requests server-side turn
        detection), not a measurement against a live Voxtral server.
        """
        if use_realtime:
            return {
                # A vad is still worth passing: it commits the buffer on
                # end-of-speech, which bounds the latency of the final.
                "requires_vad": False,
                "emits_interim": True,
                "finals_are_utterances": False,
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
        if not use_realtime and vad is None:
            # openai.STT does not enforce this itself: without streaming
            # capabilities, Agent.stt_node needs the session's VAD to segment,
            # and a caller that forgot one would only find out at the first turn.
            raise ValueError(
                "voxtral in batch mode is segmented by VAD; pass the session's vad"
            )
        if use_realtime:
            return openai.STT(
                model=settings.model,
                base_url=settings.base_url,
                api_key=settings.api_key,
                # Deliberately passed: the plugin's default is "en", so a
                # Japanese deployment that omits it transcribes as English.
                language=settings.language,
                use_realtime=True,
                vad=vad,
            )
        return openai.STT(
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            language=settings.language,
            use_realtime=False,
        )
