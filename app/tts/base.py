"""The TTS provider contract: one settings record, one abstract provider.

A provider file owns everything about one engine — the client the agent talks
to and, when we host the model ourselves, the server that hosts it. Env is read
with plain ``os.getenv`` rather than through ``app.config`` on purpose: this
package stays standalone, so a provider can be exercised without the rest of
the app.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import aiohttp

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps livekit off the import path
    from livekit.agents import tts as lk_tts


@dataclass(frozen=True)
class TTSSettings:
    provider: str
    model: str
    voice: str
    base_url: str
    api_key: str
    language: str  # "ja" - supertonic's lang code, qwen's locale hint
    instructions: str  # forwarded as the request's `instructions` field
    response_format: str  # "wav" | "pcm" - provider default unless overridden
    speed: float
    reply_min_chars: int  # the two sentence thresholds Speaker needs
    report_min_chars: int
    stream_context_chars: int


class TTSProvider(abc.ABC):
    """One engine. Subclasses declare their defaults and capability flags.

    The two class attributes that matter most are ``default_response_format``
    and ``native_sample_rate``. They are per-engine knowledge that used to sit
    in the wiring as a hardcoded literal, and they are exactly the kind of thing
    that fails as garbled audio rather than as an error: ask Supertonic for
    ``"pcm"`` and LiveKit reads its native 44.1 kHz samples as raw 24 kHz. They
    are declared here, per engine, instead of at the call site.
    """

    name: ClassVar[str]  # registry key, e.g. "supertonic"
    hosted_by: ClassVar[str]  # "self" | "vllm" | "remote"

    default_model: ClassVar[str]
    default_voice: ClassVar[str]
    default_base_url: ClassVar[str]
    default_response_format: ClassVar[str]  # supertonic "wav" @44.1k, qwen3 "pcm" @24k
    native_sample_rate: ClassVar[int]
    streams_audio: ClassVar[bool]  # False for both -> StreamAdapter + long sentences
    honors_instructions: ClassVar[bool]  # supertonic: False, it reads TTS_LANG server-side
    supports_voice_listing: ClassVar[bool]  # True if GET {base_url}/audio/voices is served
    default_reply_min_chars: ClassVar[int]
    default_report_min_chars: ClassVar[int]

    @classmethod
    def settings_from_env(cls) -> TTSSettings:
        """Env overrides on top of this provider's defaults.

        Every ``TTS_*`` variable keeps the name and the value it has today; what
        changes is where the fallback comes from — ``cls.default_*`` rather than
        a literal at the call site, so switching engines switches the defaults.
        """
        return TTSSettings(
            provider=cls.name,
            model=os.getenv("TTS_MODEL", cls.default_model),
            voice=os.getenv("TTS_VOICE", cls.default_voice),
            base_url=os.getenv("TTS_BASE_URL", cls.default_base_url),
            api_key=os.getenv("TTS_API_KEY", "EMPTY"),
            language=os.getenv("TTS_LANG", "ja"),
            instructions=os.getenv(
                "TTS_INSTRUCTIONS",
                "日本語のみで発話してください。英語、ローマ字、他の言語へ切り替えないでください。",
            ),
            response_format=os.getenv("TTS_RESPONSE_FORMAT", cls.default_response_format),
            speed=float(os.getenv("TTS_SPEED", "1.05")),
            reply_min_chars=int(
                os.getenv("TTS_REPLY_MIN_SENTENCE_CHARS", str(cls.default_reply_min_chars))
            ),
            report_min_chars=int(
                os.getenv("TTS_REPORT_MIN_SENTENCE_CHARS", str(cls.default_report_min_chars))
            ),
            stream_context_chars=int(os.getenv("TTS_STREAM_CONTEXT_CHARS", "240")),
        )

    @abc.abstractmethod
    def build(self, settings: TTSSettings) -> "lk_tts.TTS":
        """The raw TTS. Sentence batching is applied by the module facade."""

    def serve(self, settings: TTSSettings) -> None:
        """Host this model locally. Only for hosted_by == 'self'."""
        raise NotImplementedError

    async def list_voices(self, settings: TTSSettings) -> list[str]:
        """Voice ids the endpoint advertises. Only called when ``supports_voice_listing``.

        This default hits the OpenAI-compatible ``GET {base_url}/audio/voices``
        convention every vLLM-hosted provider in this registry follows
        (``{"voices": [...], "uploaded_voices": [...]}}``). A provider whose
        endpoint doesn't serve that route sets ``supports_voice_listing = False``
        instead of overriding this.
        """
        headers = {"User-Agent": "LiveKit Agents"}
        if settings.api_key and settings.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {settings.api_key}"
        url = f"{settings.base_url.rstrip('/')}/audio/voices"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return sorted(data.get("voices", []))
