"""TTS provider registry and the two things the rest of the app asks for.

``build_tts`` gives the raw engine; ``build_tts_pair`` gives the two sentence
configurations ``app.core.speaker`` installs per speech.
"""

from __future__ import annotations

import importlib
import os

from livekit.agents import tokenize
from livekit.agents import tts as lk_tts

from app.tts.base import TTSProvider, TTSSettings

# Import strings, not classes: importing ``app.tts`` must not drag
# supertonic/soundfile/onnxruntime into the agent process when
# TTS_PROVIDER=qwen3. That laziness is a requirement, not a nicety - the agent
# host is not the machine that hosts the ONNX model.
REGISTRY: dict[str, str] = {
    "supertonic": "app.tts.supertonic:SupertonicTTS",
    "qwen3": "app.tts.qwen3:Qwen3TTS",
    "irodori": "app.tts.irodori:IrodoriTTS",
}

DEFAULT_PROVIDER = "supertonic"

__all__ = [
    "REGISTRY",
    "TTSProvider",
    "TTSSettings",
    "build_tts",
    "build_tts_pair",
    "get_provider",
    "list_voices",
]


def get_provider(name: str | None = None) -> type[TTSProvider]:
    """Resolve ``TTS_PROVIDER`` (or an explicit name) to a provider class."""
    key = name or os.getenv("TTS_PROVIDER", DEFAULT_PROVIDER)
    target = REGISTRY.get(key)
    if target is None:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown TTS provider {key!r}; registered: {known}")
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


async def list_voices(settings: TTSSettings | None = None) -> list[str]:
    """Voice ids the selected provider's endpoint advertises, or ``[]`` if it can't."""
    provider_cls = get_provider(settings.provider if settings else None)
    if not provider_cls.supports_voice_listing:
        return []
    provider = provider_cls()
    return await provider.list_voices(settings or provider.settings_from_env())


def build_tts(settings: TTSSettings | None = None) -> lk_tts.TTS:
    """The raw, unbatched TTS for the selected provider."""
    provider = get_provider(settings.provider if settings else None)()
    return provider.build(settings or provider.settings_from_env())


def build_tts_pair(settings: TTSSettings | None = None) -> dict[str, lk_tts.TTS]:
    """The ``{"reply": ..., "report": ...}`` pair ``app.core.speaker`` installs.

    The keys are ``speaker.REPLY`` and ``speaker.REPORT``.
    """
    provider = get_provider(settings.provider if settings else None)()
    settings = settings or provider.settings_from_env()
    base_tts = provider.build(settings)

    # Two TTS configurations, not one. Conversation wants first audio fast;
    # research prose wants one coherent synthesis with stable prosody, which
    # a short threshold destroys by splitting a reply into many independent
    # non-streaming requests.
    return {
        "reply": lk_tts.StreamAdapter(
            tts=base_tts,
            sentence_tokenizer=_sentence_tokenizer(settings, settings.reply_min_chars),
        ),
        "report": lk_tts.StreamAdapter(
            tts=base_tts,
            sentence_tokenizer=_sentence_tokenizer(settings, settings.report_min_chars),
        ),
    }


def _sentence_tokenizer(settings: TTSSettings, min_sentence_len: int) -> tokenize.SentenceTokenizer:
    return tokenize.basic.SentenceTokenizer(
        language="japanese",
        min_sentence_len=min_sentence_len,
        stream_context_len=settings.stream_context_chars,
    )
