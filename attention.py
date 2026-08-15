"""Listening or dormant. The orb toggle, and spoken commands.

Attention gates input, never output. A dormant assistant still reports what it
found; it simply stops treating what it hears as a turn.

The gate is the orb: press it to listen, press it again to stop. There is no
wake word, and nothing else opens or closes attention on the user's behalf -
no idle timeout, no reply window - because an explicit toggle means the user
said when to start and will say when to stop. Typed text is always a turn; you
do not have to be listening to read.
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


@dataclass(frozen=True)
class Turn:
    accepted: bool
    text: str
    command: str  # "none" | "stop" | "repeat" | "continue" | "close"


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

        # Speech counts only while the orb is on. `state` is checked too so a
        # turn already in flight is not dropped by the release that lands
        # between the transcript and this call.
        addressed = from_text_input or self.button_held or self.state == OPEN
        if not addressed:
            return Turn(False, cleaned, "none")

        self.state = OPEN
        self.because = "text" if from_text_input else "speech"
        self.last_activity_at = self._now(now)

        command = self.classify(cleaned)
        if command == "close":
            self.close()
        return Turn(True, cleaned, command)

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
