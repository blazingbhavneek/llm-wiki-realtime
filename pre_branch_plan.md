# Pre-branch fixes: stop the app being awkward

Applied on `realtime-asr-and-orb-attention` **before** cutting the `shallow-only`
and `deep-silent` branches from `plan.md`, so both inherit them.

None of this changes what the research pipeline does — it only stops the app
talking when it has nothing to say, and makes the panel show the run that is
actually happening.

| # | symptom | cause |
|---|---|---|
| P1 | 「もしもし！」 on every page load | `entrypoint.py:118` queues `prompts.GREETING`, and `/token` mints a fresh room per load |
| P2 | same fixed phrase, then 「XYZを調べます」 again | up to three separate speech turns per question (below) |
| P3 | second research does not replace the first in the panel | nothing in the mirrored frames identifies which run they belong to, and every run reuses the same level ids |

---

## P1 — no greeting on connect

`app/runtime/entrypoint.py:118-119`:

```python
    # Silent on connect. A page reload mints a fresh room (see web/http.py),
    # so this fired on every reload and made debugging noisy. Uncomment both
    # lines to bring the greeting back.
    # conductor.queue_notice(prompts.GREETING)
    # inbox.put_nowait(IdleTick())  # kick the ladder so the greeting plays now
```

`prompts.GREETING` stays defined — unused, but it is the text to restore.
`prompts` and `IdleTick` are still imported for other uses in this file, so no
import churn. Nothing else waits on that first `IdleTick`: `idle_ticker` posts
one every second anyway.

While here, correct the now-false claim in `app/web/http.py:56` — the docstring
says a fresh room exists so each session gets "its own agent greeting". Keep the
fresh-room policy (it is what guarantees a fresh dispatch); just drop the
greeting half of the sentence.

**Verify:** load the page — silence. The orb still works, the panel still says
「調査はまだ実行されていません」.

---

## P2 — one announcement per question, not three

A single question can currently produce three spoken turns before any answer:

1. **the model's own preamble** — 「XYZについて調べますね」. Emitted *before* the
   `research_wiki` tool call, inside the same generation, so it has already
   reached TTS by the time the tool raises `StopResponse()`
   (`app/agent/tools.py:66-76`). The `StopResponse` stops what comes *after*
   the tool call, never the preamble in front of it.
2. **the fixed notice** — `NOTICE_RESEARCHING` 「社内ウィキを調べますので、少々お待ちください」,
   queued unconditionally by `conductor.start_research` (`conductor.py:304`).
   Same string every time — the "always says the same phrase" complaint.
3. **the plan preview** — an LLM pass over the plan queued by the `PlanReady`
   branch (`conductor.py:186-193`), which says a second time what is about to be
   researched.

Turns 2 and 3 are ours and can be removed outright. Turn 1 belongs to the LLM
and cannot be suppressed reliably from a prompt — which is exactly why the
`StopResponse` hack exists.

### P2a — delete the plan preview (do this regardless)

`app/core/conductor.py:169-194`. Comment out only the `self.pending.append(...)`
block; **keep `self.memory.note_plan(run, event.planned_levels)`** — without it
`read_result` can no longer answer "that is already being researched".

```python
        if isinstance(event, PlanReady):
            run = self.pool.get(event.run_id)
            if run is None:
                return
            self.memory.note_plan(run, event.planned_levels)
            # The preview said a second time what the model had already said
            # before the tool call. Uncomment to restore.
            # if run.focus == FOREGROUND and event.planned_levels:
            #     self.pending.append(
            #         Pending(
            #             "prompt",
            #             prompts.plan_preview_instructions(run.question, event.planned_levels),
            #             run.run_id,
            #         )
            #     )
            return
```

Saves one LLM round trip and one spoken sentence per question (~4-6 s off time
to the real answer). `plan_preview_instructions` stays in `prompts.py`, unused.

### P2b — measure before touching the notice

Whether turn 1 actually fires decides what to do with turn 2, and it is one run
to find out. Set `LIVEKIT_AGENT_LOG_LEVEL=DEBUG` in `.env` (livekit-agents logs
tool-call execution only at DEBUG — see `config.py:64-66`), ask one question, and
read the transcript in the 会話 tab:

- **preamble present** (an agent bubble before the notice) → take **P2c**: drop
  the notice, the model's own line is the announcement.
- **preamble absent** → take **P2d**: keep a notice, but stop it being the same
  sentence every time.

