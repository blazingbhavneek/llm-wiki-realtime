"""One LiveKit job: build the providers, the session, and the Conductor.

This module wires the parts together and then gets out of the way: every
decision lives in ``Conductor.handle``, and the callbacks that feed it live in
``app.runtime.producers``.
"""

from __future__ import annotations

import asyncio
import os

from livekit import agents
from livekit.agents import AgentSession, room_io
from livekit.agents.voice.agent_session import SessionConnectOptions

from app.agent import prompts
from app.agent.assistant import Assistant
from app.agent.tools import AssistantDeps
from app.config import Settings
from app.core.attention import Attention
from app.core.conductor import Conductor
from app.core.events import IdleTick
from app.core.memory import Memory
from app.core.screen import Screen
from app.core.speaker import Speaker
from app.llm import build_llm
from app.log import dbg
from app.rag import build_research_pool
from app.rag.llm_wiki import stream_url
from app.runtime.producers import attach, idle_ticker
from app.stt import build_stt
from app.tts import build_tts_pair


async def entrypoint(ctx: agents.JobContext) -> None:
    settings = Settings.from_env()
    await ctx.connect()
    dbg(
        "ENTRYPOINT_CONNECTED",
        room=ctx.room.name,
        LLM_MODEL=os.getenv("LLM_MODEL"),
        STT_MODEL=os.getenv("STT_MODEL"),
        TTS_MODEL=os.getenv("TTS_MODEL"),
        RAG_STREAM_URL=stream_url(),
    )

    inbox: asyncio.Queue = asyncio.Queue()
    memory = Memory()

    stt = build_stt(vad=ctx.proc.userdata["vad"])
    llm = build_llm()
    tts_pair = build_tts_pair()

    assistant = Assistant()
    session = AgentSession[AssistantDeps](
        userdata=AssistantDeps(inbox=inbox, memory=memory),
        # LiveKit closes the whole session after this many *consecutive*
        # unrecoverable errors from STT, LLM or TTS, and the STT counter only
        # resets on a transcript. Its default of 3 is far too tight for a
        # hand-started local stack: an ASR server that comes up a minute after
        # the agent burns all three before a single transcript can reset them,
        # and the session dies before the user has said anything. The pipeline
        # rebuilds a failed stream by itself, so a generous budget only means
        # "keep trying while a service is coming back".
        conn_options=SessionConnectOptions(
            max_unrecoverable_errors=settings.livekit.max_unrecoverable_errors,
        ),
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=llm,
        # Speaker installs the right TTS per speech; this is only the fallback
        # the session needs at construction time.
        tts=tts_pair["reply"],
        turn_handling={
            # Turn completion is unused - the Conductor reads STT finals - so
            # keep detection on plain VAD instead of loading a turn model.
            "turn_detection": "vad",
            # Every speech we create is uninterruptible so that the Conductor,
            # not the pipeline, decides what a nearby voice means. LiveKit
            # otherwise substitutes silence into the STT while such a speech
            # plays, which would make a real barge-in impossible to hear.
            "interruption": {"discard_audio_if_uninterruptible": False},
        },
    )

    speaker = Speaker(session, assistant, tts_pair, inbox)
    pool = build_research_pool(inbox)
    screen = Screen(ctx.room)
    conductor = Conductor(
        inbox=inbox,
        attention=Attention(),
        speaker=speaker,
        pool=pool,
        memory=memory,
        screen=screen,
    )

    # ---- producers: translate, never decide -----------------------------

    on_text_input = attach(session, ctx.room, ctx, inbox, screen)

    await session.start(
        room=ctx.room,
        agent=assistant,
        room_options=room_io.RoomOptions(
            text_input=room_io.TextInputOptions(text_input_cb=on_text_input),
            audio_input=True,
            audio_output=True,
            text_output=True,
        ),
    )

    # The TTS is owned by Speaker, which installs the right one per speech.
    assistant.update_options(tts=speaker.default_tts)

    conductor_task = asyncio.create_task(conductor.run(), name="conductor")
    ticker_task = asyncio.create_task(idle_ticker(inbox), name="idle-ticker")

    conductor.queue_notice(prompts.GREETING)
    inbox.put_nowait(IdleTick())  # kick the ladder so the greeting plays now

    async def shutdown() -> None:
        ticker_task.cancel()
        conductor_task.cancel()
        await pool.cancel_all()
        await speaker.aclose()
        await asyncio.gather(ticker_task, conductor_task, return_exceptions=True)

    ctx.add_shutdown_callback(shutdown)
    dbg("SESSION_STARTED", room=ctx.room.name)
