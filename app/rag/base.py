"""The research backend contract.

This is the surface ``Conductor`` already uses. Writing it down costs nothing
and turns the core layer's dependency on a research backend into a contract
rather than an accident: an implementation that quietly drops
``move_to_background`` or renames ``foreground_run`` fails against a Protocol
instead of failing at runtime, in Japanese, halfway through a report.

``app.core.conductor`` imports these under ``TYPE_CHECKING`` only, so the core
layer never imports a provider at runtime - it is handed one by
``app/runtime/``. Accordingly this module imports nothing but ``typing``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ResearchRun(Protocol):
    """One question in flight. It streams and reports; it decides nothing."""

    run_id: str
    question: str
    focus: str  # events.FOREGROUND | events.BACKGROUND
    planned_levels: list[dict[str, Any]]
    # The report count at the moment this run lost the floor; -1 means never.
    superseded_at_report: int
    finished: bool

    def start(self) -> None: ...

    async def cancel(self) -> None: ...


@runtime_checkable
class ResearchBackend(Protocol):
    """The pool of runs. Everything the Conductor asks of a research backend."""

    runs: dict[str, ResearchRun]

    def start(self, question: str) -> ResearchRun: ...

    def get(self, run_id: str) -> ResearchRun | None: ...

    def foreground_run(self) -> ResearchRun | None: ...

    def move_to_background(self, run: ResearchRun) -> None: ...

    def can_retry(self, run: ResearchRun) -> bool: ...

    def retry(self, run: ResearchRun) -> None: ...

    # Part of the pool's API but currently uncalled: the Conductor reads
    # `runs` directly and lets the idle timer notice when nothing is live.
    def active(self) -> list[ResearchRun]: ...

    async def cancel_all(self) -> None: ...
