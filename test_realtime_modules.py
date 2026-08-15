"""Unit tests for everything that does not need LiveKit, TTS, or the network."""

from __future__ import annotations

import asyncio
import unittest

from attention import DORMANT, OPEN, Attention
from conductor import Conductor
from events import (
    BACKGROUND,
    FOREGROUND,
    IdleTick,
    LevelReady,
    ListenButtonChanged,
    PlanReady,
    ResearchFailed,
    SpeechFinished,
    SpeechInterrupted,
    UserSaidText,
    UserStartedSpeaking,
)
from memory import NEW, PARTIAL, REPORTED, SILENT, Memory
from research import ResearchPool, ResearchRun, iter_sse_events


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def level_event(level_id="level-1", text="答えです。", position=1, objective="設定の差分"):
    return {
        "type": "level",
        "level_id": level_id,
        "position": position,
        "objective": objective,
        "text": text,
        "complete": True,
    }


class FakeSpeaker:
    def __init__(self, inbox):
        self.inbox = inbox
        self.ducked = False
        self.started: list[tuple[str, str]] = []
        self.interrupts = 0
        self._current: str | None = None
        self._next_id = 0

    @property
    def busy(self):
        return self._current is not None

    def _start(self, kind, text):
        self._next_id += 1
        speech_id = f"speech-{self._next_id}"
        self._current = speech_id
        self.started.append((kind, text))
        return speech_id

    def start_reply(self, user_text, *, context=""):
        return self._start("reply", user_text)

    def start_report(self, prompt):
        return self._start("report", prompt)

    def start_notice(self, text):
        return self._start("notice", text)

    def interrupt(self):
        self.interrupts += 1
        self._current = None

    def on_speech_ended(self, speech_id):
        if self._current == speech_id:
            self._current = None

    def duck(self):
        self.ducked = True

    def unduck(self):
        self.ducked = False

    # test helpers
    def finish(self, speech_id=None):
        speech_id = speech_id or self._current
        self._current = None
        return SpeechFinished(speech_id)

    def cut(self, spoken, speech_id=None):
        speech_id = speech_id or self._current
        self._current = None
        return SpeechInterrupted(speech_id, spoken)


class FakeScreen:
    def __init__(self):
        self.frames = []
        self.ducked = None
        self.attention = None

    def publish_research(self, frame):
        self.frames.append(frame)

    def set_ducked(self, ducked):
        self.ducked = ducked

    def set_attention(self, state):
        self.attention = state


class FakePool(ResearchPool):
    """A pool that never opens a stream."""

    def __init__(self, inbox):
        super().__init__(inbox)
        self.cancelled = 0
        self.retried = 0

    def start(self, question):
        run = ResearchRun(f"run-{len(self.runs) + 1}", question, self.inbox)
        self.runs[run.run_id] = run
        return run

    def retry(self, run):
        self.retried += 1
        run.attempts += 1

    async def cancel_all(self):
        self.cancelled += 1
        for run in self.runs.values():
            run.state = "cancelled"


def build():
    inbox: asyncio.Queue = asyncio.Queue()
    memory = Memory()
    speaker = FakeSpeaker(inbox)
    pool = FakePool(inbox)
    screen = FakeScreen()
    conductor = Conductor(
        inbox=inbox,
        attention=Attention(),
        speaker=speaker,
        pool=pool,
        memory=memory,
        screen=screen,
    )
    return conductor


async def feed(conductor, *events):
    for event in events:
        await conductor.handle(event)
        await conductor.speak_next()


# ---------------------------------------------------------------------------
# attention
# ---------------------------------------------------------------------------


class AttentionTests(unittest.TestCase):
    def test_orb_off_ignores_speech(self):
        attention = Attention()
        turn = attention.accept("隣の人との会話", now=1)
        self.assertFalse(turn.accepted)
        self.assertEqual(attention.state, DORMANT)

    def test_orb_on_accepts_speech_verbatim(self):
        attention = Attention()
        attention.set_button_held(True)
        attention.open("button", now=0)
        turn = attention.accept("mpf_mfs_open って何？", now=2)
        self.assertTrue(turn.accepted)
        # nothing is stripped now that there is no wake word to remove
        self.assertEqual(turn.text, "mpf_mfs_open って何？")
        self.assertTrue(attention.accept("引数も教えて", now=3).accepted)

    def test_orb_stays_open_until_pressed_again(self):
        attention = Attention()
        attention.set_button_held(True)
        attention.open("button", now=0)
        # no idle timeout: a long silence must not close it
        self.assertTrue(attention.accept("まだ聞いてる？", now=10_000).accepted)
        self.assertEqual(attention.state, OPEN)

        attention.set_button_held(False)
        attention.close()
        self.assertEqual(attention.state, DORMANT)
        self.assertFalse(attention.accept("ただの雑談", now=10_001).accepted)

    def test_typed_text_is_a_turn_even_when_dormant(self):
        attention = Attention()
        turn = attention.accept("mpf_buf は？", from_text_input=True, now=1)
        self.assertTrue(turn.accepted)

    def test_commands(self):
        classify = Attention.classify
        self.assertEqual(classify("もういい"), "stop")
        self.assertEqual(classify("やめて"), "stop")
        self.assertEqual(classify("もう一回言って"), "repeat")
        self.assertEqual(classify("続けて"), "continue")
        self.assertEqual(classify("ありがとう"), "close")
        self.assertEqual(classify("mpf_buf は？"), "none")


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


# ---------------------------------------------------------------------------
# sse transport
# ---------------------------------------------------------------------------


class AsyncLines:
    def __init__(self, lines):
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line


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
        conductor.start_research("B")  # A becomes background
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
        conductor.start_research("B")
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
        from events import ResearchProgress

        await feed(conductor, ResearchProgress("r1", {"type": "level_start", "level_id": "l1"}))
        self.assertEqual(conductor.screen.frames[-1]["type"], "level_start")

    async def test_pressing_the_orb_off_stops_listening_even_mid_run(self):
        conductor = build()
        conductor.pool.start("A")
        await feed(conductor, ListenButtonChanged(True))
        self.assertEqual(conductor.attention.state, OPEN)
        await feed(conductor, ListenButtonChanged(False))
        self.assertEqual(conductor.attention.state, DORMANT)

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
