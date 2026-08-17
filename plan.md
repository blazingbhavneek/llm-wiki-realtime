# Quieting the assistant: two modes

Both modes branch from `f3dac8a` on `realtime-asr-and-orb-attention`.
Mode A = `shallow-only`. Mode B = `deep-silent`.

Everything below prefers commenting code out over deleting it, so either branch
reverts by uncommenting one or two lines.

---

## 1. What actually goes wrong today

The backend (`llm-wiki-dist/graph/realtime.py`) plans three fixed stages in
`_plan` (line ~900):

| # | kind | objective | what it does |
|---|---|---|---|
| 1 | `fast` | 直接答える | 4 parallel readers over the reranked top-30. **This is the answer the user wanted.** |
| 2 | `deep` | 資料の続きと関連ノードを読み、詳細を補う | 3-hop subgraph walk, up to 4 subagents + 1 reader + a compile pass |
| 3 | `anticipation` | 回答で触れた用語を先回りして調べる | looks up terms the answer mentioned but did not explain |

Stage 3 is, by design, "talk about theory adjacent to the answer". Stage 2 is
"read the rest of the document". Neither is bounded by what the user asked.

The run loop (`realtime.py:700-850`) emits `plan` carrying **only level 1**
(`levels = planned[:1]`). After level 1 completes it calls
`_needs_deeper_research` (`realtime.py:1506`) — one extra LLM call against
`SUFFICIENCY_SYSTEM_PROMPT`. That gate deliberately **fails open**: a timeout, an
error, or a verdict it cannot evidence all mean "dig deeper". So it almost always
emits `plan_update reason="deeper"`, appending stages 2 and 3 — and it emits that
**before** the level-1 frame.

On the client (`llm-wiki-realtime`) one question therefore produces four spoken
turns:

1. `NOTICE_RESEARCHING` — `conductor.start_research` (`conductor.py:298`)
2. a plan-preview LLM pass — `PlanReady` handler (`conductor.py:169`)
3. the level-1 report — `speak_next` → `report()` (`conductor.py:333`, `:373`)
4. the level-2 and level-3 reports — same path, no filter

**Where "tsuzukete" comes from:** `prompts._bridge_rules` (`prompts.py:150`) says
verbatim

> 内容を言い終えた後に、一度だけ自然に『続けて、引数の意味を確認します』のように次の話題へつないでください。

It fires whenever `next_objective` is non-empty. Because `plan_update` lands
before the level-1 frame, `run.planned_levels` already holds stages 2 and 3 by
the time level 1 is narrated — so level 1 promises a continuation, and then
stages 2 and 3 deliver the rant. One cause, both symptoms.

The client asks for all of this itself:
`app/rag/llm_wiki.py:38` — `VOICE_KNOBS["max_levels"] = 3`.

---

## 2. Mode A — branch `shallow-only`

**Goal:** answer once from the shallow pass, then stop talking.
**Scope:** client only. Zero backend edits. Three files touched, one of them
optional.

### A1 — ask the backend for one stage (the whole mode, really)

`app/rag/llm_wiki.py:38-46`

```python
VOICE_KNOBS: dict[str, int] = {
    # Mode A: 1 = fast answer only. The deep and anticipation stages are what
    # produced the 「続けて…」 rant; asking for them is what started it.
    # Restore `3` to bring them back.
    "max_levels": 1,
    # "max_levels": 3,
    ...
}
```

That one value cascades through code that already exists — no other edit is
needed to kill the continuation line:

- `_plan` slices `stages[:1]`, so `pending_stages` is empty.
- `if pending_stages:` is therefore false → `_needs_deeper_research` never runs.
  One LLM call saved per question.
- No `plan_update` is emitted → `run.planned_levels` stays at length 1.
- `conductor.position_of` returns `next_objective = ""` → `_bridge_rules` takes
  its first branch: 「最後の段階では次の調査を予告せず、質問への答えを短く締めてください。」
  **続けて dies for free.**
- `plan_preview_instructions` sees `len(planned_levels) <= 1` and takes its
  single-sentence shape, which already forbids 「まず」「そのあと」「続いて」.

### A2 — drop the plan preview (optional, recommended)

`app/core/conductor.py:169-194`, inside the `PlanReady` branch. Comment out only
the `self.pending.append(...)` block; **keep `self.memory.note_plan(run, ...)`**
— removing that breaks `read_result`'s "already being researched" answer.

