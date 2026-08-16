"""The single event loop - one test per acceptance check in the design."""

from __future__ import annotations

import unittest

from app.agent.tools import NO_RETAINED_RESULT, read_retained
from app.core.attention import DORMANT, OPEN
from app.core.events import (
    BACKGROUND,
    IdleTick,
    LevelReady,
    ListenButtonChanged,
    PlanReady,
    ResearchFailed,
    ResearchFinished,
    SpeechInterrupted,
    UserSaidText,
    UserStartedSpeaking,
)
from app.core.memory import NEW, PARTIAL, REPORTED, SILENT
from tests.fakes import build, feed, level_event


# ---------------------------------------------------------------------------
# the conductor - one test per acceptance check in the design
# ---------------------------------------------------------------------------


class ConductorTests(unittest.IsolatedAsyncioTestCase):
    async def test_chatter_ducks_but_never_interrupts(self):
        conductor = build()
        speaker, screen = conductor.speaker, conductor.screen
        speaker.start_report("報告中")

        await feed(conductor, UserStartedSpeaking(), UserSaidText("隣の人との雑談"))

        self.assertEqual(speaker.interrupts, 0)
        self.assertTrue(speaker.busy)
        self.assertFalse(speaker.ducked)
        self.assertIs(screen.ducked, False)  # ducked then unducked

    async def test_speech_during_a_report_is_a_real_barge_in(self):
        conductor = build()
        speaker = conductor.speaker
        await feed(conductor, ListenButtonChanged(True))
        run = conductor.pool.start("A")
        level = conductor.memory.remember(run, level_event())
        await feed(conductor, IdleTick())  # ladder picks the level up
        self.assertEqual(speaker.started[-1][0], "report")
        reporting = speaker._current

        await feed(conductor, UserStartedSpeaking(), UserSaidText("待って"))
        self.assertEqual(speaker.interrupts, 1)
        # the cut speech reports back afterwards; the level keeps what was said
        await feed(conductor, SpeechInterrupted(reporting, "答えは"))
        self.assertEqual(level.state, PARTIAL)
        self.assertEqual(level.spoken_text, "答えは")

    async def test_partial_resumes_before_new_and_is_attributed(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run_a = conductor.pool.start("A")
        conductor.start_research("B")  # A becomes background, queues an immediate notice
        await conductor.speak_next()  # starts that notice
        await feed(conductor, speaker.finish())  # and clears it before the level ladder runs
        run_b = conductor.pool.foreground_run()

        old = memory.remember(run_a, level_event("a1", "Aの答え"))
        memory.set_focus(run_a.run_id, BACKGROUND)
        memory.mark_reporting(old)
        memory.mark_partial(old, "Aの答えは")
        new = memory.remember(run_b, level_event("b1", "Bの答え"))

        await feed(conductor, IdleTick())
        # foreground B outranks the background remainder
        self.assertIn("Bの答え", speaker.started[-1][1])
        self.assertEqual(new.state, "reporting")

        await feed(conductor, speaker.finish())
        self.assertEqual(new.state, REPORTED)
        self.assertIn("Aの答え", speaker.started[-1][1])
        self.assertIn("さっきの件ですが", speaker.started[-1][1])
        self.assertIn("続きですが", speaker.started[-1][1])

    async def test_stale_background_finding_goes_silent(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run_a = conductor.pool.start("A")
        conductor.start_research("B")  # queues an immediate notice
        await conductor.speak_next()  # starts that notice
        await feed(conductor, speaker.finish())  # and clears it before the level ladder runs
        run_b = conductor.pool.foreground_run()

        # three foreground reports pass while A sits in the background
        for index in range(4):
            level = memory.remember(run_b, level_event(f"b{index}", f"B{index}", position=index))
            await feed(conductor, IdleTick())
            await feed(conductor, speaker.finish())

        stale = memory.remember(run_a, level_event("a1", "Aの遅い答え"))
        memory.set_focus(run_a.run_id, BACKGROUND)
        await feed(conductor, IdleTick())
        self.assertEqual(stale.state, SILENT)
        # silent is not deletion
        self.assertIs(memory.find("Aの遅い答え"), stale)

    async def test_stop_does_not_depend_on_the_llm(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("A")
        level = memory.remember(run, level_event())
        speaker.start_reply("mpf_buf は？")

        await feed(conductor, ListenButtonChanged(True))
        await feed(conductor, UserStartedSpeaking(), UserSaidText("もういい"))

        self.assertEqual(conductor.pool.cancelled, 1)
        self.assertEqual(level.state, SILENT)
        self.assertEqual(speaker.started[-1], ("notice", "調査を止めました。"))

    async def test_failure_retries_once_then_apologises(self):
        conductor = build()
        run = conductor.pool.start("A")
        run.attempts = 1

        await feed(conductor, ResearchFailed(run.run_id, "level_gap"))
        self.assertEqual(conductor.pool.retried, 1)
        self.assertEqual(conductor.speaker.started, [])

        await feed(conductor, ResearchFailed(run.run_id, "level_gap"))
        self.assertEqual(conductor.pool.retried, 1)
        self.assertEqual(conductor.speaker.started[-1][0], "notice")

    async def test_plan_preview_only_for_the_foreground_run(self):
        conductor = build()
        run = conductor.pool.start("A")
        conductor.pool.move_to_background(run)
        await feed(conductor, PlanReady(run.run_id, [{"objective": "役割の確認", "position": 1}]))
        self.assertEqual(conductor.speaker.started, [])

        other = conductor.pool.start("B")
        await feed(conductor, PlanReady(other.run_id, [{"objective": "役割の確認", "position": 1}]))
        self.assertEqual(conductor.speaker.started[-1][0], "report")

    async def test_a_finished_report_does_not_reopen_attention(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("A")
        memory.remember(run, level_event())
        await feed(conductor, IdleTick())
        await feed(conductor, speaker.finish())

        # the report is output; it must not start listening on its own
        self.assertEqual(conductor.attention.state, DORMANT)
        self.assertFalse(conductor.attention.accept("それ詳しく").accepted)

    async def test_screen_mirrors_raw_frames(self):
        conductor = build()
        from app.core.events import ResearchProgress

        await feed(conductor, ResearchProgress("r1", {"type": "level_start", "level_id": "l1"}))
        self.assertEqual(conductor.screen.frames[-1]["type"], "level_start")

    async def test_pressing_the_orb_off_stops_listening_even_mid_run(self):
        conductor = build()
        conductor.pool.start("A")
        await feed(conductor, ListenButtonChanged(True))
        self.assertEqual(conductor.attention.state, OPEN)
        await feed(conductor, ListenButtonChanged(False))
        self.assertEqual(conductor.attention.state, DORMANT)

    async def test_a_level_that_adds_nothing_is_never_spoken(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("mpf_buf とは")

        first = memory.remember(run, level_event("l1", "バッファの答え", 1, "役割の確認"))
        await feed(conductor, IdleTick())
        # the first answer to a question is owed to the user whatever it contains
        self.assertNotIn("何も話さないでください", speaker.started[-1][1])
        await feed(conductor, speaker.finish("バッファの役割はこうです。"))
        self.assertEqual(first.state, REPORTED)
        self.assertEqual(memory.reports_delivered, 1)

        extra = memory.remember(run, level_event("l2", "周辺の実装例", 2, "関連機能の整理"))
        await feed(conductor, IdleTick())
        prompt = speaker.started[-1][1]
        self.assertIn("何も話さないでください", prompt)
        self.assertIn("バッファの役割はこうです。", prompt)  # judged against what was said
        await feed(conductor, speaker.finish(""))  # the pass declines to speak it

        self.assertEqual(extra.state, SILENT)
        self.assertEqual(memory.reports_delivered, 1)  # nothing heard, nothing aged
        self.assertIs(memory.find("関連機能の整理"), extra)  # still readable later

    async def test_a_repeat_speaks_even_though_it_repeats(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("mpf_buf とは")
        memory.remember(run, level_event("l1", "役割の答え", 1, "役割の確認"))
        await feed(conductor, IdleTick())
        await feed(conductor, speaker.finish("役割はこうです。"))
        memory.remember(run, level_event("l2", "引数の答え", 2, "引数の意味"))
        await feed(conductor, IdleTick())
        await feed(conductor, speaker.finish("引数はこうです。"))

        await feed(conductor, ListenButtonChanged(True))
        await feed(conductor, UserSaidText("もう一度言って"))
        self.assertNotIn("何も話さないでください", speaker.started[-1][1])

    async def test_the_live_plan_is_what_a_follow_up_waits_on(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("mpf_buf とは")
        plan = [
            {"id": "l1", "objective": "役割の確認", "position": 1},
            {"id": "l2", "objective": "引数の意味", "position": 2},
            {"id": "l3", "objective": "戻り値の確認", "position": 3},
        ]
        await feed(conductor, PlanReady(run.run_id, plan))
        self.assertEqual(
            [entry.objective for entry in memory.pending],
            ["役割の確認", "引数の意味", "戻り値の確認"],
        )
        await feed(conductor, speaker.finish("これから確認します。"))  # the plan preview

        await feed(conductor, LevelReady(run.run_id, level_event("l1", "役割の答え", 1, "役割の確認")))
        self.assertEqual([entry.objective for entry in memory.pending], ["引数の意味", "戻り値の確認"])
        await feed(conductor, speaker.finish("役割はこうです。"))

        # a follow-up about a level that has not arrived waits instead of researching
        self.assertIn("調査中", read_retained(memory, "引数の意味"))

        await feed(conductor, LevelReady(run.run_id, level_event("l2", "引数の答え", 2, "引数の意味")))
        # it arrives asked for, so this one may not be dropped as redundant
        self.assertNotIn("何も話さないでください", speaker.started[-1][1])
        await feed(conductor, speaker.finish("引数はこうです。"))

        # once the run is over, what never arrived is not coming: research again
        await feed(conductor, ResearchFinished(run.run_id, "complete"))
        self.assertEqual(memory.pending, ())
        self.assertEqual(read_retained(memory, "戻り値の確認"), NO_RETAINED_RESULT)

    async def test_levels_are_never_dropped_while_the_user_speaks(self):
        conductor = build()
        run = conductor.pool.start("A")
        conductor.user_is_speaking = True
        level = conductor.memory.remember(run, level_event())
        await feed(conductor, LevelReady(run.run_id, level_event()))
        self.assertEqual(conductor.speaker.started, [])
        self.assertEqual(level.state, NEW)


if __name__ == "__main__":
    unittest.main()
