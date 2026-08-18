"""Listening or dormant. The orb toggle, and spoken commands.

Attention gates input, never output. A dormant assistant still reports what it
found; it simply stops treating what it hears as a turn.

The orb is the manual override: press it to listen, press it again to stop.
When dormant, a final STT transcript beginning with the assistant's name also
opens the gate for that request. This is transcript-based wake detection, not
an audio wake-word engine, so it activates only after VAD and batch STT finish.
Typed text is always a turn; you do not have to be listening to read.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

DORMANT = "dormant"
OPEN = "open"

# Commands are plain regex and are checked before the LLM ever sees the text.
# A stop that depends on a healthy LLM is not a stop.
_STOP = re.compile(r"(やめて|止めて|停止|ストップ|キャンセル|もういい|やっぱりいい)")
_REPEAT = re.compile(r"(もう一回|もう一度|繰り返|さっきの.*(?:何|言って))")
_CONTINUE = re.compile(r"(続き|続けて|その先)")
_CLOSE = re.compile(r"^(ありがとう|ありがと|どうも|終了|さようなら|おわり|終わり)")

# Keep the name at the beginning.  Matching it anywhere would make ordinary
# discussion *about* AI Minato wake the assistant.  The ASR can render the
# same name in katakana, hiragana, ASCII/full-width initials, or phonetically.
_WAKE_WORD = re.compile(
    r"^\s*(?:え[ーえ]|えっと|あの)?[\s、,]*"
    r"(?:[AＡ][IＩ][\s　]*(?:みなと|ミナト)|(?:あい|アイ|えーあい|エーアイ)[\s　]*(?:みなと|ミナト))"
    r"(?:さん)?[\s、,、。！？!?：:]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Turn:
    accepted: bool
    text: str
    command: str  # "none" | "stop" | "repeat" | "continue" | "close"
    wake_activated: bool = False


class Attention:
    def __init__(self) -> None:
        self.state = DORMANT
        self.because = ""
        self.button_held = False
        self.last_activity_at = 0.0

    # -- commands from the Conductor ---------------------------------------

    def open(self, because: str, *, now: float | None = None) -> None:
        self.state = OPEN
        self.because = because
        self.last_activity_at = self._now(now)

    def close(self) -> None:
        self.state = DORMANT
        self.because = ""

    def set_button_held(self, held: bool) -> None:
        """Records the toggle only. Whether it also opens or closes is a
        decision, and decisions live in the Conductor."""
        self.button_held = held

    # -- the whole gate, one call ------------------------------------------

    def accept(
        self,
        text: str,
        *,
        from_text_input: bool = False,
        now: float | None = None,
    ) -> Turn:
        cleaned = (text or "").strip()

        wake_open = not from_text_input and self.state == OPEN and self.because == "wake_word"
        wake_match = None if (from_text_input or self.state == OPEN) else _WAKE_WORD.match(cleaned)

        # Speech counts while the orb is on, after a previous name-only wake,
        # or when this completed STT turn begins with the wake phrase.
        addressed = from_text_input or self.button_held or self.state == OPEN or wake_match
        if not addressed:
            return Turn(False, cleaned, "none")

        # A name-only wake leaves one final transcript pending.  That next
        # transcript is still the same one-request activation.
        wake_activated = wake_match is not None or wake_open
        if wake_match is not None:
            cleaned = cleaned[wake_match.end() :].strip()

        self.state = OPEN
        self.because = "text" if from_text_input else ("wake_word" if wake_activated else "speech")
        self.last_activity_at = self._now(now)

        command = self.classify(cleaned)
        if command == "close":
            self.close()
        return Turn(True, cleaned, command, wake_activated=wake_activated)

    @staticmethod
    def classify(text: str) -> str:
        if not text:
            return "none"
        if _CLOSE.match(text):
            return "close"
        if _STOP.search(text):
            return "stop"
        if _CONTINUE.search(text):
            return "continue"
        if _REPEAT.search(text):
            return "repeat"
        return "none"

    @staticmethod
    def _now(now: float | None) -> float:
        return time.monotonic() if now is None else now