```python
        if isinstance(event, PlanReady):
            run = self.pool.get(event.run_id)
            if run is None:
                return
            self.memory.note_plan(run, event.planned_levels)
            # Mode A: with one stage there is nothing worth previewing, and
            # NOTICE_RESEARCHING already covers the gap. Uncomment to restore.
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

Saves one LLM turn and one spoken sentence (~4-6 s to first real answer).

### A3 — shorten the holding notice (optional)

`app/agent/prompts.py:21`

```python
# NOTICE_RESEARCHING = "社内ウィキを調べますので、少々お待ちください。"
NOTICE_RESEARCHING = "調べます。"
```

### Resulting behaviour

「調べます」 → (silence) → one report → stop.

### Known cost

Open-ended questions ("which functions do X", "how do A and B differ") get only
what the shallow pass found, with no path to more except asking again. That is
the trade the mode buys.

### Tests

`uv run pytest tests/test_conductor.py tests/test_memory.py -q`

If A2 is taken, these two will fail and should be marked
`@unittest.skip("Mode A: plan preview removed")` rather than deleted:

- `test_plan_preview_only_for_the_foreground_run` (`tests/test_conductor.py:~137`)
- `test_queued_background_plan_preview_is_discarded` (`tests/test_conductor.py:~180`)

Smoke, no e2e: run `uv run python -m app.rag.placeholder` as the backend, ask one
question, count the spoken turns — expect exactly two (notice + report).

---

## 3. Mode B — branch `deep-silent`

**Goal:** report the shallow answer immediately, keep researching in silence,
never announce it, and at the end ask once whether the user wants the rest.
No 続けて anywhere.

**Scope:** client edits, plus **one shared backend patch** (section 4) that is
inert under Mode A — so both branches can run against the same deployed
`llm-wiki-dist`.

### B1 — knobs: drop the anticipation stage, shrink the deep budget

`app/rag/llm_wiki.py:38-46`. All of these are already accepted by
`RealtimeAskBody` (`llm-wiki-dist/app.py:1403-1470`), so none of them costs a
backend edit.

```python
VOICE_KNOBS: dict[str, int] = {
    # Mode B: 2 = fast + deep. Stage 3 (anticipation) is "look up the terms the
    # answer used but did not explain" — the off-topic rant, dropped outright.
    "max_levels": 2,
    # Deep stage, tightened so it lands well inside the client's 150 s
    # RAG_LEVEL_TIMEOUT_SECONDS watchdog. Backend defaults are 4 / 5 / 30 / 120.
    "subagent_count": 2,
    "subagent_max_steps": 3,
    "subagent_compile_wait_seconds": 15,
    "deep_deadline_seconds": 45,
    ...
}
```

### B2 — speak the first level only; retain the rest silently

`app/core/conductor.py:196-200`, the `LevelReady` branch. `Memory.remember`
already returns the stored `LevelResult` (or `None` for a duplicate), and
`next_new` only ever picks `state == NEW`, so marking later levels SILENT keeps
them out of the speech ladder with no change to `speak_next` or `memory.py`.

```python
        if isinstance(event, LevelReady):
            run = self.pool.get(event.run_id)
            if run is not None:
                level = self.memory.remember(run, event.level)
                # Mode B: only the first level of a run is spoken. Everything
                # after it is retained (read_result still finds it) and offered
                # once at the end instead of narrated unprompted.
                if level is not None and self.first_level_done(run.run_id):
                    self.memory.mark_silent(level)
            return
```

plus one small helper next to `position_of`:

```python
    def first_level_done(self, run_id: str) -> bool:
        """True once this run has already produced a level before this one."""
        return sum(1 for level in self.memory.levels if level.run_id == run_id) > 1
```

SILENT levels stay reachable through `Memory.find`, so the existing
`read_result` tool can already read them — that is the LLM half of the yes-path
in B5.

### B3 — never promise a next stage

`app/core/conductor.py:374-375`, top of `report()`. The deep stage is still in
`run.planned_levels`, so `position_of` would hand the report a `next_objective`
and `_bridge_rules` would emit 続けて again. Two lines close that door without
touching `prompts.py`:

```python
        step, step_count, _next_objective = self.position_of(level)
        # Mode B: the deep stage is silent, so nothing may be promised out loud.
        # Blanking these puts _bridge_rules on its "close it short" branch and
        # stops "[説明の段階] 1 / 2" from implying a part two.
        next_objective = ""
        step_count = step
```

### B4 — offer once, at the end, only if there is something to offer

`app/agent/prompts.py`, new constant beside the other notices:

```python
NOTICE_DEEPER_AVAILABLE = "さらに詳しい内容も調べてあります。お聴きになりますか？"
```

`app/core/conductor.py:215-221`, the `ResearchFinished` branch. The
`_is_no_information_result` helper already at the top of this file is what makes
"nothing was missing" resolve to silence rather than a pointless question:

```python
        if isinstance(event, ResearchFinished):
            self.memory.close_plan(event.run_id)
            # Mode B: the deep stage ran silently. Offer it only if it actually
            # holds something — an empty or boilerplate deep level says nothing,
            # so we say nothing.
            run = self.pool.get(event.run_id)
            if run is not None and run.focus == FOREGROUND and self.has_offerable(event.run_id):
                self.offered_run_id = event.run_id
                self.pending.append(
                    Pending("notice", prompts.NOTICE_DEEPER_AVAILABLE, event.run_id)
                )
            return