### P2c — if the model announces it itself: drop the notice

`app/core/conductor.py:304`:

```python
        # The model already says 「〜を調べます」 before the tool call, so this
        # was the second time the user heard it. Uncomment if a turn ever
        # reaches research with no spoken preamble at all.
        # self.pending.append(Pending("notice", prompts.NOTICE_RESEARCHING, run.run_id))
```

Result: exactly one announcement, in the model's own words, naming the actual
subject — which is what the fixed phrase was standing in for.

### P2d — if it does not: rotate a short notice

`app/agent/prompts.py:21`. Round-robin, not random, so it stays testable:

```python
# Spoken verbatim by Speaker.start_notice. Rotated because one fixed sentence
# on every single question reads as a machine stuck in a loop.
NOTICE_RESEARCHING_VARIANTS = (
    "調べます。",
    "確認します。",
    "少々お待ちください。",
)
# NOTICE_RESEARCHING = "社内ウィキを調べますので、少々お待ちください。"


def researching_notice(count: int) -> str:
    """``count`` is the number of runs started so far, so callers stay stateless."""
    return NOTICE_RESEARCHING_VARIANTS[count % len(NOTICE_RESEARCHING_VARIANTS)]
```

`conductor.start_research` then uses
`prompts.researching_notice(len(self.pool.runs))` — `pool.runs` is already the
run counter, so no new state.

**Do not do both P2c and P2d.** One announcement is the target.

---

## P3 — the panel keeps showing the previous research

Sidebar behaviour is deliberately left alone — this is only about the panel
following the run that is actually happening.

### Why this can happen at all

The mirrored frames carry **no run identity**. The backend stamps `run_id` onto
the `run` frame and onto `cancelled` (`llm-wiki-dist/app.py:1838`, `:1855`) and
onto nothing else: `plan`, `plan_update`, `level_start`, `level`, `done` and
`error` all go out bare (`realtime.py:_level_event`, and the `plan` emit in
`run`). The Conductor forwards `event.frame` verbatim (`conductor.py:157-161`).

And the ids **inside** those frames are the same for every run: `_plan`
(`realtime.py:~905`) names its stages `level_1`, `level_2`, `level_3`, always.

So the reducer's two guards, which read as per-run, are in fact global
(`frontend/src/lib/research.js`):

- `case 'level'`: `if (state.results.has(event.level_id)) return state`
- `case 'plan'`: `if (event.version < state.planVersion) return state`

Exactly one thing resets that state: the synthetic `ask` frame published by
`conductor.start_research` (`conductor.py:307`). Miss that one frame and run 2's
`level_1` is discarded as a duplicate of run 1's, and run 2's plan v1 loses to
run 1's v2 — the panel goes on showing the old research while the voice answers
the new question. That is the failure.

### The three ways the reset gets missed, ranked

1. **A retry.** `RAG_STREAM_MAX_RETRIES=1` with a 10 s plan watchdog
   (`.env:117-118`). On `plan_timeout` or `level_gap` the Conductor calls
   `pool.retry(run)` (`conductor.py:207-210`), which reopens the stream under a
   new *backend* run id but the same *local* one — **and publishes no `ask`**.
   The backend then replays plan v1 and `level_1`, both of which are rejected.
   *Fits if the panel freezes mid-run and never recovers.*
2. **A dropped frame.** `Screen._publish` swallows every exception
   (`screen.py:47-48`). A `level` frame carries the full narration plus every
   fact, and LiveKit's reliable data channel caps a packet at roughly 15 KB; an
   oversize one fails silently. *Fits if the question and plan update but the
   level bodies never fill in.*
3. **There was no second run.** The model answered from `read_result` rather
   than `research_wiki` — which `ASSISTANT_INSTRUCTIONS` explicitly routes
   follow-ups to. No `ResearchRequested`, no `ask`, and the panel is correctly
   still showing the last real run. *Fits if the answer came with no
   「調べます」 preamble and the sidebar never even flickered.*

### Two minutes to tell them apart

Make the swallowed failure audible first — one line, and the debugging aid is
worth having regardless. `app/core/screen.py:44-48`:

```python
    async def _publish(self, data: bytes, topic: str) -> None:
        try:
            await self.room.local_participant.publish_data(data, reliable=True, topic=topic)
        except Exception as exc:
            # Was a bare `pass`, which made a frame the browser never received
            # indistinguishable from one it received and ignored.
            print(f"[SCREEN DROP] topic={topic} bytes={len(data)} {exc!r}", flush=True)
```

