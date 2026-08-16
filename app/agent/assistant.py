"""The Agent the session runs. Instructions in, tools attached, replies suppressed.

The pipeline's own reply is always suppressed; Attention decides whether what
was heard was a turn, and the Conductor asks for the reply.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import Agent
from livekit.agents.llm import StopResponse

from app.agent import prompts
from app.agent.tools import read_result, research_wiki, stop_research


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=prompts.ASSISTANT_INSTRUCTIONS,
            tools=[research_wiki, read_result, stop_research],
        )

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        # The pipeline's own reply is always suppressed. Attention decides
        # whether this was a turn, and the Conductor asks for the reply.
        raise StopResponse()
