"""Shared fixtures: the fake Speaker/Screen/pool and the Conductor they build."""

from __future__ import annotations

import asyncio

from app.core.attention import Attention
from app.core.conductor import Conductor
from app.core.events import SpeechFinished, SpeechInterrupted
from app.core.memory import Memory
from app.rag.llm_wiki import ResearchPool, ResearchRun


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def level_event(level_id="level-1", text="答えです。", position=1, objective="設定の差分"):
    return {
        "type": "level",
        "level_id": level_id,
        "position": position,
        "objective": objective,
        "text": text,
        "complete": True,
    }


class FakeSpeaker:
    def __init__(self, inbox):
        self.inbox = inbox
        self.ducked = False
        self.started: list[tuple[str, str]] = []
        self.interrupts = 0
        self._current: str | None = None
        self._next_id = 0

    @property
    def busy(self):
        return self._current is not None

    def _start(self, kind, text):
        self._next_id += 1
        speech_id = f"speech-{self._next_id}"
        self._current = speech_id
        self.started.append((kind, text))
        return speech_id

    def start_reply(self, user_text, *, context=""):
        return self._start("reply", user_text)

    def start_report(self, prompt):
        return self._start("report", prompt)

    def start_notice(self, text):
        return self._start("notice", text)

    def interrupt(self):
        self.interrupts += 1
        self._current = None

    def on_speech_ended(self, speech_id):
        if self._current == speech_id:
            self._current = None

    def duck(self):
        self.ducked = True

    def unduck(self):
        self.ducked = False

    # test helpers
    def finish(self, speech_id=None):
        speech_id = speech_id or self._current
        self._current = None
        return SpeechFinished(speech_id)

    def cut(self, spoken, speech_id=None):
        speech_id = speech_id or self._current
        self._current = None
        return SpeechInterrupted(speech_id, spoken)


class FakeScreen:
    def __init__(self):
        self.frames = []
        self.ducked = None
        self.attention = None

    def publish_research(self, frame):
        self.frames.append(frame)

    def set_ducked(self, ducked):
        self.ducked = ducked

    def set_attention(self, state):
        self.attention = state


class FakePool(ResearchPool):
    """A pool that never opens a stream."""

    def __init__(self, inbox):
        super().__init__(inbox)
        self.cancelled = 0
        self.retried = 0

    def start(self, question):
        run = ResearchRun(f"run-{len(self.runs) + 1}", question, self.inbox)
        self.runs[run.run_id] = run
        return run

    def retry(self, run):
        self.retried += 1
        run.attempts += 1

    async def cancel_all(self):
        self.cancelled += 1
        for run in self.runs.values():
            run.state = "cancelled"


def build():
    inbox: asyncio.Queue = asyncio.Queue()
    memory = Memory()
    speaker = FakeSpeaker(inbox)
    pool = FakePool(inbox)
    screen = FakeScreen()
    conductor = Conductor(
        inbox=inbox,
        attention=Attention(),
        speaker=speaker,
        pool=pool,
        memory=memory,
        screen=screen,
    )
    return conductor


async def feed(conductor, *events):
    for event in events:
        await conductor.handle(event)
        await conductor.speak_next()


# ---------------------------------------------------------------------------
# sse transport
# ---------------------------------------------------------------------------


class AsyncLines:
    def __init__(self, lines):
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line
