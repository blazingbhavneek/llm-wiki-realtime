"""Stopwatch for one voice turn, from end-of-speech to first audio out.

Answers "where did the latency go" without a profiler: each stage marks
itself, and every mark prints the gap since the previous one and the total
since the user stopped talking. Off unless TURN_TIMING=1, so nothing here
costs anything in normal runs.

The stages, in order:

    eos          Silero heard the user stop. Everything after is our cost.
    stt_final    the transcript came back from the ASR server.
    accepted     the Conductor took it as a turn (gated by the orb).
    llm_request  generate_reply() handed it to the LLM.
    llm_first    the LLM produced something to say.
    audio_out    TTS put the first sample on the wire.
"""

from __future__ import annotations

import os
import time

ENABLED = os.getenv("TURN_TIMING") == "1"

_started_at: float | None = None
_last_at: float | None = None
_last_stage: str = ""


def start(stage: str = "eos") -> None:
    """Begin a turn. Any previous, unfinished turn is discarded."""
    global _started_at, _last_at, _last_stage
    if not ENABLED:
        return
    _started_at = _last_at = time.monotonic()
    _last_stage = stage
    print(f"[TURN] {stage:<12} +0ms (total 0ms)", flush=True)


def mark(stage: str, **fields: object) -> None:
    """Record a stage. A mark before any start() begins the turn itself, so
    a path that skips end-of-speech (typed text) still gets measured."""
    global _last_at, _last_stage
    if not ENABLED:
        return
    if _started_at is None or _last_at is None:
        start(stage)
        return
    now = time.monotonic()
    delta = (now - _last_at) * 1000
    total = (now - _started_at) * 1000
    extra = f"  {fields}" if fields else ""
    print(
        f"[TURN] {stage:<12} +{delta:.0f}ms since {_last_stage} "
        f"(total {total:.0f}ms){extra}",
        flush=True,
    )
    _last_at, _last_stage = now, stage
