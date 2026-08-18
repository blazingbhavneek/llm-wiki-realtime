"""Any OpenAI-shaped ``/v1/chat/completions`` endpoint.

One provider covers llama-server, vLLM and SGLang - they all speak the same
wire format, so switching between them (or between models on the same host) is
an ``LLM_MODEL`` + ``LLM_BASE_URL`` edit rather than a code change. The package
exists so that a backend which is *not* OpenAI-shaped can be added without
reopening the wiring layer.

``supports_tools`` is True: the assistant's three function tools
(``research_wiki``, ``read_result``, ``stop_research``) are dispatched through
this endpoint's tool-calling support, and a server built without it will look
like a model that simply never researches anything.
"""

from __future__ import annotations

from livekit.agents import llm
from livekit.plugins import openai

from app.llm.base import LLMProvider, LLMSettings


class OpenAICompatibleLLM(LLMProvider):
    """The default: Gemma on a local llama-server, or anything else OpenAI-shaped."""

    name = "openai_compatible"

    default_model = "gemma-4-31B"
    default_base_url = "http://10.160.144.101:51028/v1"

    supports_tools = True

    def build(self, settings: LLMSettings) -> llm.LLM:
        return openai.LLM(
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
        )
