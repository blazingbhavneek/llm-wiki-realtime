"""LLM providers: the registry and the facade.

``LLM_PROVIDER`` picks the backend; ``LLM_MODEL`` and ``LLM_BASE_URL`` pick the
model and the host it runs on.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING

from app.llm.base import LLMProvider, LLMSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from livekit.agents import llm as lk_llm

# Import strings, not classes - the same laziness the other provider packages
# use. There is one entry today, but the rule is what keeps an unselected
# provider's dependencies out of the agent process.
REGISTRY: dict[str, str] = {
    "openai_compatible": "app.llm.openai_compatible:OpenAICompatibleLLM",
}

DEFAULT_PROVIDER = "openai_compatible"

__all__ = [
    "REGISTRY",
    "DEFAULT_PROVIDER",
    "LLMProvider",
    "LLMSettings",
    "get_provider",
    "build_llm",
]


def get_provider(name: str | None = None) -> type[LLMProvider]:
    """Resolve a registry name to its provider class, importing it lazily."""
    if name is None:
        name = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)
    try:
        target = REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown LLM provider {name!r}; registered: {known}") from None
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def build_llm(settings: LLMSettings | None = None) -> "lk_llm.LLM":
    """Build the configured LLM."""
    if settings is None:
        provider_cls = get_provider()
        settings = provider_cls.settings_from_env()
    else:
        provider_cls = get_provider(settings.provider)
    return provider_cls().build(settings)
