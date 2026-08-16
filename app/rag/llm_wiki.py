"""SSE research runs. The only file that talks to the llm-wiki backend.

A run never decides anything. It opens a stream, pushes events into the inbox,
and dies. Whether its findings are spoken, and whether a failure is retried, is
the Conductor's call.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from app.core.events import (
    BACKGROUND,
    FOREGROUND,
    LevelReady,
    PlanReady,
    ResearchFailed,
    ResearchFinished,
    ResearchProgress,
)
from app.rag.sse import iter_sse_events

PLANNING = "planning"
RESEARCHING = "researching"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# The endpoint's own defaults, per SSE_SPEC. The previous values asked for about
# half the retrieval the backend offers; `max_queries_per_level: 4` was never
# reachable inside a 12s stage budget, so queries 3 and 4 were cut every time.
VOICE_KNOBS: dict[str, int] = {
    "max_levels": 3,
    "max_queries_per_level": 2,
    "search_limit": 16,
    "max_context_chars": 32_000,
    "min_initial_read_nodes": 16,
    "deadline_seconds": 90,
}


def _base_url() -> str:
    return os.getenv("LLM_WIKI_BASE_URL", "http://10.160.152.38:8000").rstrip("/")


def _database() -> str:
    return os.getenv("LLM_WIKI_DATABASE", "moove_wiki").strip("/")


def _prefix() -> str:
    return os.getenv("LLM_WIKI_PREFIX", "/llm-wiki").strip("/")


def stream_url() -> str:
    override = os.getenv("LLM_WIKI_REALTIME_URL")
    if override:
        return override
    return f"{_base_url()}/{_prefix()}/{_database()}/api/ask/realtime/stream"


def cancel_url(run_id: str) -> str:
    return f"{_base_url()}/{_prefix()}/{_database()}/api/agent-runs/{run_id}/stop"


def plan_timeout() -> float:
    value = os.getenv("RAG_PLAN_TIMEOUT_SECONDS") or os.getenv(
        "RAG_INITIAL_PLAN_TIMEOUT_SECONDS", "5"
    )
    return float(value)


def level_timeout() -> float:
    return float(os.getenv("RAG_LEVEL_TIMEOUT_SECONDS", "20"))


def max_retries() -> int:
    return int(os.getenv("RAG_STREAM_MAX_RETRIES", "1"))


async def stop_remote_run(remote_run_id: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(cancel_url(remote_run_id))
        response.raise_for_status()


_stop_tasks: set[asyncio.Task] = set()


def stop_remote_run_soon(remote_run_id: str | None) -> None:
    """Tell the backend to drop the run without making anyone wait for it."""
    if not remote_run_id:
        return

    async def attempt() -> None:
        try:
            await stop_remote_run(remote_run_id)
        except Exception:
            pass

    task = asyncio.create_task(attempt())
    _stop_tasks.add(task)
    task.add_done_callback(_stop_tasks.discard)


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


class ResearchRun:
    def __init__(self, run_id: str, question: str, inbox: asyncio.Queue) -> None:
        self.run_id = run_id  # local and stable; survives a retry
        self.question = question
        self.inbox = inbox
        self.state = PLANNING
        self.focus = FOREGROUND
        self.planned_levels: list[dict[str, Any]] = []
        self.remote_run_id: str | None = None
        self.attempts = 0
        self.levels_received = 0
        # Set by the Conductor when it backgrounds this run: the report count at
        # the moment it lost the floor. -1 means it never has.
        self.superseded_at_report = -1
        self.task: asyncio.Task | None = None
        self._plan_seen = False
        self._plan_version = 0
        self._log = print

    def start(self) -> None:
        """Open one attempt. Returns immediately; the stream runs detached."""
        self.attempts += 1
        self.state = PLANNING
        self._plan_seen = False
        self._plan_version = 0
        self.remote_run_id = None
        self.task = asyncio.create_task(self._attempt(), name=f"research:{self.run_id}")

    async def cancel(self) -> None:
        # Local first: the stream must stop feeding the inbox immediately, and
        # nobody waits on the backend acknowledging the stop.
        self.state = CANCELLED
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
        stop_remote_run_soon(self.remote_run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELLED)

    # -- the attempt -------------------------------------------------------

    async def _attempt(self) -> None:
        progress: asyncio.Queue[str] = asyncio.Queue()
        stream_task = asyncio.create_task(self._consume(progress))
        watchdog_task = asyncio.create_task(self._watchdog(progress, stream_task))
        try:
            await asyncio.gather(stream_task, watchdog_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        finally:
            watchdog_task.cancel()
            stream_task.cancel()

    async def _watchdog(self, progress: asyncio.Queue[str], stream_task: asyncio.Task) -> None:
        """Terminates when the stream terminates - not when the plan looks complete.

        A `partial` run legitimately ends with fewer levels than planned. The
        previous version only stopped once every planned level arrived, so it
        re-ran the whole question 20s after it had already answered it.
        """
        while True:
            timeout = plan_timeout() if not self._plan_seen else level_timeout()
            try:
                token = await asyncio.wait_for(progress.get(), timeout=timeout)
            except asyncio.TimeoutError:
                reason = "plan_timeout" if not self._plan_seen else "level_gap"
                self.state = FAILED
                stop_remote_run_soon(self.remote_run_id)
                stream_task.cancel()
                self.inbox.put_nowait(ResearchFailed(self.run_id, reason))
                return
            if token == "end":
                return

    async def _consume(self, progress: asyncio.Queue[str]) -> None:
        try:
            timeout = httpx.Timeout(None, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    stream_url(),
                    json={"question": self.question, **VOICE_KNOBS},
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                ) as response:
                    response.raise_for_status()
                    async for frame in iter_sse_events(response.aiter_lines()):
                        if self._on_frame(frame, progress):
                            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state = FAILED
            self.inbox.put_nowait(ResearchFailed(self.run_id, f"stream_error: {exc!r}"))
            progress.put_nowait("end")
            return

        # The stream closed without a terminal frame.
        if self.levels_received:
            self.state = DONE
            self.inbox.put_nowait(ResearchFinished(self.run_id, "partial"))
        else:
            self.state = FAILED
            self.inbox.put_nowait(ResearchFailed(self.run_id, "stream_error: closed early"))
        progress.put_nowait("end")

    def _on_frame(self, frame: dict[str, Any], progress: asyncio.Queue[str]) -> bool:
        """Translate one SSE frame. Returns True when the stream is over."""
        kind = str(frame.get("type") or "")
        self.inbox.put_nowait(ResearchProgress(self.run_id, frame))

        if kind == "run":
            if frame.get("run_id"):
                self.remote_run_id = str(frame["run_id"])
            return False

        if kind == "plan":
            self._plan_seen = True
            self._plan_version = int(frame.get("version") or 0)
            self.planned_levels = list(frame.get("levels") or [])
            self.state = RESEARCHING
            progress.put_nowait("plan")
            self.inbox.put_nowait(PlanReady(self.run_id, self.planned_levels))
            return False

        if kind == "plan_update":
            version = int(frame.get("version") or 0)
            if version > self._plan_version:
                self._plan_version = version
                self.planned_levels = list(frame.get("levels") or self.planned_levels)
            progress.put_nowait("plan_update")
            return False

        if kind == "level_start":
            progress.put_nowait("level_start")
            return False

        if kind == "level":
            self.levels_received += 1
            progress.put_nowait("level")
            self.inbox.put_nowait(LevelReady(self.run_id, frame))
            return False

        if kind == "done":
            self.state = DONE
            status = str(frame.get("status") or "complete")
            self.inbox.put_nowait(ResearchFinished(self.run_id, status))
            progress.put_nowait("end")
            return True

        if kind == "error":
            self.state = FAILED
            message = str(frame.get("message") or "unknown")
            self.inbox.put_nowait(ResearchFailed(self.run_id, f"stream_error: {message}"))
            progress.put_nowait("end")
            return True

        if kind == "cancelled":
            self.state = CANCELLED
            self.inbox.put_nowait(ResearchFinished(self.run_id, "partial"))
            progress.put_nowait("end")
            return True

        return False


# ---------------------------------------------------------------------------
# All runs
# ---------------------------------------------------------------------------


class ResearchPool:
    def __init__(self, inbox: asyncio.Queue) -> None:
        self.inbox = inbox
        self.runs: dict[str, ResearchRun] = {}

    def start(self, question: str) -> ResearchRun:
        run = ResearchRun(f"run-{time.monotonic_ns():x}", question, self.inbox)
        self.runs[run.run_id] = run
        run.start()
        return run

    def get(self, run_id: str) -> ResearchRun | None:
        return self.runs.get(run_id)

    def foreground_run(self) -> ResearchRun | None:
        for run in reversed(list(self.runs.values())):
            if run.focus == FOREGROUND and not run.finished:
                return run
        return None

    def move_to_background(self, run: ResearchRun) -> None:
        """A superseded run is never cancelled. It finishes and it is remembered."""
        run.focus = BACKGROUND

    def can_retry(self, run: ResearchRun) -> bool:
        return run.attempts <= max_retries()

    def retry(self, run: ResearchRun) -> None:
        run.start()

    def active(self) -> list[ResearchRun]:
        return [run for run in self.runs.values() if not run.finished]

    async def cancel_all(self) -> None:
        await asyncio.gather(*(run.cancel() for run in self.runs.values()), return_exceptions=True)


# The registry names this backend `LLMWikiBackend`; the code has always called it
# `ResearchPool`, and every call site still does.
LLMWikiBackend = ResearchPool
