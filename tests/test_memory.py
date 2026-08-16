"""Every level ever received, and what may still be read back."""

from __future__ import annotations

import unittest

from app.core.events import BACKGROUND, FOREGROUND
from app.core.memory import NEW, PARTIAL, REPORTED, SILENT, Memory
from tests.fakes import level_event


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


class _Run:
    def __init__(self, run_id="r1", question="q", focus=FOREGROUND):
        self.run_id = run_id
        self.question = question
        self.focus = focus


class MemoryTests(unittest.TestCase):
    def test_replayed_level_is_remembered_once(self):
        memory = Memory()
        run = _Run()
        first = memory.remember(run, level_event())
        self.assertIsNotNone(first)
        # a retry keeps the local run and replays the body under a fresh level id
        self.assertIsNone(memory.remember(run, level_event(level_id="level-1-retry")))
        self.assertIsNone(memory.remember(run, level_event()))
        self.assertIsNone(memory.remember(run, level_event(text="")))
        # a different question that happens to land on the same answer still speaks
        self.assertIsNotNone(memory.remember(_Run(run_id="r2", question="別の質問"), level_event()))

    def test_ladder_order_is_position_then_arrival(self):
        memory = Memory()
        run = _Run()
        memory.remember(run, level_event("l2", "二番目", position=2))
        first = memory.remember(run, level_event("l1", "一番目", position=1))
        self.assertIs(memory.next_new(FOREGROUND), first)
        self.assertIsNone(memory.next_new(BACKGROUND))

    def test_focus_moves_with_the_run(self):
        memory = Memory()
        run = _Run()
        level = memory.remember(run, level_event())
        memory.set_focus(run.run_id, BACKGROUND)
        self.assertIsNone(memory.next_new(FOREGROUND))
        self.assertIs(memory.next_new(BACKGROUND), level)

    def test_silent_and_reported_levels_stay_readable(self):
        memory = Memory()
        run = _Run(question="mpf_buf とは")
        level = memory.remember(run, level_event(objective="バッファの役割"))
        memory.mark_silent(level)
        self.assertEqual(level.state, SILENT)
        self.assertIs(memory.find("バッファの役割"), level)
        self.assertIs(memory.find("mpf_buf"), level)
        # never "not found" while anything exists
        self.assertIs(memory.find("まったく無関係な語"), level)
        self.assertIsNone(Memory().find("何か"))

    def test_partial_needs_audible_speech(self):
        memory = Memory()
        level = memory.remember(_Run(), level_event())
        memory.mark_reporting(level)
        memory.mark_partial(level, "  ")
        self.assertEqual(level.state, NEW)
        memory.mark_partial(level, "答えは")
        self.assertEqual(level.state, PARTIAL)
        self.assertEqual(level.spoken_char_count, 3)


if __name__ == "__main__":
    unittest.main()
