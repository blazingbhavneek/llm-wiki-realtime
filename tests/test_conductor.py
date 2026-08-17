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
    PlanRevised,
    ResearchFailed,
    ResearchFinished,
    ResearchProgress,
    SpeechInterrupted,
    UserSaidText,
    UserStartedSpeaking,
)
from app.core.conductor import Pending
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

    async def test_background_partial_is_not_resumed_without_an_explicit_request(self):
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
        self.assertEqual(old.state, PARTIAL)
        self.assertEqual(len(speaker.started), 2)

    async def test_background_finding_is_retained_but_not_spoken(self):
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
        self.assertEqual(stale.state, NEW)
        self.assertEqual(len(speaker.started), 5)
        # Not spoken is not deletion: the user can explicitly ask for it.
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

    async def test_a_retry_republishes_the_ask_frame_with_the_run_id(self):
        conductor = build()
        run = conductor.pool.start("A")
        run.attempts = 1

        await feed(conductor, ResearchFailed(run.run_id, "level_gap"))
        # The retry reopens the stream under the same local run id but the
        # backend replays plan v1 and level_1; without a republished ask the
        # panel goes on rejecting both as stale.
        self.assertEqual(
            conductor.screen.frames[-1],
            {"type": "ask", "question": "A", "agent_run_id": run.run_id},
        )

    async def test_a_background_runs_retry_does_not_pull_the_panel_back(self):
        conductor = build()
        old = conductor.pool.start("古い質問")
        conductor.start_research("新しい質問")  # supersedes: old -> background
        conductor.screen.frames.clear()

        await feed(conductor, ResearchFailed(old.run_id, "level_gap"))

        # It is still retried - a superseded run is never cancelled - but the
        # panel belongs to the newest question, so the republished ask that a
        # foreground retry owes the panel must not be sent on its behalf.
        self.assertEqual(conductor.pool.retried, 1)
        self.assertEqual(conductor.screen.frames, [])

    @unittest.skip("plan preview removed; see pre_branch_plan.md P2a")
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

    async def test_screen_mirrors_only_foreground_frames(self):
        conductor = build()
        foreground = conductor.pool.start("新しい質問")
        background = conductor.pool.start("古い質問")
        conductor.pool.move_to_background(background)
        await feed(conductor, ResearchProgress(background.run_id, {"type": "done"}))
        self.assertEqual(conductor.screen.frames, [])
        await feed(conductor, ResearchProgress(foreground.run_id, {"type": "level_start", "level_id": "l1"}))
        # Stamped with the foreground run's own id, not the backend's reused
        # level_1/level_2/level_3 -- that stamp is what the reducer keys on.
        self.assertEqual(
            conductor.screen.frames[-1],
            {"type": "level_start", "level_id": "l1", "agent_run_id": foreground.run_id},
        )

    async def test_research_progress_frames_carry_the_run_id(self):
        conductor = build()
        run = conductor.pool.start("A")
        await feed(conductor, ResearchProgress(run.run_id, {"type": "plan", "version": 1}))
        # The backend's own frame carries no run id; the Conductor stamps one
        # on so the reducer can tell two runs' level_1 apart.
        self.assertEqual(conductor.screen.frames[-1]["agent_run_id"], run.run_id)
        self.assertEqual(conductor.screen.frames[-1]["type"], "plan")

    async def test_new_research_resets_the_sidebar_to_its_question(self):
        conductor = build()
        conductor.start_research("最新の質問")
        run = conductor.pool.foreground_run()
        self.assertEqual(
            conductor.screen.frames[-1],
            {"type": "ask", "question": "最新の質問", "agent_run_id": run.run_id},
        )

    @unittest.skip("plan preview removed; see pre_branch_plan.md P2a")
    async def test_queued_background_plan_preview_is_discarded(self):
        conductor = build()
        old = conductor.pool.start("古い質問")
        conductor.pending.append(Pending("prompt", "古い質問の計画", old.run_id))
        conductor.start_research("最新の質問")
        # The latest run's notice is still valid; once it finishes, the
        # queued old plan must be dropped instead of reaching TTS.
        await feed(conductor, conductor.speaker.finish())
        self.assertEqual(len(conductor.speaker.started), 1)
        self.assertFalse(conductor.pending)

    async def test_background_failure_is_not_announced(self):
        conductor = build()
        old = conductor.pool.start("古い質問")
        conductor.start_research("最新の質問")
        conductor.pending.clear()
        old.attempts = 99
        await feed(conductor, ResearchFailed(old.run_id, "stream_error"))
        self.assertEqual(conductor.speaker.started, [])

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

    async def test_a_no_information_level_is_silent_after_an_answer(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("巡回編成ファイルのレコードサイズ")

        first = memory.remember(run, level_event("l1", "九十二バイトより大きくします。"))
        await feed(conductor, IdleTick())
        await feed(conductor, speaker.finish("九十二バイトより大きくします。"))

        empty = memory.remember(
            run,
            level_event(
                "l2",
                "この領域では具体的な情報は見つかりませんでした。",
                objective="追加の確認",
            ),
        )
        await feed(conductor, IdleTick())

        self.assertEqual(first.state, REPORTED)
        self.assertEqual(empty.state, SILENT)
        self.assertEqual(len(speaker.started), 1)
        self.assertIs(memory.find("追加の確認"), empty)

    async def test_a_first_no_information_level_is_still_reported(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("見つかった情報はありますか")

        level = memory.remember(
            run,
            level_event("l1", "この領域では具体的な情報は見つかりませんでした。"),
        )
        await feed(conductor, IdleTick())

        self.assertEqual(level.state, "reporting")
        self.assertEqual(speaker.started[-1][0], "report")

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

    async def test_a_plan_revision_is_tracked_but_never_re_announced(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("__syncthreads とメモリフェンスの違い")
        run.planned_levels = [
            {"id": "l1", "objective": "役割を確認する", "position": 1},
            {"id": "l2", "objective": "違いと使い分けを説明する", "position": 2},
        ]
        await feed(conductor, PlanReady(run.run_id, run.planned_levels))
        self.assertEqual(len(speaker.started), 0)  # the preview is gone; see P2a
        await feed(conductor, speaker.finish("まず役割から確認します。"))

        # the backend splits stage 2 in two, mid-run
        run.planned_levels = [
            {"id": "l1", "objective": "役割を確認する", "position": 1},
            {"id": "l2", "objective": "フェンス関数の種類を整理する", "position": 2},
            {"id": "l3", "objective": "それぞれの使用場面を説明する", "position": 3},
        ]
        await feed(conductor, PlanRevised(run.run_id, run.planned_levels))

        # bookkeeping followed the revision...
        self.assertEqual(
            [entry.objective for entry in memory.pending],
            ["役割を確認する", "フェンス関数の種類を整理する", "それぞれの使用場面を説明する"],
        )
        self.assertIn("調査中", read_retained(memory, "使用場面"))
        # ...and it cost no speech: a turn per plan tweak is the whole problem
        self.assertEqual(len(speaker.started), 0)

    async def test_a_grown_plan_reaches_the_user_through_the_hand_off(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("__syncthreads とメモリフェンスの違い")
        run.planned_levels = [
            {"id": "l1", "objective": "役割を確認する", "position": 1},
            {"id": "l2", "objective": "フェンス関数の種類を整理する", "position": 2},
            {"id": "l3", "objective": "それぞれの使用場面を説明する", "position": 3},
        ]
        memory.remember(run, level_event("l1", "役割の答え", 1, "役割を確認する"))
        await feed(conductor, IdleTick())

        prompt = speaker.started[-1][1]
        self.assertEqual(prompt.count("[説明の段階]\n1 / 3"), 1)
        # stage 2 is not the end, so the hand-off may not sound like one
        self.assertIn("次の観点のあとにも段階が残っています", prompt)

        await feed(conductor, speaker.finish("役割はこうです。続けて、使用場面を説明します。"))
        memory.remember(run, level_event("l3", "使用場面の答え", 3, "それぞれの使用場面を説明する"))
        await feed(conductor, IdleTick())
        # the last stage closes instead of promising a fourth
        self.assertIn("最後の段階では次の調査を予告せず", speaker.started[-1][1])

    async def test_two_reports_never_hand_off_with_the_same_sentence(self):
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("__syncthreads とメモリフェンスの違い")
        # the objectives of stage 2 and 3 read alike, which is exactly what a
        # mid-run plan split leaves behind
        run.planned_levels = [
            {"id": "l1", "objective": "違いを説明する", "position": 1},
            {"id": "l2", "objective": "使い分けの場面を整理する", "position": 2},
            {"id": "l3", "objective": "それぞれの使用場面を説明する", "position": 3},
        ]
        memory.remember(run, level_event("l1", "違いの答え", 1, "違いを説明する"))
        await feed(conductor, IdleTick())
        # nothing has been said yet, so there is no close to differ from
        self.assertNotIn("[直前の締めの一文]", speaker.started[-1][1])
        await feed(conductor, speaker.finish("違いはこうです。続けて、それぞれの具体的な使用場面について説明します。"))

        memory.remember(run, level_event("l2", "種類の答え", 2, "使い分けの場面を整理する"))
        await feed(conductor, IdleTick())
        prompt = speaker.started[-1][1]
        # the sentence it must not reach for again is quoted back on its own,
        # not merely buried in what was said
        self.assertIn(
            "[直前の締めの一文]\n続けて、それぞれの具体的な使用場面について説明します。",
            prompt,
        )
        self.assertIn("言い換えただけのほぼ同じ一文で締めてはいけません", prompt)
        # and when it genuinely cannot differ, the hand-off is dropped, not faked
        self.assertIn("次の話題へつなぐ一文自体を省き", prompt)

    async def test_a_cancelled_stage_is_never_announced_as_next(self):
        # The backend answered the whole question in stage one and dropped the
        # rest. Nothing announces that; the report simply stops handing off.
        conductor = build()
        speaker, memory = conductor.speaker, conductor.memory
        run = conductor.pool.start("実行構成の各引数は何を指定するか")
        run.planned_levels = [
            {"id": "l1", "objective": "各引数の指定内容を確認する", "position": 1, "status": "complete"},
            {"id": "l2", "objective": "資料の続きを読み詳細を補う", "position": 2, "status": "skipped"},
            {"id": "l3", "objective": "触れた用語を先回りして調べる", "position": 3, "status": "skipped"},
        ]
        await feed(conductor, PlanRevised(run.run_id, run.planned_levels))
        memory.remember(run, level_event("l1", "引数の答え", 1, "各引数の指定内容を確認する"))
        await feed(conductor, IdleTick())

        prompt = speaker.started[-1][1]
        self.assertEqual(prompt.count("[説明の段階]\n1 / 1"), 1)
        self.assertIn("最後の段階では次の調査を予告せず", prompt)
        # the dropped stage is never offered as the thing coming next
        self.assertNotIn("資料の続きを読み詳細を補う", prompt)

    @unittest.skip("plan preview removed; see pre_branch_plan.md P2a")
    async def test_a_one_stage_plan_is_previewed_without_promising_a_next_step(self):
        # The backend plans one stage when it expects to answer in one. Told to
        # say "what comes first and what comes next", the model fills the slot
        # with a step that never runs.
        conductor = build()
        run = conductor.pool.start("tex1DLayered の3番目の引数は何ですか")
        await feed(
            conductor,
            PlanReady(run.run_id, [{"id": "l1", "objective": "引数の指定内容", "position": 1}]),
        )

        prompt = conductor.speaker.started[-1][1]
        self.assertIn("このあとに別の段階が続くと受け取れる言い方は使わないでください", prompt)
        self.assertIn("手順を分けて説明しないでください", prompt)
        # the multi-stage phrasing, and its worked example, must be absent
        self.assertNotIn("まず何から確認し、次に何を見るかまで伝えれば十分です", prompt)
        self.assertNotIn("そのあと引数の意味を見ます", prompt)

    @unittest.skip("plan preview removed; see pre_branch_plan.md P2a")
    async def test_the_spoken_preview_does_not_commit_to_a_stage_count(self):
        conductor = build()
        run = conductor.pool.start("A")
        await feed(
            conductor,
            PlanReady(
                run.run_id,
                [
                    {"id": "l1", "objective": "役割の確認", "position": 1},
                    {"id": "l2", "objective": "使い分けの整理", "position": 2},
                ],
            ),
        )
        prompt = conductor.speaker.started[-1][1]
        # the plan below this sentence is provisional and the sentence is never
        # corrected, so it may not close the list
        self.assertIn("これで全部だと受け取れる言い方をしたりしないでください", prompt)
        self.assertIn("段階の数を言ったり", prompt)

    async def test_levels_are_never_dropped_while_the_user_speaks(self):
        conductor = build()
        run = conductor.pool.start("A")
        conductor.user_is_speaking = True
        level = conductor.memory.remember(run, level_event())
        await feed(conductor, LevelReady(run.run_id, level_event()))
        self.assertEqual(conductor.speaker.started, [])
        self.assertEqual(level.state, NEW)


class PlanFrameTests(unittest.IsolatedAsyncioTestCase):
    """The frame the backend really sends produces the event the table handles.

    Everything above posts `PlanRevised` by hand; this is the one place that
    checks a `plan_update` frame ever becomes one.
    """

    def _drain(self, inbox):
        events = []
        while not inbox.empty():
            events.append(inbox.get_nowait())
        return events

    async def test_a_plan_update_frame_becomes_a_plan_revision(self):
        import asyncio

        from app.rag.llm_wiki import ResearchRun

        inbox: asyncio.Queue = asyncio.Queue()
        progress: asyncio.Queue = asyncio.Queue()
        run = ResearchRun("r1", "質問", inbox)

        first = [{"id": "l1", "objective": "役割", "position": 1}]
        run._on_frame({"type": "plan", "version": 1, "levels": first}, progress)
        self.assertEqual([type(event) for event in self._drain(inbox)], [ResearchProgress, PlanReady])

        grown = first + [{"id": "l2", "objective": "使い分け", "position": 2}]
        run._on_frame({"type": "plan_update", "version": 2, "levels": grown}, progress)
        events = self._drain(inbox)
        self.assertEqual([type(event) for event in events], [ResearchProgress, PlanRevised])
        self.assertEqual(events[-1].planned_levels, grown)
        self.assertEqual(run.planned_levels, grown)

        # a stale re-broadcast changes nothing and is not worth an event: acting
        # on it would resurrect objectives version 2 already dropped
        run._on_frame({"type": "plan_update", "version": 1, "levels": first}, progress)
        self.assertEqual([type(event) for event in self._drain(inbox)], [ResearchProgress])
        self.assertEqual(run.planned_levels, grown)


if __name__ == "__main__":
    unittest.main()
