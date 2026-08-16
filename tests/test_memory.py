"""Every level ever received, and what may still be read back."""

from __future__ import annotations

import unittest

from app.agent.tools import NO_RETAINED_RESULT, read_retained
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
        # an empty handle still means "the latest"
        self.assertIs(memory.find(""), level)
        # but an unrelated handle misses, so read_result says so and the model
        # opens a run instead of answering off a neighbouring level
        self.assertIsNone(memory.find("cudaMemAdvise"))
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

    def test_spoken_for_is_what_was_said_not_the_evidence(self):
        memory = Memory()
        run = _Run()
        first = memory.remember(run, level_event("l1", "生の根拠", 1, "役割"))
        memory.mark_reported(first, "役割はこうです。")
        second = memory.remember(run, level_event("l2", "別の根拠", 2, "引数"))

        said = memory.spoken_for(run.run_id, exclude=second)
        self.assertIn("役割はこうです。", said)
        self.assertNotIn("生の根拠", said)
        # a run that has said nothing yet has nothing to be judged against
        self.assertEqual(memory.spoken_for("r-other"), "")


# ---------------------------------------------------------------------------
# the plan of a live run
# ---------------------------------------------------------------------------


class PlanTests(unittest.TestCase):
    def test_a_planned_objective_is_pending_until_its_level_arrives(self):
        memory = Memory()
        run = _Run(question="mpf_buf とは")
        memory.note_plan(
            run,
            [
                {"id": "l2", "objective": "引数の意味", "position": 2},
                {"id": "l1", "objective": "役割の確認", "position": 1},
            ],
        )
        # plan order, not the order the frames happened to list
        self.assertEqual([entry.objective for entry in memory.pending], ["役割の確認", "引数の意味"])

        memory.remember(run, level_event("l1", "役割の答え", 1, "役割の確認"))
        self.assertEqual([entry.objective for entry in memory.pending], ["引数の意味"])

        memory.close_plan(run.run_id)
        self.assertEqual(memory.pending, ())

    def test_a_revised_plan_replaces_the_old_one(self):
        memory = Memory()
        run = _Run()
        memory.note_plan(run, [{"id": "l1", "objective": "役割の確認", "position": 1}])
        memory.note_plan(run, [{"id": "l9", "objective": "別の観点", "position": 1}])
        self.assertEqual([entry.objective for entry in memory.pending], ["別の観点"])

    def test_only_the_foreground_plan_answers_a_follow_up(self):
        memory = Memory()
        run = _Run(question="mpf_buf とは")
        memory.note_plan(run, [{"id": "l1", "objective": "引数の意味", "position": 1}])
        self.assertIsNotNone(memory.await_pending("引数の意味"))
        # the user moved on: an old run's plan must not make the new question
        # look like it is already being answered
        memory.set_focus(run.run_id, BACKGROUND)
        self.assertIsNone(memory.await_pending("引数の意味"))

    def test_an_unrelated_follow_up_still_misses(self):
        memory = Memory()
        memory.note_plan(
            _Run(question="mpf_buf とは"),
            [{"id": "l1", "objective": "引数の意味", "position": 1}],
        )
        self.assertIsNone(memory.await_pending("cudaMemAdvise"))

    def test_a_level_someone_waited_for_arrives_forced(self):
        memory = Memory()
        run = _Run(question="mpf_buf とは")
        memory.note_plan(run, [{"id": "l1", "objective": "引数の意味", "position": 1}])
        memory.await_pending("引数の意味")
        level = memory.remember(run, level_event("l1", "引数の答え", 1, "引数の意味"))
        # asked for while in flight, so the report pass may not drop it
        self.assertTrue(level.forced)


class RetainedLookupTests(unittest.TestCase):
    def test_retained_beats_in_flight_beats_nothing(self):
        memory = Memory()
        run = _Run(question="mpf_buf とは")
        memory.note_plan(run, [{"id": "l2", "objective": "引数の意味", "position": 2}])
        self.assertIn("調査中", read_retained(memory, "引数の意味"))

        memory.remember(run, level_event("l2", "引数の答え", 2, "引数の意味"))
        # once it lands the raw evidence is handed over, not the narration
        self.assertIn("引数の答え", read_retained(memory, "引数の意味"))
        self.assertEqual(read_retained(Memory(), "何か"), NO_RETAINED_RESULT)


if __name__ == "__main__":
    unittest.main()
