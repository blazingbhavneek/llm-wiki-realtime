"""Producers: translate, never decide.

Every decision lives in ``Conductor.handle``. The LiveKit callbacks below only
translate session and room events into inbox events - nothing here reacts to
anything.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from livekit import agents, rtc
from livekit.agents import AgentSession, room_io
from livekit.agents.voice.events import CloseReason

from app.core.events import (
    IdleTick,
    ListenButtonChanged,
    UserSaidText,
    UserStartedSpeaking,
    UserStoppedSpeaking,
)
from app.core.screen import Screen
from app.log import dbg


async def idle_ticker(inbox: asyncio.Queue) -> None:
    while True:
        await asyncio.sleep(1.0)
        inbox.put_nowait(IdleTick())


def attach(
    session: AgentSession,
    room: rtc.Room,
    ctx: agents.JobContext,
    inbox: asyncio.Queue,
    screen: Screen,
) -> Callable[[AgentSession, room_io.TextInputEvent], None]:
    """Register every session and room callback.

    Returns the text-input callback, which is not registered by decorator but
    handed to ``room_io.TextInputOptions`` when the session starts.
    """

    @session.on("user_state_changed")
    def _on_user_state(event: Any) -> None:
        if event.new_state == "speaking":
            inbox.put_nowait(UserStartedSpeaking())
        elif event.old_state == "speaking":
            inbox.put_nowait(UserStoppedSpeaking())

    @session.on("user_input_transcribed")
    def _on_transcript(event: Any) -> None:
        # Read straight off the STT rather than off turn completion: LiveKit
        # drops turn completion entirely while an uninterruptible speech plays,
        # which is exactly when a barge-in has to be heard.
        if not event.is_final:
            return
        text = (event.transcript or "").strip()
        if text:
            inbox.put_nowait(UserSaidText(text, from_text_input=False))

    def _on_text_input(_session: AgentSession, event: room_io.TextInputEvent) -> None:
        text = event.text.strip()
        if text:
            inbox.put_nowait(UserSaidText(text, from_text_input=True))

    # ---- failures: say so, out loud, to the log and to the browser ------
    # Without these a dying session is invisible. The orb is drawn from the
    # browser's own microphone state, so a session that has closed still looks
    # like it is listening: red, labelled "聞いています", and deaf. Every
    # component failure now leaves a line in the log naming which one it was.

    @session.on("error")
    def _on_session_error(event: Any) -> None:
        error = getattr(event, "error", None)
        recoverable = bool(getattr(error, "recoverable", False))
        source = getattr(event, "source", None)
        dbg(
            "SESSION_ERROR",
            component=getattr(error, "type", type(error).__name__),
            provider=getattr(source, "provider", type(source).__name__),
            model=getattr(source, "model", ""),
            recoverable=recoverable,
            error=repr(getattr(error, "error", error)),
        )
        if not recoverable:
            screen.set_agent_status(
                "degraded", str(getattr(error, "type", "") or "unknown")
            )

    @session.on("close")
    def _on_session_close(event: Any) -> None:
        reason = getattr(event, "reason", None)
        reason_text = getattr(reason, "value", str(reason))
        dbg("SESSION_CLOSED", reason=reason_text, error=repr(getattr(event, "error", None)))
        if reason is not CloseReason.ERROR:
            return  # a normal shutdown; the job is already on its way out
        # The session is done but the agent is still a participant, so the
        # browser would keep talking to a corpse. Tell it, then leave the room
        # so it also sees the participant go and can start a fresh dispatch.
        screen.set_agent_status("closed", reason_text)
        ctx.shutdown(reason="agent session closed on an unrecoverable error")

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != "attention":
            return
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except Exception:
            return
        if payload.get("type") == "listen":
            inbox.put_nowait(ListenButtonChanged(bool(payload.get("held"))))

    return _on_text_input
