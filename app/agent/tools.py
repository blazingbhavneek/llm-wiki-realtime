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


# Read by the model, never spoken. Says what to do next rather than only that
# the lookup missed: a bare "nothing retained" is the point where the model
# falls back on its own knowledge instead of opening a run.
NO_RETAINED_RESULT = (
    "この質問に対応する保持済みの調査結果はありません。"
    "記憶から答えず、research_wiki で新しく調べてください。"
)

# Not a miss. The live plan is already fetching this, so researching it again
# would ask the same question twice and the answer would arrive anyway. The
# turn still gets an answer - "one moment" is an answer, silence is not.
RESEARCH_IN_FLIGHT = (
    "その観点は現在調査中で、まだ結果が届いていません。"
    "research_wiki で調べ直さず、少し待ってほしいと一言だけ伝えてください。"
    "結果が届いたら別の指示が音声にします。"
)


def read_retained(memory: Memory, handle: str) -> str:
    """The body of ``read_result``. A plain function, so it can be tested without
    a session: retained wins, in-flight is the fallback, missing is last."""
    level: LevelResult | None = memory.find(handle)
    if level is not None:
        return f"[観点] {level.objective}\n[質問] {level.question}\n[内容]\n{level.text}"
    pending = memory.await_pending(handle)
    if pending is not None:
        return f"{RESEARCH_IN_FLIGHT}\n[調査中の観点] {pending.objective}"
    return NO_RETAINED_RESULT


@function_tool
async def research_wiki(ctx: RunContext[AssistantDeps], question: str) -> str:
    """Search the internal Wiki. Use this for EVERY question that is not small talk.

    This includes questions you believe you can already answer, and public or
    general technical topics: the Wiki holds general technical documentation
    (APIs, functions, macros, option lists, file formats), not only
    company-internal trivia. Never answer a technical question from your own
    knowledge without calling this first.
    """
    ctx.userdata.inbox.put_nowait(ResearchRequested(question))
    # Tool-level stop is deliberate: a prompt alone cannot reliably stop a model
    # from voicing a speculative "I'll look that up" before the answer exists.
    raise StopResponse()


@function_tool
async def read_result(ctx: RunContext[AssistantDeps], handle: str) -> str:
    """Read a Wiki result already retained from an earlier research run.

    Only for a follow-up about that same subject. If the question names anything
    an earlier run did not cover, call research_wiki instead of this. It also
    answers whether a running investigation is about to cover it.
    """
    return read_retained(ctx.userdata.memory, handle)


@function_tool
async def stop_research(ctx: RunContext[AssistantDeps]) -> str:
    """Stop every running Wiki investigation."""
    ctx.userdata.inbox.put_nowait(ResearchStopRequested())
    raise StopResponse()
