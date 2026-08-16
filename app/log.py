"""Structured stdout tracing.

LiveKit runs the agent in a subprocess whose logging config is its own, so
every trace in this project is a flushed print rather than a logger call.
"""

from __future__ import annotations

import json
from typing import Any


def dbg(label: str, **fields: Any) -> None:
    print(
        "[AGENT DEBUG]",
        json.dumps({"label": label, **fields}, ensure_ascii=False, default=str),
        flush=True,
    )