```

with, next to `first_level_done`:

```python
    def has_offerable(self, run_id: str) -> bool:
        return any(
            level.run_id == run_id
            and level.state == SILENT
            and level.text.strip()
            and not _is_no_information_result(level.text)
            for level in self.memory.levels
        )
```

`self.offered_run_id: str | None = None` goes in `Conductor.__init__`
(`conductor.py:~96`), and `SILENT` joins the existing `from app.core.memory
import NEW, PARTIAL, ...` line.

### B5 — the yes-path, both routes

You asked for both. They coexist without conflict, because `Attention.classify`
runs before the LLM ever sees the turn: the regex wins when it matches, and the
LLM route is the fallback for anything phrased outside it.

**(a) Regex route.** `app/core/attention.py:26`, a second pattern rather than
widening `_CONTINUE` (bare はい must not be classified as "resume the cut-off
sentence"):

```python
_CONTINUE = re.compile(r"(続き|続けて|その先)")
# Mode B: an answer to "お聴きになりますか？". Anchored, so it only catches a turn
# that *starts* as an affirmation, not any sentence containing はい.
_AFFIRM = re.compile(r"^(はい|うん|ええ|お願い|おねがい|聞きたい|教えて|知りたい)")
```

and in `classify` (`attention.py:95`):

```python
        if _CONTINUE.search(text) or _AFFIRM.match(text):
            return "continue"
```

**(b) Conductor side.** `app/core/conductor.py:286-290`. Today the `continue`
branch only knows about a cut-off report; it gains the offer, and — importantly
— stops answering a stray 「はい」 with 「途中の回答はありません」:

```python
        if turn.command == "continue":
            level = self.memory.last_partial()
            if level is not None:
                return  # the ladder resumes it, as before
            # Mode B: no cut-off report, but a deep result was offered — this is
            # the user accepting it. Promote it back into the speech ladder.
            if self.accept_offer():
                return
            # Neither: fall through to a normal reply instead of a canned
            # "nothing to continue", which is the wrong answer to a stray はい.
            if not turn.text:
                self.pending.append(Pending("notice", prompts.NOTICE_NOTHING_TO_CONTINUE))
                return
            self.speaker.start_reply(turn.text, context=self.memory.summary_for_llm())
            return
```

```python
    def accept_offer(self) -> bool:
        """Un-silence the deep levels the user just asked for. Mode B."""
        run_id, self.offered_run_id = self.offered_run_id, None
        if run_id is None:
            return False
        promoted = False
        for level in self.memory.levels:
            if level.run_id == run_id and level.state == SILENT and level.text.strip():
                level.forced = True  # asked for out loud, so the report pass may not skip it
                self.memory.mark_new(level)
                promoted = True
        return promoted
