"""The STT provider contract.

One external speech-recognition dependency = one module in this package, and
every one of them subclasses :class:`STTProvider`. The class attributes are not
documentation: ``requires_vad``, ``emits_interim``, ``finals_are_utterances``
and ``language_is_locale`` are the four facts about an ASR engine that decide
whether the rest of the pipeline behaves, and each of them has already cost a
debugging session when it was left implicit in the wiring.

This module imports nothing from the rest of the app on purpose (not even
``app.config``): a provider package has to stand alone so that reading its
settings never depends on the process having booted the agent.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps import cost off the hot path
    from livekit.agents import stt as lk_stt


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class STTSettings:
    """Everything a provider needs to build its client, read from the env once."""

    provider: str
    model: str
    base_url: str
    api_key: str
    language: str  # nemotron needs the full locale "ja-JP"
    sample_rate: int
    automatic_punctuation: bool


class STTProvider(abc.ABC):
    """A swappable speech-to-text engine.

    Subclasses declare their defaults and their capabilities, and implement
    :meth:`build`. When we host the model ourselves they also implement
    :meth:`serve`, so that everything about one engine - the client the agent
    talks to and the server that hosts it - lives in one file.
    """

    name: ClassVar[str]
    hosted_by: ClassVar[str]  # "self" | "vllm" | "remote"

    default_model: ClassVar[str]
    default_base_url: ClassVar[str]
    default_language: ClassVar[str]
    native_sample_rate: ClassVar[int]

    # The engine has no turn boundary of its own, so the caller must hand
    # ``build()`` the session's VAD and the provider commits on end-of-speech.
    requires_vad: ClassVar[bool]

    # Partial transcripts arrive while the user is still speaking.
    emits_interim: ClassVar[bool]

    # Every FINAL_TRANSCRIPT is one complete user utterance.
    #
    # This is the assumption the whole downstream design rests on: the
    # transcript handler bypasses LiveKit's turn completion and treats each
    # final as a finished question, which is also why the local ASR server is
    # started with ``--endpointing`` off. An engine that finalizes on its own
    # server-side silence detection instead emits a final *per pause*, so one
    # spoken question arrives as three or four separate turns - a failure that
    # looks like a bad ASR model rather than a bad integration. Declaring the
    # flag lets the wiring layer refuse such a provider, or aggregate its
    # finals, instead of every new provider rediscovering this.
    finals_are_utterances: ClassVar[bool]

    # The language string is matched by exact locale, not by base tag:
    # nemotron's "ja" is silently auto-detect while "ja-JP" is Japanese.
    language_is_locale: ClassVar[bool]

    @classmethod
    def settings_from_env(cls) -> STTSettings:
        """Read ``STT_*`` from the environment, falling back to this provider's defaults."""
        return STTSettings(
            provider=cls.name,
            model=os.getenv("STT_MODEL", cls.default_model),
            base_url=os.getenv("STT_BASE_URL", cls.default_base_url),
            api_key=os.getenv("STT_API_KEY", "EMPTY"),
            language=os.getenv("STT_LANGUAGE", cls.default_language),
            sample_rate=int(os.getenv("STT_SAMPLE_RATE", str(cls.native_sample_rate))),
            automatic_punctuation=_env_bool("STT_AUTOMATIC_PUNCTUATION", True),
        )

    @abc.abstractmethod
    def build(self, settings: STTSettings, *, vad: Any | None = None) -> "lk_stt.STT":
        """Return the LiveKit STT the agent session will use.

        When ``requires_vad`` is True the caller MUST pass ``vad`` - the same
        VAD instance the session uses, so that one speech segment is one final
        is one user turn - and ``build`` raises :class:`ValueError` if it is
        missing. Providers that do not need it ignore the argument.
        """

    def serve(self, settings: STTSettings) -> None:
        """Host this model locally. Only for ``hosted_by == "self"``."""
        raise NotImplementedError(f"{self.name} is hosted by {self.hosted_by}, not by us")
