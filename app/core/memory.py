"""Every result ever received, spoken or not.

Not speaking something is not the same as forgetting it: SILENT and REPORTED
levels stay here forever and ``find`` can still reach both.
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


class Memory:
    def __init__(self) -> None:
        self._levels: list[LevelResult] = []
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
        return result

    def set_focus(self, run_id: str, focus: str) -> None:
        for level in self._levels:
            if level.run_id == run_id:
                level.focus = focus

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

    def _first(self, focus: str, state: str) -> LevelResult | None:
        candidates = [
            level for level in self._levels if level.focus == focus and level.state == state
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda level: (level.position, level.serial))

    def find(self, handle: str) -> LevelResult | None:
        """Fuzzy on purpose.

        Returning "not found" while any level exists pushes the LLM into opening
        a redundant research run, which is far worse than reading a slightly
        wrong level.
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
            scored = [
                (len(tokens & set(_tokens(f"{level.objective} {level.question} {level.text}"))), level)
                for level in self._levels
            ]
            best_score, best = max(scored, key=lambda pair: (pair[0], pair[1].serial))
            if best_score:
                return best
        return self._levels[-1]

    def summary_for_llm(self) -> str:
        if not self._levels:
            return ""
        lines = ["[保持している調査結果]", "handle\t状態\t質問"]
        for level in self._levels[-12:]:
            lines.append(f"{level.objective}\t{level.state}\t{level.question}")
        return "\n".join(lines)


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
]
