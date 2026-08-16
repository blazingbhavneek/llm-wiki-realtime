"""A line-oriented Server-Sent Events parser, used by ``llm_wiki.py``.

It turns a stream of SSE lines into decoded JSON frames and knows nothing about
what those frames mean - no run ids, no plans, no levels. Deliberately so: the
transport is generic and reusable, and every decision about a frame's meaning
belongs to the backend that reads it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from typing import Any


def _decode_frame(event_name: str | None, data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    if not isinstance(payload, dict):
        raise ValueError("SSE data must decode to a JSON object")
    if event_name and "type" not in payload:
        payload["type"] = event_name
    return payload


async def iter_sse_events(lines: AsyncIterable[str]):
    """Parse an SSE line stream, ignoring comments and flushing at EOF."""
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line.startswith(":"):
            continue
        if not line:
            frame = _decode_frame(event_name, data_lines)
            if frame is not None:
                yield frame
            event_name = None
            data_lines = []
            continue

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    frame = _decode_frame(event_name, data_lines)
    if frame is not None:
        yield frame
