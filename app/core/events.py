"""The vocabulary. Every moving part speaks only these.

No behavior lives here. If something cannot be said with one of these
dataclasses, it does not happen: producers push them into the single inbox and
``Conductor.handle`` is the only reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Focus is orthogonal to a run's lifecycle: a background run keeps streaming and
# keeps being remembered, it just loses the speech floor to the newest question.
FOREGROUND = "foreground"
BACKGROUND = "background"


@dataclass(frozen=True)
class UserStartedSpeaking:
    """VAD detected voice. Says nothing about whether we are being addressed."""


@dataclass(frozen=True)
class UserStoppedSpeaking:
    """VAD went quiet. Needed because chatter that produces no transcript at all
    must still be able to release the duck."""


@dataclass(frozen=True)
class UserSaidText:
    text: str
    from_text_input: bool = False  # typed, so addressing is implicit


@dataclass(frozen=True)
class ListenButtonChanged:
    held: bool  # push-to-talk from the browser


@dataclass(frozen=True)
class ResearchRequested:
    question: str  # posted by the research_wiki tool


@dataclass(frozen=True)
class ResearchStopRequested:
    """Posted by the stop_research tool. The 「止めて」 regex path does not use it."""


@dataclass(frozen=True)
class PlanReady:
    run_id: str
    planned_levels: list[dict[str, Any]]


@dataclass(frozen=True)
class LevelReady:
    run_id: str
    level: dict[str, Any]  # raw SSE level event


@dataclass(frozen=True)
class ResearchProgress:
    """A raw SSE frame with no decision attached to it.

    The plan gives the Conductor sole command of the screen mirror, but the
    browser panel renders `run` / `level_start` / `plan_update` / `done` /
    `cancelled` frames that carry no decision for the Conductor. Rather than let
    a producer write to an owned resource, those frames travel through the inbox
    like everything else and the Conductor forwards them in one line.
    """

    run_id: str
    frame: dict[str, Any]


@dataclass(frozen=True)
class ResearchFailed:
    run_id: str
    reason: str  # "plan_timeout" | "level_gap" | "stream_error"


@dataclass(frozen=True)
class ResearchFinished:
    run_id: str
    status: str  # "complete" | "partial"


@dataclass(frozen=True)
class SpeechFinished:
    speech_id: str


@dataclass(frozen=True)
class SpeechInterrupted:
    speech_id: str
    spoken_text: str  # what actually reached the speaker before the cut

    @property
    def spoken_char_count(self) -> int:
        return len(self.spoken_text)


@dataclass(frozen=True)
class IdleTick:
    """1 Hz. Drives the attention timeout and nothing else."""
