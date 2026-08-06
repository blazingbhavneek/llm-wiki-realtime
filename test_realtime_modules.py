import asyncio
import unittest

from attention import AttentionFSM, AttentionState
from delivery import DeliveryQueue
from rag_client import iter_sse_events
from scheduler import EtaPredictor, SpeechScheduler


class AsyncLines:
    def __init__(self, lines):
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line


class RagClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_parser_ignores_comments_and_flushes_at_eof(self):
        lines = AsyncLines([
            ": connected",
            "",
            "event: run",
            'data: {"run_id":"r1"}',
            "",
            ": ping",
            "event: done",
            'data: {"type":"done",',
            'data: "status":"complete"}',
        ])
        events = [event async for event in iter_sse_events(lines)]
        self.assertEqual(events[0], {"type": "run", "run_id": "r1"})
        self.assertEqual(events[1]["status"], "complete")


def level_event(level_id="level-1", text="答えです。", **overrides):
    event = {
        "type": "level",
        "level_id": level_id,
        "position": 1,
        "objective": "設定の差分",
        "text": text,
        "facts": [],
        "reference_node_ids": [],
        "complete": True,
        "latency_ms": 1200,
        "queries": [{"enough": True, "search_result_count": 4}],
    }
    event.update(overrides)
    return event


class DeliveryTests(unittest.TestCase):
    def test_deduplicates_replayed_level_and_resumes_from_offset(self):
        queue = DeliveryQueue()
        chunk = queue.enqueue_event("run-1", level_event())
        self.assertIsNotNone(chunk)
        self.assertIsNone(queue.enqueue_event("run-2", level_event()))
        queue.mark_speaking(chunk)
        queue.mark_interrupted(chunk, 3)
        self.assertEqual(chunk.remaining_text, "す。")
        self.assertIs(queue.resume_interrupted(), chunk)
        self.assertEqual(queue.next_pending(), chunk)

    def test_empty_levels_are_not_enqueued_and_partial_levels_hedge(self):
        queue = DeliveryQueue()
        self.assertIsNone(queue.enqueue_event("r", level_event(text="")))
        chunk = queue.enqueue_event("r", level_event(complete=False))
        self.assertTrue(chunk.should_hedge)

    def test_old_pending_results_become_stale_but_remain_readable(self):
        queue = DeliveryQueue()
        chunk = queue.enqueue_event("r", level_event())
        for _ in range(3):
            queue.advance_turn()
        self.assertEqual(chunk.state, "stale")
        self.assertEqual(queue.read_result("設定の差分"), "答えです。")


class AttentionTests(unittest.TestCase):
    def test_wake_word_opens_followup_window(self):
        fsm = AttentionFSM(idle_seconds=20)
        ignored = fsm.evaluate("隣の人との会話", now=1)
        self.assertFalse(ignored.accepted)
        opened = fsm.evaluate("モーヴィ、CIを調べて", now=2)
        self.assertTrue(opened.accepted)
        self.assertEqual(opened.text, "CIを調べて")
        followup = fsm.evaluate("依存関係も見て", now=3)
        self.assertTrue(followup.accepted)

    def test_research_prevents_idle_close(self):
        fsm = AttentionFSM(idle_seconds=20)
        fsm.evaluate("モーヴィ", now=1)
        fsm.research_started()
        self.assertTrue(fsm.evaluate("続き", now=100).accepted)
        fsm.research_finished()
        self.assertFalse(fsm.evaluate("ただの会話", now=121).accepted)
        self.assertEqual(fsm.state, AttentionState.DORMANT)


class EtaTests(unittest.TestCase):
    def test_predictor_uses_median_and_thresholds(self):
        eta = EtaPredictor()
        self.assertEqual(eta.estimate_ms, 2500)
        eta.level_completed("a", 1000)
        eta.level_completed("b", 3000)
        eta.level_completed("c", 2000)
        self.assertEqual(eta.estimate_ms, 2000)
        self.assertEqual(eta.strategy(2500), "seamless")
        self.assertEqual(eta.strategy(1700), "stretch")
        self.assertEqual(eta.strategy(1000), "bridge")


class FakeHandle:
    def __init__(self):
        self.done = asyncio.Event()
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True
        self.done.set()

    def __await__(self):
        return self.done.wait().__await__()


class FakeSession:
    def __init__(self):
        self.spoken = []
        self.handles = []

    def say(self, text, **_kwargs):
        self.spoken.append(text)
        handle = FakeHandle()
        self.handles.append(handle)
        asyncio.get_running_loop().call_soon(handle.done.set)
        return handle


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_drains_chunk_and_marks_it_spoken(self):
        queue = DeliveryQueue()
        chunk = queue.enqueue_event("r", level_event())
        session = FakeSession()
        scheduler = SpeechScheduler(session, queue)
        scheduler.start()
        scheduler.notify()
        for _ in range(10):
            if chunk.state == "spoken":
                break
            await asyncio.sleep(0)
        self.assertEqual(chunk.state, "spoken")
        self.assertEqual(session.spoken, ["答えです。"])
        await scheduler.close()


if __name__ == "__main__":
    unittest.main()
