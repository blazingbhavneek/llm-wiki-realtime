"""Qwen3-TTS behind an OpenAI-compatible vLLM endpoint.

Client only: the model is hosted by vLLM on the shared GPU box, outside this
repo, so there is no ``serve()`` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.tts.base import TTSProvider, TTSSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from livekit.agents import tts as lk_tts


class Qwen3TTS(TTSProvider):
    name = "qwen3"
    hosted_by = "vllm"

    default_model = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    default_voice = "Ono_Anna"
    default_base_url = "http://10.160.144.101:51027/v1"
    default_response_format = "pcm"
    native_sample_rate = 24000
    streams_audio = False
    honors_instructions = True

    # Qwen is served through OpenAI's non-streaming TTS endpoint.  A short
    # threshold turns one reply into many independent synthesis requests,
    # which makes language/prosody reset mid-answer.  This deliberately
    # favors a coherent utterance over first-word latency.
    #
    # The main branch used one threshold, TTS_MIN_SENTENCE_CHARS=180, for both
    # kinds of speech; both defaults below are that same value.
    default_reply_min_chars = 180
    default_report_min_chars = 180

    def build(self, settings: TTSSettings) -> "lk_tts.TTS":
        from livekit.plugins import openai

        return openai.TTS(
            model=settings.model,
            voice=settings.voice,
            base_url=settings.base_url,
            api_key=settings.api_key,
            response_format=settings.response_format,
            # The OpenAI-compatible adapter forwards this as the request's
            # `instructions` field.  Qwen servers that implement it can disable
            # automatic language selection for otherwise Japanese text.
            instructions=settings.instructions,
        )
