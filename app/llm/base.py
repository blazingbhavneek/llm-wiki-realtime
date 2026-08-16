"""The LLM provider contract.

Deliberately thinner than the STT and TTS contracts: an LLM's failure modes are
loud (a bad model id is a 404), not silent the way a wrong sample rate or a
truncated locale is. The one capability worth declaring is ``supports_tools`` -
the assistant is useless without function calling.

Imports nothing from the rest of the app (not even ``app.config``) so the
package stands alone.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from livekit.agents import llm as lk_llm


@dataclass(frozen=True)
class LLMSettings:
    """Everything a provider needs to build its client, read from the env once."""

    provider: str
    model: str
    base_url: str
    api_key: str


class LLMProvider(abc.ABC):
    """A swappable chat-completions backend."""

    name: ClassVar[str]

    default_model: ClassVar[str]
    default_base_url: ClassVar[str]

    # The assistant declares three function tools (research_wiki, read_result,
    # stop_research). A provider without tool calling cannot drive it.
    supports_tools: ClassVar[bool]

    @classmethod
    def settings_from_env(cls) -> LLMSettings:
        """Read ``LLM_*`` from the environment, falling back to this provider's defaults."""
        return LLMSettings(
            provider=cls.name,
            model=os.getenv("LLM_MODEL", cls.default_model),
            base_url=os.getenv("LLM_BASE_URL", cls.default_base_url),
            api_key=os.getenv("LLM_API_KEY", "EMPTY"),
        )

    @abc.abstractmethod
    def build(self, settings: LLMSettings) -> "lk_llm.LLM":
        """Return the LiveKit LLM the agent session will use."""
