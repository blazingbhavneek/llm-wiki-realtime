"""Every result ever received, spoken or not, and every result still coming.

Not speaking something is not the same as forgetting it: SILENT and REPORTED
levels stay here forever and ``find`` can still reach both.

The plan of a live run is kept here too, so that "we have not looked that up"
and "that answer is on its way" can be told apart before anything has arrived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.events import BACKGROUND, FOREGROUND

NEW = "new"
REPORTING = "reporting"
REPORTED = "reported"
PARTIAL = "partial"
SILENT = "silent"


class RunLike(Protocol):
    """What Memory needs from a run. It never calls back into ResearchPool."""

    run_id: str
    question: str
    focus: str


@dataclass
class LevelResult:
    run_id: str
    question: str  # the question this level belongs to
    objective: str
    text: str
    level_id: str = ""
    position: int = 0
    focus: str = FOREGROUND
    state: str = NEW
    spoken_text: str = ""  # the narration already delivered, not a slice of `text`
    serial: int = 0  # arrival order, the only ordering Memory trusts
    reported_serial: int = 0
    # This level was explicitly asked for, so the report pass may not decide it
    # is not worth speaking. Set by a repeat, or by a follow-up that waited for it.
    forced: bool = False

    @property
    def spoken_char_count(self) -> int:
        return len(self.spoken_text)

    @property
    def remaining_text(self) -> str:
        """Evidence still owed to the user.

        A report is narrated by the LLM rather than read out verbatim, so the
        remainder is not a suffix of ``text``. What resumes a cut-off report is
        the evidence plus the sentence fragment already spoken, which the report
        prompt uses to continue rather than restart.
        """
        return self.text


@dataclass
class PendingObjective:
    """A planned level that has not arrived yet.

    The plan names every objective up front, long before the levels complete, so
    a follow-up can be told "that is being looked into" instead of opening a
    second run for something the first run is already fetching.
    """

    run_id: str
    question: str
    objective: str
    level_id: str = ""
    focus: str = FOREGROUND
    asked: bool = False  # a follow-up is waiting on this one


class Memory:
    def __init__(self) -> None:
        self._levels: list[LevelResult] = []
        self._pending: list[PendingObjective] = []
        self._keys: set[tuple[str, str]] = set()
        self._fingerprints: set[tuple[str, str]] = set()
        self._serial = 0
        self._reports = 0

    @property
    def levels(self) -> tuple[LevelResult, ...]:
        return tuple(self._levels)

    @property
    def reports_delivered(self) -> int:
        """Monotonic count of finished reports. Used to age background findings."""
        return self._reports

    @property
    def pending(self) -> tuple[PendingObjective, ...]:
        return tuple(self._pending)

    # -- writing -----------------------------------------------------------

    def remember(self, run: RunLike, level: dict[str, Any]) -> LevelResult | None:
        text = str(level.get("voice_text") or level.get("text") or "").strip()
        if not text:
            return None
        level_id = str(level.get("level_id") or level.get("id") or "")
        key = (run.run_id, level_id)
        # A retry keeps the local run id but opens a fresh backend run with fresh
        # level ids, replaying what already completed. Scoped to the run, so the
        # same answer to a genuinely new question is still reported.
        fingerprint = (run.run_id, text)
        if key in self._keys or fingerprint in self._fingerprints:
            return None
        self._serial += 1
        result = LevelResult(
            run_id=run.run_id,
            question=run.question,
            objective=str(level.get("objective") or "調査結果"),
            text=text,
            level_id=level_id,
            position=int(level.get("position") or 0),
            focus=run.focus,
            serial=self._serial,
        )
        self._keys.add(key)
        self._fingerprints.add(fingerprint)
        self._levels.append(result)
        self._settle(result)
        return result

    def note_plan(self, run: RunLike, planned_levels: list[dict[str, Any]]) -> None:
        """Record what this run still owes. Replaces the run's previous plan, so
        a revised plan shrinks the pending set instead of stacking onto it."""
        waited_on = {
            entry.objective
            for entry in self._pending
            if entry.asked and entry.run_id == run.run_id
        }
        self._pending = [entry for entry in self._pending if entry.run_id != run.run_id]
        # A retry replays what already completed under fresh level ids, so the
        # objective, not the id, is what says "this one is already in".
        arrived = {level.objective for level in self._levels if level.run_id == run.run_id}
        for item in sorted(planned_levels, key=lambda item: int(item.get("position") or 0)):
            level_id = str(item.get("id") or item.get("level_id") or "")
            objective = str(item.get("objective") or "")
            if not objective or objective in arrived or (run.run_id, level_id) in self._keys:
                continue  # already arrived; the level itself is the answer now
            self._pending.append(
                PendingObjective(
                    run_id=run.run_id,
                    question=run.question,
                    objective=objective,
                    level_id=level_id,
                    focus=run.focus,
                    asked=objective in waited_on,
                )
            )

    def close_plan(self, run_id: str) -> None:
        """The run is over. Whatever never arrived is not coming."""
        self._pending = [entry for entry in self._pending if entry.run_id != run_id]

    def _settle(self, result: LevelResult) -> None:
        """An arrival cancels its own pending entry, and inherits the wait."""
        for entry in tuple(self._pending):
            if entry.run_id != result.run_id:
                continue
            same_id = bool(entry.level_id) and entry.level_id == result.level_id
            same_objective = bool(entry.objective) and entry.objective == result.objective
            if same_id or same_objective:
                result.forced = result.forced or entry.asked
                self._pending.remove(entry)
                return

    def set_focus(self, run_id: str, focus: str) -> None:
        for level in self._levels:
            if level.run_id == run_id:
                level.focus = focus
        for entry in self._pending:
            if entry.run_id == run_id:
                entry.focus = focus

    def mark_reporting(self, level: LevelResult) -> None:
        level.state = REPORTING

    def mark_reported(self, level: LevelResult, spoken_text: str = "") -> None:
        if spoken_text:
            level.spoken_text = spoken_text
        level.state = REPORTED
        self._reports += 1
        level.reported_serial = self._reports

    def mark_partial(self, level: LevelResult, spoken_text: str) -> None:
        level.spoken_text = spoken_text
        # Nothing audible reached the user, so this is not a resumable fragment.
        level.state = PARTIAL if spoken_text.strip() else NEW

    def mark_silent(self, level: LevelResult) -> None:
        level.state = SILENT

    def mark_new(self, level: LevelResult) -> None:
        level.state = NEW

    # -- reading -----------------------------------------------------------

    def next_new(self, focus: str) -> LevelResult | None:
        return self._first(focus, NEW)

    def next_partial(self, focus: str) -> LevelResult | None:
        return self._first(focus, PARTIAL)

    def last_reported(self) -> LevelResult | None:
        for level in reversed(self._levels):
            if level.state == REPORTED:
                return level
        return None

    def last_partial(self) -> LevelResult | None:
        for level in reversed(self._levels):
            if level.state == PARTIAL:
                return level
        return None

    def spoken_for(self, run_id: str, exclude: LevelResult | None = None) -> str:
        """What this question has already been told, in the order it was said.

        The report pass needs it to judge whether the level in front of it adds
        anything; without it "do not repeat the previous step" is unenforceable.
        """
        lines = [
            f"{level.objective}: {level.spoken_text.strip()}"
            for level in self._levels
            if level.run_id == run_id and level is not exclude and level.spoken_text.strip()
        ]
        return "\n".join(lines)[-800:]

    def await_pending(self, handle: str) -> PendingObjective | None:
        """A follow-up the live plan already covers, or None.

        Only the foreground run is consulted: a background run's plan must never
        make a genuinely new question look like it is already being answered.
        Matching marks the objective as waited on, which is what later stops its
        level from being judged not worth speaking.
        """
        candidates = [entry for entry in self._pending if entry.focus == FOREGROUND]
        if not candidates:
            return None
        needle = (handle or "").strip().lower()
        if not needle:
            best = candidates[0]  # plan order: the next thing we will know
        else:
            tokens = set(_tokens(needle))
            scored = [(_pending_score(needle, tokens, entry), entry) for entry in candidates]
            score, best = max(scored, key=lambda pair: pair[0])
            if not score:
                return None
        best.asked = True
        return best

    def _first(self, focus: str, state: str) -> LevelResult | None:
        candidates = [
            level for level in self._levels if level.focus == focus and level.state == state
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda level: (level.position, level.serial))

    def find(self, handle: str) -> LevelResult | None:
        """Fuzzy, but it is allowed to miss.

        Still generous: an empty handle means "the latest", and a partial or
        misspelled one still resolves by token overlap. What it will not do is
        hand back an unrelated level for a handle that matches nothing. That
        fallback used to exist to avoid a redundant research run, but a level
        about a neighbouring topic is exactly what the model needs to answer a
        genuinely new question without looking it up. A redundant run is the
        cheaper mistake.
        """
        if not self._levels:
            return None
        needle = (handle or "").strip().lower()
        if not needle:
            return self._levels[-1]

        for level in reversed(self._levels):
            if needle in (level.level_id.lower(), level.objective.lower()):
                return level
        for level in reversed(self._levels):
            if needle in level.objective.lower() or needle in level.question.lower():
                return level

        tokens = set(_tokens(needle))
        if tokens:
            # Both sides lowercased: `needle` already is, and an un-normalised
            # haystack made every handle containing ASCII score zero.
            scored = [
                (
                    len(
                        tokens
                        & set(
                            _tokens(
                                f"{level.objective} {level.question} {level.text}".lower()
                            )
                        )
                    ),
                    level,
                )
                for level in self._levels
            ]
            best_score, best = max(scored, key=lambda pair: (pair[0], pair[1].serial))
            if best_score:
                return best
        return None

    def summary_for_llm(self) -> str:
        lines: list[str] = []
        if self._levels:
            lines += ["[保持している調査結果]", "handle\t状態\t質問"]
            lines += [
                f"{level.objective}\t{level.state}\t{level.question}"
                for level in self._levels[-12:]
            ]
        # Named, not just counted: the model has to be able to tell whether the
        # follow-up in front of it is one of these before it opens a second run.
        waiting = [entry for entry in self._pending if entry.focus == FOREGROUND]
        if waiting:
            lines += ["[調査中でまだ届いていない観点]"]
            lines += [entry.objective for entry in waiting[:8]]
        return "\n".join(lines)


def _pending_score(needle: str, tokens: set[str], entry: PendingObjective) -> int:
    haystack = f"{entry.objective} {entry.question}".lower()
    if needle in haystack or (entry.objective and entry.objective.lower() in needle):
        return 100
    return len(tokens & set(_tokens(haystack)))


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^0-9A-Za-z぀-ヿ一-鿿]+", text) if token]


__all__ = [
    "BACKGROUND",
    "FOREGROUND",
    "NEW",
    "PARTIAL",
    "REPORTED",
    "REPORTING",
    "SILENT",
    "LevelResult",
    "Memory",
    "PendingObjective",
]
