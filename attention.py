"""Conversation-window attention state and keyword-spot fallback."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum


class AttentionState(str, Enum):
    DORMANT = "dormant"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class AttentionDecision:
    accepted: bool
    addressed: bool
    text: str
    action: str = "turn"
    wake_detected: bool = False


class AttentionFSM:
    def __init__(self, *, idle_seconds: float = 20.0, wake_words: tuple[str, ...] | None = None):
        self.idle_seconds = idle_seconds
        self.wake_words = wake_words or ("モーヴィ", "モービー", "モーヴィー", "movi", "moovy")
        self.state = AttentionState.DORMANT
        self.last_exchange_at = 0.0
        self.reply_window_until = 0.0
        self.research_in_flight = 0

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def _wake_pattern(self) -> re.Pattern[str]:
        alternatives = "|".join(re.escape(word) for word in self.wake_words)
        return re.compile(alternatives, flags=re.IGNORECASE)

    def _strip_wake_word(self, text: str) -> tuple[str, bool]:
        pattern = self._wake_pattern()
        found = bool(pattern.search(text))
        cleaned = pattern.sub("", text)
        cleaned = re.sub(r"^[\s、,。.!！?？:：]+|[\s]+$", "", cleaned)
        return cleaned, found

    def _expire_if_idle(self, now: float) -> None:
        if (
            self.state == AttentionState.OPEN
            and not self.research_in_flight
            and now - self.last_exchange_at >= self.idle_seconds
        ):
            self.state = AttentionState.DORMANT

    def research_started(self) -> None:
        self.research_in_flight += 1

    def research_finished(self) -> None:
        self.research_in_flight = max(0, self.research_in_flight - 1)

    def agent_asked(self, *, now: float | None = None) -> None:
        current = self._now(now)
        self.reply_window_until = current + 3.0
        self.last_exchange_at = current

    def close(self) -> None:
        self.state = AttentionState.DORMANT
        self.reply_window_until = 0.0

    def evaluate(
        self,
        text: str,
        *,
        now: float | None = None,
        text_input: bool = False,
        only_speaker: bool = True,
    ) -> AttentionDecision:
        current = self._now(now)
        self._expire_if_idle(current)
        cleaned, wake_detected = self._strip_wake_word(text.strip())

        if self._is_close_command(cleaned):
            accepted = text_input or wake_detected or self.state == AttentionState.OPEN
            if accepted:
                self.close()
            return AttentionDecision(accepted, accepted, cleaned, "close", wake_detected)

        reply_window = current <= self.reply_window_until
        imperative = self._is_interrupt_command(cleaned)
        addressed = text_input or wake_detected or imperative or reply_window or (
            self.state == AttentionState.OPEN and only_speaker
        )

        if self.state == AttentionState.DORMANT and not (text_input or wake_detected):
            return AttentionDecision(False, False, cleaned, "ignore", wake_detected)
        if not addressed:
            return AttentionDecision(False, False, cleaned, "ignore", wake_detected)

        self.state = AttentionState.OPEN
        self.last_exchange_at = current
        if not cleaned:
            return AttentionDecision(True, True, "", "wake", wake_detected)
        if self._is_cancel_command(cleaned):
            return AttentionDecision(True, True, cleaned, "cancel", wake_detected)
        if self._is_continue_command(cleaned):
            return AttentionDecision(True, True, cleaned, "continue", wake_detected)
        if self._is_repeat_command(cleaned):
            return AttentionDecision(True, True, cleaned, "repeat", wake_detected)
        return AttentionDecision(True, True, cleaned, "turn", wake_detected)

    @staticmethod
    def _is_interrupt_command(text: str) -> bool:
        return bool(re.match(r"^(待って|ちょっと|違う|やめて|ストップ|止めて)", text))

    @staticmethod
    def _is_cancel_command(text: str) -> bool:
        return bool(re.search(r"(やっぱりいい|調査を?やめ|検索を?やめ|キャンセル|もういい)", text))

    @staticmethod
    def _is_continue_command(text: str) -> bool:
        return bool(re.fullmatch(r"(?:続き|続きを|続けて|その先)(?:お願い|ください)?[。.!！]?", text))

    @staticmethod
    def _is_repeat_command(text: str) -> bool:
        return bool(re.search(r"(もう一回|もう一度|さっきの.*(?:何|言って)|繰り返)", text))

    @staticmethod
    def _is_close_command(text: str) -> bool:
        return bool(re.fullmatch(r"(?:ありがとう|ありがと|もういいよ|終了|さようなら)[。.!！]?", text))
