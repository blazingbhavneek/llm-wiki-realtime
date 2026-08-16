"""The three tools. Talks to the inbox and to Memory.

A tool never starts work itself. It posts one event and returns, so the
Conductor stays the only thing that decides what happens next.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import StopResponse

from app.core.events import ResearchRequested, ResearchStopRequested
from app.core.memory import LevelResult, Memory


@dataclass
class AssistantDeps:
    """Everything the tools are allowed to touch."""

    inbox: asyncio.Queue
    memory: Memory


@function_tool
async def research_wiki(ctx: RunContext[AssistantDeps], question: str) -> str:
    """Search the internal Wiki for a question that needs company-specific facts."""
    ctx.userdata.inbox.put_nowait(ResearchRequested(question))
    # Tool-level stop is deliberate: a prompt alone cannot reliably stop a model
    # from voicing a speculative "I'll look that up" before the answer exists.
    raise StopResponse()


@function_tool
async def read_result(ctx: RunContext[AssistantDeps], handle: str) -> str:
    """Read a retained Wiki result before answering a related follow-up question."""
    level: LevelResult | None = ctx.userdata.memory.find(handle)
    if level is None:
        return "保持している調査結果はまだありません。"
    return f"[観点] {level.objective}\n[質問] {level.question}\n[内容]\n{level.text}"


@function_tool
async def stop_research(ctx: RunContext[AssistantDeps]) -> str:
    """Stop every running Wiki investigation."""
    ctx.userdata.inbox.put_nowait(ResearchStopRequested())
    raise StopResponse()
