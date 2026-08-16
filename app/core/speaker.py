"""The single speech slot. The only file that touches the session or the TTS.

Nothing here decides what to say. ``start_*`` launches a detached task and
returns a speech id immediately, so the Conductor never blocks on playout and a
barge-in is always processed within one event.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.core.events import SpeechFinished, SpeechInterrupted

from app import timing

REPLY = "reply"
REPORT = "report"
NOTICE = "notice"
NONE = "none"


@dataclass
class Speech:
    speech_id: str
    kind: str
    handle: Any


class Speaker:
    def __init__(self, session: Any, agent: Any, tts_pair: dict[str, Any], inbox: asyncio.Queue) -> None:
        self.session = session
        self.agent = agent
        self.inbox = inbox
        self.ducked = False
        self._current: Speech | None = None
        self._tasks: set[asyncio.Task] = set()
        self._tts_kind = ""

        # The two configurations are built by app.tts.build_tts_pair; Speaker
        # only picks between them.
        self._tts = dict(tts_pair)

    # -- state -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._current is not None

    @property
    def current_kind(self) -> str:
        return self._current.kind if self._current else NONE

    @property
    def default_tts(self) -> Any:
        return self._tts[REPLY]

    # -- starting speech ---------------------------------------------------

    def start_reply(self, user_text: str, *, context: str = "") -> str:
        """A user turn. The LLM answers and may call tools."""
        self._use(REPLY)
        timing.mark("llm_request", chars=len(user_text))
        extra = {"instructions": context} if context else {}
        handle = self.session.generate_reply(
            user_input=user_text,
            allow_interruptions=False,
            **extra,
        )
        return self._track(handle, REPLY)

    def start_report(self, prompt: str) -> str:
        """Narrate research. One LLM pass over evidence, no tools."""
        self._use(REPORT)
        handle = self.session.generate_reply(
            instructions=prompt,
            tools=[],
            allow_interruptions=False,
        )
        return self._track(handle, REPORT)

    def start_notice(self, text: str) -> str:
        """A fixed sentence. Spoken verbatim so the model cannot embellish it."""
        self._use(REPLY)
        handle = self.session.say(text, allow_interruptions=False, add_to_chat_ctx=True)
        return self._track(handle, NOTICE)

    # -- taking speech away ------------------------------------------------

    def interrupt(self) -> None:
        """Free the slot now. The cut speech still reports what it managed to say."""
        speech = self._current
        self._current = None
        if speech is None:
            return
        try:
            speech.handle.interrupt(force=True)
        except Exception:
            pass

    def on_speech_ended(self, speech_id: str) -> None:
        """Clear the slot, unless a barge-in already handed it to someone else."""
        if self._current is not None and self._current.speech_id == speech_id:
            self._current = None

    def duck(self) -> None:
        self.ducked = True

    def unduck(self) -> None:
        self.ducked = False

    async def aclose(self) -> None:
        self.interrupt()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- internals ---------------------------------------------------------

    def _use(self, kind: str) -> None:
        if self._tts_kind == kind:
            return
        self._tts_kind = kind
        try:
            # tts_node reads the agent's TTS per synthesis, and the slot is free
            # when this runs, so no in-flight playout can see the swap.
            self.agent.update_options(tts=self._tts[kind])
        except Exception:
            pass

    def _track(self, handle: Any, kind: str) -> str:
        speech_id = str(getattr(handle, "id", "") or id(handle))
        speech = Speech(speech_id=speech_id, kind=kind, handle=handle)
        self._current = speech
        task = asyncio.create_task(self._watch(speech), name=f"speech:{speech_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return speech_id

    async def _watch(self, speech: Speech) -> None:
        try:
            await speech.handle.wait_for_playout()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        spoken = _spoken_text(speech.handle)
        if getattr(speech.handle, "interrupted", False):
            self.inbox.put_nowait(SpeechInterrupted(speech.speech_id, spoken))
        else:
            self.inbox.put_nowait(SpeechFinished(speech.speech_id, spoken))


def _spoken_text(handle: Any) -> str:
    """What actually reached the speaker.

    On a barge-in LiveKit stores the assistant message truncated to the audio
    that was forwarded, so this is the real spoken prefix, not the full
    generation.
    """
    parts: list[str] = []
    for item in getattr(handle, "chat_items", ()) or ():
        if getattr(item, "role", "") != "assistant":
            continue
        content = getattr(item, "text_content", "")
        if callable(content):
            content = content()
        if content:
            parts.append(str(content))
    return "".join(parts).strip()
