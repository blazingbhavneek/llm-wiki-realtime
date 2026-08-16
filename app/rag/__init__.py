"""Research backend registry and the one thing the rest of the app asks for.

``build_research_pool(inbox)`` gives the Conductor its ``pool``. Today that is
``app.rag.llm_wiki.ResearchPool``, exposed to the registry under the name
``LLMWikiBackend``.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.rag.base import ResearchBackend

# Import strings, not classes: importing ``app.rag`` must not drag ``httpx`` and
# the backend client into a process that only wants the protocols.
#
# `placeholder` is deliberately NOT here. It is not a backend - it is a local
# stand-in *server* that the `llm_wiki` backend talks to when
# LLM_WIKI_BASE_URL points at it, which is exactly how .env is configured
# today. Registering it as a backend would invent behaviour that has never
# existed. See app/rag/README.md.
REGISTRY: dict[str, str] = {
    "llm_wiki": "app.rag.llm_wiki:LLMWikiBackend",
}

DEFAULT_PROVIDER = "llm_wiki"

__all__ = [
    "REGISTRY",
    "build_research_pool",
    "get_backend",
]


def get_backend(name: str | None = None) -> type:
    """Resolve ``RAG_PROVIDER`` (or an explicit name) to a backend class."""
    key = name or os.getenv("RAG_PROVIDER", DEFAULT_PROVIDER)
    target = REGISTRY.get(key)
    if target is None:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown RAG provider {key!r}; registered: {known}")
    module_name, _, class_name = target.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def build_research_pool(inbox: asyncio.Queue) -> "ResearchBackend":
    """The pool of runs for the selected backend, feeding ``inbox``."""
    return get_backend()(inbox)
