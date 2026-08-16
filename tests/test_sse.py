"""The SSE frame parser, independent of what the frames mean."""

from __future__ import annotations

import unittest

from app.rag.sse import iter_sse_events
from tests.fakes import AsyncLines


# ---------------------------------------------------------------------------
# sse transport
# ---------------------------------------------------------------------------


class SseTests(unittest.IsolatedAsyncioTestCase):
    async def test_parser_ignores_comments_and_flushes_at_eof(self):
        lines = AsyncLines(
            [
                ": connected",
                "",
                "event: run",
                'data: {"run_id":"r1"}',
                "",
                ": ping",
                "event: done",
                'data: {"type":"done",',
                'data: "status":"complete"}',
            ]
        )
        events = [event async for event in iter_sse_events(lines)]
        self.assertEqual(events[0], {"type": "run", "run_id": "r1"})
        self.assertEqual(events[1]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
