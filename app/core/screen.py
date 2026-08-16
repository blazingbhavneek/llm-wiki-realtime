"""Everything the browser sees. Commanded only by the Conductor.

The plan draws the screen mirror as an owned resource without giving it a file;
this is that file. It holds no state and makes no decision - it is a wire.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class Screen:
    def __init__(self, room: Any) -> None:
        self.room = room
        self._tasks: set[asyncio.Task] = set()

    def publish_research(self, frame: dict[str, Any]) -> None:
        """Mirror one raw SSE frame so the panel and the voice see the same run."""
        self._send("research", frame)

    def set_ducked(self, ducked: bool) -> None:
        self._send("attention", {"type": "duck", "ducked": ducked})

    def set_attention(self, state: str) -> None:
        self._send("attention", {"type": "attention", "state": state})

    def set_agent_status(self, state: str, detail: str = "") -> None:
        """"I am no longer able to hear you." The browser draws the orb from
        its own microphone, so a broken agent looks identical to a working one
        until it says otherwise. ``state`` is "degraded" or "closed"."""
        self._send("attention", {"type": "agent_status", "state": state, "detail": detail})

    def _send(self, topic: str, payload: dict[str, Any]) -> None:
        if self.room is None:
            return
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # Fire and forget: a dropped panel update must never stall the Conductor.
        task = asyncio.create_task(self._publish(data, topic))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish(self, data: bytes, topic: str) -> None:
        try:
            await self.room.local_participant.publish_data(data, reliable=True, topic=topic)
        except Exception:
            pass