```

The offer is also cleared in `start_research` (`conductor.py:298`) and in
`stop_everything` (`conductor.py:~310`) — one line each,
`self.offered_run_id = None` — so a stale offer from the previous question can
never be accepted after the topic moved on.

**(c) LLM route — no code.** Retained SILENT levels are already reachable via
`Memory.find`, and `summary_for_llm` already lists them, so a paraphrased yes
(「もっと詳しく」、「で、他には？」) goes through the normal reply pass and lands on
`read_result`. Nothing to change; it just keeps working.

### B6 — drop the plan preview

Same edit as **A2**. Mandatory here rather than optional: Mode B must not
announce a plan whose second half it has decided not to narrate.

### Resulting behaviour

「調べます」 → shallow report, closed cleanly, no 続けて → silence while deep runs →
either nothing at all (deep found nothing new, or the sufficiency gate said the
answer was already closed and deep never ran) or one short
「さらに詳しい内容も調べてあります。お聴きになりますか？」.

### Risk to watch

`RAG_LEVEL_TIMEOUT_SECONDS=150` (`.env:117`) is the client watchdog on the gap
between SSE frames. If the deep stage exceeds it, `ResearchFailed(level_gap)`
fires and `RAG_STREAM_MAX_RETRIES=1` re-runs **the whole question**, shallow
included — the user would hear the first answer twice. The B1 budgets
(45 s deep deadline, 15 s compile wait) keep it well under; do not raise them
without raising the watchdog to match.

### Tests

`uv run pytest tests/test_conductor.py tests/test_attention.py tests/test_memory.py -q`

Expected failures, to be skipped rather than deleted:

- `test_plan_preview_only_for_the_foreground_run`, `test_queued_background_plan_preview_is_discarded` (B6 removed the preview)
- the bridge-line assertions around `tests/test_conductor.py:~360-390`
  (「次の観点のあとにも段階が残っています」, 「直前の締めの一文」, and the two
  「続けて…」 fixtures) — B3 makes those paths unreachable by design

Worth adding, cheap and unit-level: one test that a second `LevelReady` on the
same run lands as SILENT and queues no speech, and one that `ResearchFinished`
with an empty deep level queues nothing.

---

## 4. The one shared backend patch (`llm-wiki-dist`)

Same patch for both branches, deployed once. Under Mode A it is dead code —
`max_levels=1` means `pending_stages` is empty and `_needs_deeper_research` is
never called — so one deployed backend serves both branches.

**What it fixes:** `_needs_deeper_research` already asks the model to name, in
`SufficiencyVerdict.missing`, *the specific thing a further search could still
turn up*. That string is then thrown away and collapsed into a bool. The deep
stage consequently runs its generic "read the rest of the document" prompt
instead of chasing the gap the gate just identified — which is both slower and
the reason the deep output drifts off the user's actual question.

Four additive edits, ~10 lines, all no-ops when the gap string is empty (gate
disabled, gate timed out, or Mode A):

**1. Carry the gap on the run state** — `graph/realtime.py:2440`, `_RunState`:

```python
    incomplete_reason: str | None = None
    # What the sufficiency gate said was still missing. The deep stage reads it
    # so it chases that gap instead of re-reading the whole document.
    gap: str = ""
```

**2. Return it instead of discarding it** — `graph/realtime.py:1506`,
`_needs_deeper_research`. Change the signature to `-> tuple[bool, str]` and the
three exits:

```python
        if not options.sufficiency_gate or not state.facts:
            return True, ""
        ...
        except Exception as exc:
            log.info("realtime depth check failed; researching deeper: %s", exc)
            return True, ""
        ...
        return not complete, missing
```

**3. Store it at the call site** — `graph/realtime.py:827`:

```python
                if pending_stages:
                    deeper, gap = self._needs_deeper_research(
                        retrieval, state, opts, pool, stop_event, deadline
                    )
                    state.gap = gap
                    if deeper:
                        levels.extend(pending_stages)
                        ...
```

**4. Point the deep stage at it** — two prompt sites:

- `_submit_deep_readers` (`graph/realtime.py:1690`), inside the prompt tuple:

```python
                    f"User question:\n{retrieval.query}",
                    (
                        f"The first pass left this specific gap; answer only it:\n{state.gap}"
                        if state.gap
                        else ""
                    ),
                    self._scope_line(retrieval),
```

  (the surrounding comprehension already drops empty parts, so no guard needed)

- `_submit_deep_agents` (`graph/realtime.py:1597`), prepended to
  `extra_instructions`:

```python
        extra_instructions = (
            (f"今回埋めるべき不足は次の点だけです。これ以外の調査はしないでください:\n{state.gap}\n\n"
             if state.gap else "")
            + "既に他の調査で次の内容が確認済みです。重複を避け、"
            ...
        )
```

**Not touched, deliberately:** the gate's fail-open bias stays as it is. Mode B
handles an over-eager gate by staying silent, and loosening it would degrade the
non-voice callers of this endpoint.

**Backend tests:** `pytest tests/test_realtime.py tests/test_ask_realtime.py -q`
in `llm-wiki-dist`. Anything asserting `_needs_deeper_research(...) is True/False`
needs the tuple unpacked.

---

## 5. Order of work

1. Branch `shallow-only` from `f3dac8a`; apply A1, A2, A3; run the two test
   files; smoke against `app.rag.placeholder`. **~15 minutes, no backend deploy.**
2. Branch `deep-silent` from `f3dac8a`; apply B1-B6; run the three test files.
3. Apply the section-4 backend patch in `llm-wiki-dist`, deploy once. Both
   branches keep working against it.

Mode A is the safe demo fallback: it needs no backend deploy and reverts by
uncommenting one line.

## 6. Verbosity budget, before and after

| | today | Mode A | Mode B |
|---|---|---|---|
| holding notice | 1 | 1 | 1 |
| plan preview | 1 (LLM) | 0 | 0 |
| spoken reports | 3 | 1 | 1 |
| 続けて hand-offs | up to 2 | 0 | 0 |
| closing question | 0 | 0 | 0 or 1 |
| backend LLM calls | shallow + gate + deep (readers, agents, selector, compile) + anticipation | shallow only | shallow + gate + gap-scoped deep |
