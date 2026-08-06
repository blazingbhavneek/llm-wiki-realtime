"""Single-owner speech scheduler for queued research results."""

from __future__ import annotations

import asyncio
import inspect
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from delivery import DeliveryQueue, ResultChunk, sanitize_for_speech


DuckCallback = Callable[[bool], Awaitable[None] | None]


class EtaPredictor:
    def __init__(self, *, seed_ms: int = 2500) -> None:
        self.seed_ms = seed_ms
        self.latencies: deque[int] = deque(maxlen=9)
        self.level_started_at: dict[str, float] = {}
        self.bridged_runs: set[str] = set()

    def level_started(self, level_id: str, *, now: float | None = None) -> None:
        self.level_started_at[level_id] = time.monotonic() if now is None else now

    def level_completed(self, level_id: str, latency_ms: int) -> None:
        self.level_started_at.pop(level_id, None)
        if latency_ms > 0:
            self.latencies.append(latency_ms)

    @property
    def estimate_ms(self) -> int:
        if not self.latencies:
            return self.seed_ms
        return int(statistics.median(self.latencies))

    def strategy(self, remaining_speech_ms: int, *, expected_ms: int | None = None) -> str:
        eta = self.estimate_ms if expected_ms is None else expected_ms
        if eta < remaining_speech_ms:
            return "seamless"
        if eta < remaining_speech_ms * 1.3:
            return "stretch"
        return "bridge"


class SpeechScheduler:
    def __init__(
        self,
        session: Any,
        delivery: DeliveryQueue,
        *,
        chars_per_second: float = 6.0,
        duck_callback: DuckCallback | None = None,
    ) -> None:
        self.session = session
        self.delivery = delivery
        self.chars_per_second = chars_per_second
        self.duck_callback = duck_callback
        self.eta = EtaPredictor()
        self.user_speaking = False
        self.last_user_speech_at = 0.0
        self.current_chunk: ResultChunk | None = None
        self.current_handle: Any = None
        self.current_started_at = 0.0
        self._notices: deque[str] = deque()
        self._signal = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task | None = None

    @property
    def is_speaking(self) -> bool:
        return self.current_handle is not None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        self._signal.set()
        if self.current_handle is not None and hasattr(self.current_handle, "interrupt"):
            self.current_handle.interrupt()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def notify(self) -> None:
        self.delivery.compact_backpressure()
        self._signal.set()

    def enqueue_notice(self, text: str) -> None:
        value = sanitize_for_speech(text)
        if value:
            self._notices.append(value)
            self._signal.set()

    def level_started(self, level_id: str) -> None:
        self.eta.level_started(level_id)

    def level_completed(self, level_id: str, latency_ms: int) -> None:
        self.eta.level_completed(level_id, latency_ms)

    def user_started_speaking(self) -> None:
        self.user_speaking = True
        self.last_user_speech_at = time.monotonic()
        if self.is_speaking:
            self._set_ducked(True)

    def resolve_user_speech(self, *, addressed: bool) -> None:
        self.user_speaking = False
        self.last_user_speech_at = time.monotonic()
        if not addressed:
            self._set_ducked(False)
            self._signal.set()
            return
        if self.current_chunk is not None and self.current_chunk.state == "speaking":
            elapsed = max(0.0, time.monotonic() - self.current_started_at)
            offset = self.current_chunk.spoken_upto + int(elapsed * self.chars_per_second)
            self.delivery.mark_interrupted(self.current_chunk, offset)
        if self.current_handle is not None and hasattr(self.current_handle, "interrupt"):
            self.current_handle.interrupt()
        self._set_ducked(False)

    def _set_ducked(self, ducked: bool) -> None:
        if self.duck_callback is None:
            return
        result = self.duck_callback(ducked)
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    async def _await_handle(self, handle: Any) -> None:
        if inspect.isawaitable(handle):
            await handle
        elif hasattr(handle, "wait_for_playout"):
            await handle.wait_for_playout()

    async def _say(self, text: str, chunk: ResultChunk | None = None) -> None:
        if not text:
            return
        self.current_chunk = chunk
        self.current_started_at = time.monotonic()
        handle = self.session.say(
            text,
            allow_interruptions=False,
            # Full RAG bodies live in DeliveryQueue. The LLM receives only the
            # compact state block and can pull a body with read_result.
            add_to_chat_ctx=False,
        )
        self.current_handle = handle
        try:
            await self._await_handle(handle)
        finally:
            self.current_handle = None
            self.current_chunk = None
            self._set_ducked(False)

    async def _run(self) -> None:
        while not self._closed:
            if self.user_speaking or time.monotonic() - self.last_user_speech_at < 0.25:
                try:
                    await asyncio.wait_for(self._signal.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                self._signal.clear()
                continue

            if self._notices:
                notice = self._notices.popleft()
                await self._say(notice)
                continue

            chunk = self.delivery.next_pending()
            if chunk is not None:
                self.delivery.mark_speaking(chunk)
                remaining = sanitize_for_speech(chunk.remaining_text)
                if chunk.spoken_upto:
                    remaining = f"続きですが、{remaining}"
                elif chunk.should_hedge:
                    remaining = f"確実ではありませんが、{remaining}"
                try:
                    await self._say(remaining, chunk)
                except Exception:
                    if chunk.state == "speaking":
                        chunk.state = "pending"
                    # An explicit barge-in may make the provider's speech
                    # handle raise. The chunk was already moved to
                    # ``interrupted`` and the scheduler must stay alive.
                    if chunk.state != "interrupted":
                        await asyncio.sleep(0)
                    continue
                if chunk.state == "speaking":
                    self.delivery.mark_spoken(chunk)
                continue

            self._signal.clear()
            await self._signal.wait()
