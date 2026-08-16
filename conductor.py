"""The giga thread. Every decision in the program is in this file.

One consumer of one queue, so there are no locks and no races to reason about.
Anything slow is a detached task that reports back through the inbox; the loop
itself never awaits playout, generation, or the network.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass
from typing import Any

import agent as prompts
import timing
from attention import OPEN, Attention
from events import (
    BACKGROUND,
    FOREGROUND,
    IdleTick,
    LevelReady,
    ListenButtonChanged,
    PlanReady,
    ResearchFailed,
    ResearchFinished,
    ResearchProgress,
    ResearchRequested,
    ResearchStopRequested,
    SpeechFinished,
    SpeechInterrupted,
    UserSaidText,
    UserStartedSpeaking,
    UserStoppedSpeaking,
)
from memory import NEW, PARTIAL, LevelResult, Memory
from research import ResearchPool
from screen import Screen
from speaker import Speaker

# How many reports may pass before a background finding stops being worth
# saying. Deliberately dumb: a slightly over-talkative coworker is a much
# smaller problem than one that silently drops findings.
BACKGROUND_STALE_AFTER_REPORTS = int(os.getenv("BACKGROUND_STALE_AFTER_REPORTS", "3"))


@dataclass
class Pending:
    """Speech waiting for the slot. `notice` is spoken verbatim, `prompt` is an
    LLM pass over evidence."""

    kind: str  # "notice" | "prompt"
    text: str


class Conductor:
    def __init__(
        self,
        *,
        inbox: asyncio.Queue,
        attention: Attention,
        speaker: Speaker,
        pool: ResearchPool,
        memory: Memory,
        screen: Screen,
    ) -> None:
        self.inbox = inbox
        self.attention = attention
        self.speaker = speaker
        self.pool = pool
        self.memory = memory
        self.screen = screen

        self.user_is_speaking = False
        self.pending: deque[Pending] = deque()
        self.speaking_level: dict[str, LevelResult] = {}
        self.stopped_runs: set[str] = set()
        self._published_attention = ""

    # -- the loop ----------------------------------------------------------

    async def run(self) -> None:
        while True:
            event = await self.inbox.get()
            try:
                await self.handle(event)
                await self.speak_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad event must not kill the program
                print(f"[CONDUCTOR ERROR] {type(event).__name__}: {exc!r}", flush=True)

    # -- the decision table ------------------------------------------------

    async def handle(self, event: Any) -> None:
        if isinstance(event, ListenButtonChanged):
            self.attention.set_button_held(event.held)
            if event.held:
                self.attention.open("button")
            else:
                # An explicit press off means stop listening, even mid-run.
                # Research already started keeps going and still reports.
                self.attention.close()
            self.publish_attention()
            return

        if isinstance(event, UserStartedSpeaking):
            self.user_is_speaking = True
            if self.attention.state == OPEN:
                # Being addressed: the user always wins the floor immediately.
                self.interrupt_current()
            elif self.speaker.busy:
                # Someone is talking near the mic. Not necessarily to us.
                self.speaker.duck()
                self.screen.set_ducked(True)
            return

        if isinstance(event, UserStoppedSpeaking):
            self.user_is_speaking = False
            return

        if isinstance(event, UserSaidText):
            await self.handle_user_text(event)
            return

        if isinstance(event, ResearchRequested):
            self.start_research(event.question)
            return

        if isinstance(event, ResearchStopRequested):
            await self.stop_everything()
            return

        if isinstance(event, ResearchProgress):
            self.screen.publish_research(event.frame)
            return

        if isinstance(event, PlanReady):
            run = self.pool.get(event.run_id)
            if run is not None and run.focus == FOREGROUND and event.planned_levels:
                self.pending.append(
                    Pending("prompt", prompts.plan_preview_instructions(run.question, event.planned_levels))
                )
            return

        if isinstance(event, LevelReady):
            run = self.pool.get(event.run_id)
            if run is not None:
                self.memory.remember(run, event.level)
            return

        if isinstance(event, ResearchFailed):
            run = self.pool.get(event.run_id)
            if run is not None and self.pool.can_retry(run):
                self.pool.retry(run)
                return
            self.pending.append(Pending("notice", prompts.NOTICE_RESEARCH_FAILED))
            return

        if isinstance(event, ResearchFinished):
            # Nothing to do: levels were remembered as they arrived, and the
            # idle timer unpins by itself once no run is active.
            return

        if isinstance(event, SpeechFinished):
            self.speaker.on_speech_ended(event.speech_id)
            level = self.speaking_level.pop(event.speech_id, None)
            if level is not None:
                self.memory.mark_reported(level)
            return

        if isinstance(event, SpeechInterrupted):
            self.speaker.on_speech_ended(event.speech_id)
            level = self.speaking_level.pop(event.speech_id, None)
            if level is not None:
                self.memory.mark_partial(level, event.spoken_text)
            return

        if isinstance(event, IdleTick):
            if self.speaker.ducked and not self.user_is_speaking:
                self.speaker.unduck()
                self.screen.set_ducked(False)
            self.publish_attention()
            return

    # -- an accepted user turn ---------------------------------------------

    async def handle_user_text(self, event: UserSaidText) -> None:
        turn = self.attention.accept(event.text, from_text_input=event.from_text_input)

        if self.speaker.ducked:
            self.speaker.unduck()
            self.screen.set_ducked(False)
        self.user_is_speaking = False

        if not turn.accepted:
            return  # not for us; the transcript is discarded

        timing.mark("accepted", command=turn.command)

        # Speech accepted mid-report is a real barge-in.
        if self.speaker.busy:
            self.interrupt_current()

        if turn.command == "stop":
            await self.stop_everything()
            return

        if turn.command == "close":
            self.pending.append(Pending("notice", prompts.NOTICE_CLOSED))
            return

        if turn.command == "repeat":
            level = self.memory.last_reported()
            if level is None:
                self.pending.append(Pending("notice", prompts.NOTICE_NOTHING_TO_REPEAT))
            else:
                level.spoken_text = ""
                self.memory.mark_new(level)
            return

        if turn.command == "continue":
            level = self.memory.last_partial()
            if level is None:
                self.pending.append(Pending("notice", prompts.NOTICE_NOTHING_TO_CONTINUE))
            return

        if not turn.text:
            self.pending.append(Pending("notice", prompts.NOTICE_ACKNOWLEDGED))
            return

        self.speaker.start_reply(turn.text, context=self.memory.summary_for_llm())

    def start_research(self, question: str) -> None:
        current = self.pool.foreground_run()
        if current is not None:
            self.pool.move_to_background(current)
            current.superseded_at_report = self.memory.reports_delivered
            self.memory.set_focus(current.run_id, BACKGROUND)
        run = self.pool.start(question)
        self.stopped_runs.discard(run.run_id)

    async def stop_everything(self) -> None:
        for run in self.pool.runs.values():
            self.stopped_runs.add(run.run_id)
        await self.pool.cancel_all()
        for level in self.memory.levels:
            if level.state in (NEW, PARTIAL):
                self.memory.mark_silent(level)
        self.pending.clear()
        self.pending.append(Pending("notice", prompts.NOTICE_STOPPED))

    def interrupt_current(self) -> None:
        self.speaker.interrupt()
        if self.speaker.ducked:
            self.speaker.unduck()
            self.screen.set_ducked(False)

    # -- the priority ladder -----------------------------------------------

    async def speak_next(self) -> None:
        """Runs after every event, so any change in the world re-decides what
        should be coming out of the speaker. First match wins."""
        if self.speaker.busy:
            return  # 1. never preempt ourselves
        if self.user_is_speaking:
            return  # 2. never talk over the user

        # 3. short, time-sensitive speech: plan previews, apologies, confirmations
        if self.pending:
            item = self.pending.popleft()
            if item.kind == "notice":
                self.speaker.start_notice(item.text)
            else:
                self.speaker.start_report(item.text)
            return

        # 4. finish the sentence you cut me off in
        level = self.memory.next_partial(FOREGROUND)
        if level is not None:
            return self.report(level, resume=True)

        # 5. the question you just asked
        level = self.memory.next_new(FOREGROUND)
        if level is not None:
            return self.report(level)

        # 6. the sentence you cut me off in, about the OLD question
        level = self.memory.next_partial(BACKGROUND)
        if level is not None:
            return self.report(level, resume=True, attribute=True)

        # 7. new findings on the old question, only if still worth saying
        while (level := self.memory.next_new(BACKGROUND)) is not None:
            if self.is_still_relevant(level):
                return self.report(level, attribute=True)
            self.memory.mark_silent(level)

    def report(self, level: LevelResult, *, resume: bool = False, attribute: bool = False) -> None:
        step, step_count, next_objective = self.position_of(level)
        prompt = prompts.report_instructions(
            level,
            step=step,
            step_count=step_count,
            next_objective=next_objective,
            resume=resume,
            attribute=attribute,
        )
        self.memory.mark_reporting(level)
        speech_id = self.speaker.start_report(prompt)
        self.speaking_level[speech_id] = level

    def position_of(self, level: LevelResult) -> tuple[int, int, str]:
        """Where this level sits in its run's plan, for the 'and next we look at' line."""
        run = self.pool.get(level.run_id)
        planned = sorted(
            (run.planned_levels if run else []),
            key=lambda item: int(item.get("position") or 0),
        )
        index = next(
            (
                position
                for position, item in enumerate(planned)
                if str(item.get("id") or item.get("level_id") or "") == level.level_id
            ),
            max(level.position - 1, 0),
        )
        step_count = max(len(planned), index + 1)
        next_objective = ""
        if index + 1 < len(planned):
            next_objective = str(planned[index + 1].get("objective") or "")
        return index + 1, step_count, next_objective

    def is_still_relevant(self, level: LevelResult) -> bool:
        if level.run_id in self.stopped_runs:
            return False
        run = self.pool.get(level.run_id)
        if run is None or run.superseded_at_report < 0:
            return True
        elapsed = self.memory.reports_delivered - run.superseded_at_report
        return elapsed <= BACKGROUND_STALE_AFTER_REPORTS

    # -- bootstrap ---------------------------------------------------------

    def publish_attention(self) -> None:
        """Only on change: the browser does not need a heartbeat."""
        if self.attention.state != self._published_attention:
            self._published_attention = self.attention.state
            self.screen.set_attention(self.attention.state)

    def queue_notice(self, text: str) -> None:
        self.pending.append(Pending("notice", text))


__all__ = ["Conductor", "Pending"]
