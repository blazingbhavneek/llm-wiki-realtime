"""Speech-to-text providers: the registry and the facade.

``STT_PROVIDER`` picks the engine; everything else about it - model id, endpoint,
sample rate, how it treats the language string - comes from the provider class
rather than from an ``os.getenv`` default buried in the wiring.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any

from app.stt.base import STTProvider, STTSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from livekit.agents import stt as lk_stt

# Import strings, not classes. Importing ``app.stt`` must never drag an
# unselected provider's dependencies into the agent process: nemotron speaks a
# private WebSocket dialect over aiohttp, voxtral needs the openai plugin, and a
# future engine may want torch. Resolution happens in get_provider(), after the
# name is known.
REGISTRY: dict[str, str] = {
    "nemotron": "app.stt.nemotron:NemotronProvider",
    "qwen": "app.stt.qwen:QwenProvider",
    "voxtral": "app.stt.voxtral:VoxtralProvider",
}

DEFAULT_PROVIDER = "nemotron"

__all__ = [
    "REGISTRY",
    "DEFAULT_PROVIDER",
    "STTProvider",
    "STTSettings",
    "get_provider",
    "build_stt",
]


def get_provider(name: str | None = None) -> type[STTProvider]:
    """Resolve a registry name to its provider class, importing it lazily."""
    if name is None:
        name = os.getenv("STT_PROVIDER", DEFAULT_PROVIDER)
    try:
        target = REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown STT provider {name!r}; registered: {known}") from None
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def build_stt(*, vad: Any | None = None, settings: STTSettings | None = None) -> "lk_stt.STT":
    """Build the configured STT.

    Pass the session's VAD: every provider with ``requires_vad`` needs it to
    mark the end of a turn, and will raise ``ValueError`` without it.
    """
    if settings is None:
        provider_cls = get_provider()
        settings = provider_cls.settings_from_env()
    else:
        provider_cls = get_provider(settings.provider)
    return provider_cls().build(settings, vad=vad)