Then ask two research questions in one session. A `SCREEN DROP` line names cause
2; a `ResearchFailed` / retry in the log names cause 1; neither, and no second
`ask` reaching the browser, names cause 3.

### The fix that closes all three

Stamp the local run id on every mirrored frame and let the reducer refuse
anything from another run. The guards then become per-run by construction, and
no single missed frame can corrupt the panel again.

**`app/core/conductor.py`** — both publish sites. `run.run_id` is the local id,
which survives a retry:

```python
        if isinstance(event, ResearchProgress):
            run = self.pool.get(event.run_id)
            if run is not None and run.focus == FOREGROUND:
                # Stamped, because the backend's own frames carry no run id and
                # every run reuses level_1/level_2/level_3.
                self.screen.publish_research({**event.frame, "agent_run_id": run.run_id})
            return
```

```python
        self.screen.publish_research(
            {"type": "ask", "question": question, "agent_run_id": run.run_id}
        )
```

and the retry branch republishes `ask`, which is the reset the panel never got
(`conductor.py:207-210`):

```python
            if run is not None and self.pool.can_retry(run):
                self.pool.retry(run)
                # The new attempt replays plan v1 and the same level ids; without
                # this the panel goes on rejecting them as stale.
                self.screen.publish_research(
                    {"type": "ask", "question": run.question, "agent_run_id": run.run_id}
                )
                return
```

**`frontend/src/lib/research.js`** — `emptyResearch()` gains `agentRunId: null`
(and `activeId: null`, the one key the shape never declared although three cases
read it), plus one guard above the switch:

```js
export function researchReducer(state, event) {
  // `ask` is the only frame allowed to change runs. Anything else carrying a
  // different run id is a straggler from a superseded or retried attempt, and
  // its level ids and plan version would be matched against the wrong state.
  const runId = event.agent_run_id
  if (runId && state.agentRunId && runId !== state.agentRunId && event.type !== 'ask') {
    return state
  }

  switch (event.type) {
    case 'ask': {
      const next = emptyResearch()
      next.agentRunId = runId ?? null
      next.question = event.question
      next.status = 'planning'
      next.startedAt = performance.now()
      return next
    }
    ...
```

**`frontend/src/App.jsx`** — where `ResearchPanel` is rendered (`App.jsx:~562`):

```jsx
            : <ResearchPanel key={research.agentRunId ?? 'idle'} research={research} />}
```

`ResearchPanel` holds an `overrides` Map of levels the user collapsed, keyed by
bare `level.id` (`ResearchPanel.jsx:118-124`) — the same reused ids again, so a
level collapsed during run 1 renders collapsed in run 2. Remounting on a run
change drops that map, with no change inside the component.

---

## Order and verification

1. **P1**, **P2a**, and the `[SCREEN DROP]` log line — unambiguous wins, and the
   log line is what makes the rest diagnosable.
2. **P3**'s stamp-and-guard fix. It is worth applying whichever of the three
   causes turns out to be yours: it closes all three, and the retry republish is
   the only one of them that has no other fix.
3. One run with `LIVEKIT_AGENT_LOG_LEVEL=DEBUG` to settle **P2b**, then whichever
   of **P2c** / **P2d** it points at.

```bash
uv run pytest tests/test_conductor.py tests/test_memory.py -q
cd frontend && npm run build      # http.py serves frontend/dist, not the sources
```

P2a breaks two tests, which should be skipped rather than deleted:
`test_plan_preview_only_for_the_foreground_run` and
`test_queued_background_plan_preview_is_discarded` (`tests/test_conductor.py`).

Manual smoke, no e2e rig: `uv run python -m app.rag.placeholder` as the backend,
then **two** research questions in one session. Expect — silence on load, **one**
announcement per question, and the panel showing the second question with only
the second run's levels under it.

`tests/test_conductor.py` already covers the reset
(`test_new_research_resets_the_sidebar_to_its_question`, ~line 170) and the
suppression of background frames just above it; both assert on frame contents,
so adding `agent_run_id` will need those two expectations updated.

## Effect on `plan.md`

P2a is the same edit as that document's **A2** and **B6**. Once it lands here,
both branches inherit it: A2 becomes "already done", B6 stops being a required
step. Nothing else here collides — P1 and P3 are untouched by either mode.
