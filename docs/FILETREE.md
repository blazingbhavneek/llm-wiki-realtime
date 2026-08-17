# File tree and navigation map

This is a Japanese-speaking LiveKit voice agent that answers questions from an
internal wiki: it listens, decides in one place (`Conductor`) whether to talk
casually or open a research run, and speaks findings as they stream back over
SSE. This branch, `deep-silent` (Mode B of `plan.md` §3), changes only how
much of that research gets said: the shallow answer is spoken immediately,
everything deeper is fetched in silence, and it is offered — once, only if
there is something to offer — instead of narrated.

This is a map, not an explainer. The *why* lives in `docs/DESIGN.md`
(architecture), `docs/OPERATIONS.md` (runbook), `docs/SETUP.md` (bootstrap) —
linked, not repeated. Symbols are referenced by name, not line number.

---

## 1. Annotated tree

Generated/vendored, not documented individually: `.venv/`,
`frontend/node_modules/`, `frontend/dist/`, `**/__pycache__/`,
`.pytest_cache/`, `.cache/`, `certs/`, `models/`, `vendor/`. `uv.lock` /
`frontend/package-lock.json` are lockfiles.

### `app/` root

- `__main__.py` — `python -m app` → `runtime.worker.main()`. No logic here.
- `config.py` — `Settings.from_env()`, read once. Cross-cutting wiring only; provider env stays in each provider's own `base.py`.
- `log.py` — `dbg()`, flushed structured print (LiveKit's own logging config doesn't reach this subprocess reliably).
- `timing.py` — `TURN_TIMING=1` latency stopwatch. Must stay a costless no-op when unset.

### `app/core/` — decisions only. No network, no provider imports.

| File | Owns | Must never |
|---|---|---|
| `events.py` | The vocabulary: frozen dataclasses. | Contain behavior. |
| `attention.py` | `Attention` — orb open/dormant + regex commands (`stop`/`repeat`/`continue`/`close`), incl. Mode B's `_AFFIRM`. | Gate *output* — a dormant assistant still reports (§4). |
| `memory.py` | `Memory` — every `LevelResult` (`NEW→REPORTING→{REPORTED,PARTIAL}`/`SILENT`) and `PendingObjective`. `find()` is `read_result`'s fuzzy lookup. | Return "not found" while any level exists (§4). |
| `speaker.py` | `Speaker` — the single speech slot, the only file touching `AgentSession`/TTS. `start_*` returns a `speech_id` immediately. | Block the Conductor — never `await` playout inline. |
| `screen.py` | `Screen` — fire-and-forget mirror to the browser's data channel (`research`/`attention` topics). | Let a publish failure stall the Conductor (logs `[SCREEN DROP]` instead). |
| `conductor.py` | `Conductor` — every decision. `handle()` dispatches by event type; `speak_next()` is the priority ladder. Mode B: `offered_run_id`, `accept_offer()`, `has_offerable()`, `_deep_levels()`, `first_level_done()`. | Do slow work inline. |

### `app/agent/` — what the LLM is told and may call

- `assistant.py` — `Assistant(Agent)`. `on_user_turn_completed` always raises `StopResponse`; only the Conductor calls `speaker.start_reply`.
- `tools.py` — `research_wiki`/`read_result`/`stop_research`. A tool posts one event and returns; the first two also raise `StopResponse` (§4).
- `prompts.py` — every user-facing string: `GREETING`, `NOTICE_*` (incl. Mode B's `NOTICE_DEEPER_AVAILABLE`), `ASSISTANT_INSTRUCTIONS` (the casual-vs-research router), `report_instructions`/`_bridge_rules` (per-level narration + hand-off).

### `app/stt/`, `app/tts/`, `app/llm/`, `app/rag/` — swappable providers

Each: `base.py` (contract + capability `ClassVar`s), `__init__.py` (`REGISTRY` of import-strings, resolved lazily), `README.md`. Selector env vars in §6.

- `stt/nemotron.py` — self-hosted realtime WebSocket client + `serve()`. Commits on VAD end-of-speech, not the server's own endpointing (see `finals_are_utterances`, §4).
- `stt/qwen.py` — vLLM batch, VAD-segmented, hardcodes `language=ja`.
- `stt/voxtral.py` — vLLM; batch mode verified, the bespoke realtime `/v1/realtime` dialect is not — read its docstring before trusting it.
- `tts/supertonic.py` — self-hosted client **and** the ONNX FastAPI server (`serve()`) in one file; heavy deps import lazily so `build()` never pulls them in.
- `tts/qwen3.py` — vLLM client only.
- `llm/openai_compatible.py` — any `/v1/chat/completions` endpoint (llama-server, vLLM, SGLang).
- `rag/base.py` — `ResearchRun`/`ResearchBackend` as `@runtime_checkable` Protocols; the only contract `conductor.py` imports (under `TYPE_CHECKING`).
- `rag/sse.py` — generic SSE parser, no domain meaning.
- `rag/llm_wiki.py` — **the only file that talks to the real backend**. `ResearchRun` (one stream + watchdog), `ResearchPool`/`LLMWikiBackend`, `VOICE_KNOBS` (Mode B's `max_levels=2` + tightened deep budgets, §7).
- `rag/placeholder.py` — a stand-in **server**, not a registered backend (see its own warning in `rag/__init__.py`); always returns the same dummy string.

### `app/web/`, `app/runtime/`

- `web/http.py` — FastAPI app; serves `frontend/dist` (not the sources — §5), `/health`, `/token`.
- `web/tokens.py` — room create + agent dispatch + JWT.
- `web/tls.py` — local dev cert only; unused when Caddy fronts TLS.
- `runtime/entrypoint.py` — one LiveKit job: builds providers, `AgentSession`, `Conductor`. The commented-out greeting lives here (§9).
- `runtime/producers.py` — LiveKit callbacks → inbox events. **Translates, never decides.** Also `SESSION_ERROR`/`SESSION_CLOSED` → `screen.set_agent_status`.
- `runtime/worker.py` — process entry: `load_dotenv`, VAD prewarm, combined uvicorn + LiveKit worker bootstrap.

### `frontend/src/` — Vite/React client

- `App.jsx` — LiveKit `Room` lifecycle, mic/orb wiring, `research`/`attention` data-channel handler, text composer. Remounts `<ResearchPanel key={research.agentRunId}>` per run.
- `lib/research.js` — `researchReducer`; refuses any frame whose `agent_run_id` mismatches the current run, except `ask` (the only reset frame). Also the dev `DEMO_SCRIPT`.
- `lib/audio.js` — `AudioBus`, mic/agent level metering + the orb's voice gate. Must attach both tracks as `MediaStreamSource`, never `MediaElementSource`.
- `components/WaveField.jsx` — the orb: WebGL visualizer **and** the only mic control.
- `components/ResearchPanel.jsx` — sidebar trace; deliberately no markdown/diagram rendering.
- `components/Transcript.jsx`, `components/Composer.jsx` — conversation tab, typed-text row.

### `tests/`

- `fakes.py` — `FakeSpeaker`/`FakeScreen`/`FakePool`, `build()`/`feed()` harness.
- `test_attention.py` — the orb gate + commands, incl. Mode B affirmations.
- `test_conductor.py` — one test per `DESIGN.md` acceptance check plus a Mode B section. Read before touching `conductor.py`.
- `test_memory.py` — the `LevelResult` state machine + `find()`'s fuzzy matching.
- `test_providers.py` — every registered provider resolves offline and declares its capability flags; never calls `build()`.
- `test_sse.py` — the SSE parser alone.
- `live/` — **not collected** by a default run (`live_stt.py`/`live_tts.py` don't start with `test_`); run explicitly, §8. Hits real model servers.

### `scripts/`

- `build_asr_server.sh` — clones+builds NeMo-Speech.cpp (nemotron STT). Idempotent.
- `setup_https_livekit.sh` — one-shot/idempotent: `.env`/cert wiring, LiveKit+Caddy up. Re-run after any `PUBLIC_HOST` change.

### `docs/`

- `DESIGN.md` — architecture rationale. **Stale in one section** — see §2.
- `OPERATIONS.md` — day-2 runbook: ports, start/stop, troubleshooting.
- `SETUP.md` — day-0 bootstrap + provider-switch cheat sheet.
- `REORGANIZATION.md` — the layout proposal this tree now matches (its own "proposal only" header is out of date — §2).

### Root

- `README.md` — orientation, layout tree, run commands.
- `pyproject.toml` — deps, `supertonic` extra, console scripts.
- `.env` / `.env.example` — real config / redacted template.
- `Caddyfile`, `docker-compose.caddy.yml` — TLS proxy + LiveKit containers.
- `livekit.yaml` — LiveKit's **own** config; its `keys:` block is a separate credential pair from `.env`'s `LIVEKIT_API_KEY`/`SECRET` (see `OPERATIONS.md` §9).
- `plan.md`, `pre_branch_plan.md` — the design docs this branch implements. Verify claims against code (§2, §7).

---

## 2. Documentation accuracy notes

- **`DESIGN.md` §4.1 (Attention) is stale.** It documents wake words, a 20s
  idle timeout, a 5s reply window. The real `app/core/attention.py` has none
  of these — its own docstring says so: push-to-talk orb only.
- **`DESIGN.md` §9.1's `VOICE_KNOBS` table is stale.** Lists
  `deadline_seconds: 90`; the code has `600` (raised from `260`, per its own
  comment, for a slow backend LLM). It also predates every Mode B knob (§7).
- **`REORGANIZATION.md`'s "proposal only, no code changed" header is false
  now** — the current `app/` tree matches its proposed layout exactly. Read
  it for the provider-contract rationale, not as a to-do list.

---

## 3. Control flow

```
 mic (VAD) ──┐                                ┌──> Speaker.start_report/reply/notice
 typed text ─┼─> runtime/producers.py ───┐    │      -> AgentSession.generate_reply/say
 tool call ──┤   (translate ONLY)        │    │      -> Speaker._watch() awaits playout
 SSE frame ──┘  rag/llm_wiki.py tasks    │    │
  (detached, per-ResearchRun,            ▼    │
   never decide)                  inbox: asyncio.Queue  <── SpeechFinished/SpeechInterrupted
                                   (SINGLE CONSUMER)          (posted by Speaker._watch)
                                          │
                                          ▼
                    Conductor.run(): event = await inbox.get()
                                     await self.handle(event)     <- the ONE decision point
                                     await self.speak_next()      <- ladder, EVERY event
                                          │                   │
                     screen.publish_research(frame)    Attention.accept(text) -> Turn
                     (agent_run_id-stamped, FOREGROUND only)  -> command or start_reply()
                                          │
                                          ▼
              browser: frontend/src/lib/research.js researchReducer
              (rejects frames whose agent_run_id != current, except `ask`)
```

1. **Speech/text → event.** `runtime/producers.py`'s `_on_user_state`/
   `_on_transcript` push `UserStartedSpeaking`/`UserSaidText(from_text_input=False)`;
   typed text goes through `_on_text_input` with `from_text_input=True`.
2. **`Conductor.handle` is the single decision point** — one dispatch chain
   in `core/conductor.py`. Accepted text → `handle_user_text` →
   `Attention.accept()` → a `Turn.command`, dispatched; `none` ends in
   `speaker.start_reply(turn.text, context=...)`.
3. **A tool call opens research, never does it.** `agent/tools.py::research_wiki`
   posts `ResearchRequested` and raises `StopResponse` (§4).
   `Conductor.handle` reacts with `start_research()` → `pool.start(question)`.
4. **Research streams back through the same inbox.**
   `rag/llm_wiki.py::ResearchRun._attempt` runs detached; its frames push
   `ResearchProgress` (always), `PlanReady`/`PlanRevised`, `LevelReady`,
   terminally `ResearchFinished`/`ResearchFailed` — into the same `inbox`.
5. **`speak_next` is a priority ladder**, run after *every* event: busy /
   user-speaking → return; else drain `self.pending` (foreground-only);
   else `memory.next_partial(FOREGROUND)`; else `memory.next_new(FOREGROUND)`.
   Background levels are never picked up here — see §7.
6. **Frames mirror to the browser** via `screen.publish_research`, stamped
   with `agent_run_id` at exactly three sites in `conductor.py` (§4).
   `frontend/src/lib/research.js::researchReducer` is the only consumer.

---

## 4. Invariants an editor must not break

- **Single-consumer inbox, no locks.** Only `Conductor.run()` calls
  `inbox.get()`; everything else only `put_nowait()`s. No second reader, no
  `await` inside the loop body besides `handle`/`speak_next`.
- **Tool functions raise `StopResponse`, and why.** `research_wiki`/
  `stop_research` post one event then raise — a tool never works itself,
  and a prompt alone can't reliably stop the model voicing a speculative
  "let me check" before the event even reaches the Conductor.
  `Assistant.on_user_turn_completed` *always* raises `StopResponse` too —
  the pipeline's own reply never plays; only the Conductor calls
  `speaker.start_reply`.
- **Foreground/background focus is orthogonal to lifecycle.**
  `move_to_background` only flips `run.focus` — a superseded run is never
  cancelled, keeps streaming and being remembered; it only loses the
  speech floor and screen mirroring (`ResearchProgress` filtered to
  `FOREGROUND`).
- **Watchdog/retry is a hard budget.** `RAG_PLAN_TIMEOUT_SECONDS` /
  `RAG_LEVEL_TIMEOUT_SECONDS` are enforced by `ResearchRun._watchdog`;
  blowing either fires `ResearchFailed`, and `RAG_STREAM_MAX_RETRIES` caps
  how many times `retry()` reopens the stream **from scratch** — including
  the already-delivered shallow answer. Backend stage budgets in
  `VOICE_KNOBS` must sum well under `RAG_LEVEL_TIMEOUT_SECONDS`; see §7 for
  the Mode B numbers.
- **Frames carry `agent_run_id` because the backend's don't.** The backend
  reuses `level_1`/`level_2`/`level_3` on every run and stamps no run id.
  Without the client-side stamp, the frontend's dedupe/version guards are
  effectively global, so a second question's frames get rejected as stale
  duplicates of the first (the bug fixed by `24de93c`; §9 lists its
  guarding tests). A new publish call that forgets the stamp reintroduces it.
- **Every speech is uninterruptible at the pipeline level, on purpose.**
  `runtime/entrypoint.py`'s `SessionConnectOptions`/
  `discard_audio_if_uninterruptible=False` stop LiveKit from substituting
  silence into STT mid-speech or auto-interrupting. Only the Conductor
  (`UserStartedSpeaking` → `interrupt_current()`/`duck()`) decides what a
  nearby voice means — letting LiveKit decide would make a real barge-in
  undetectable while a report plays.

---

## 5. How to run things

Full start order/ports/troubleshooting: `docs/OPERATIONS.md` §1/§6. Two
easy-to-miss facts:

- **`uv run pytest` cannot install here.** `uv.lock` pins
  `onnxruntime==1.28.0`, whose only macOS wheel targets `macosx_14_0_arm64`+;
  this machine is macOS 13, so `uv sync`/`uv run pytest` fails to resolve.
  Tests were actually run against a separately built venv resolving
  `onnxruntime==1.23.2` — use an equivalent venv, or
  `python -m unittest discover -s tests -t . -q` inside one, instead of
  `uv sync`/`uv run pytest`. `tests/live/` (`live_stt.py`, `live_tts.py`,
  `live_helpers.py`) is **not** collected by that command — its filenames
  don't start with `test_` — run it explicitly:
  `python -m unittest discover -s tests/live -t . -p "live_*.py" -q`
  (needs real model servers; see `tests/live/README.md`).
- **`app/web/http.py` serves `frontend/dist`, not the sources.** A change
  under `frontend/src/` is invisible until `cd frontend && npm run build`
  — no hot reload, and restarting `python -m app` alone doesn't help.

---

## 6. The seams for swapping a provider

| Kind | Registry | Env var | Names |
|---|---|---|---|
| STT | `app/stt/__init__.py::REGISTRY`/`get_provider()` | `STT_PROVIDER` | `nemotron`, `qwen`, `voxtral` |
| TTS | `app/tts/__init__.py::REGISTRY`/`get_provider()` | `TTS_PROVIDER` | `supertonic`, `qwen3` |
| LLM | `app/llm/__init__.py::REGISTRY`/`get_provider()` | `LLM_PROVIDER` | `openai_compatible` |
| RAG | `app/rag/__init__.py::REGISTRY`/`get_backend()` | `RAG_PROVIDER` | `llm_wiki` |

Each registry maps a name to an **import string**, resolved lazily, so an
unselected provider's dependencies (e.g. `onnxruntime` for `supertonic`)
never load. Per-provider env tables and "add one" steps live in each
package's `README.md`; `app/rag/README.md` also explains why
`app/rag/placeholder.py` must never join `REGISTRY`.

---

## 7. What this branch is: `deep-silent`, Mode B of `plan.md` §3

Verified against the code — see §2 for the one place this branch's final
state differs from `plan.md`'s description of it (the backend patch, below).

**The shape.** One shallow answer spoken immediately
(`VOICE_KNOBS["max_levels"] = 2` — stage 3, "anticipation", is dropped
outright); the deep stage runs and is retained in total silence; at most one
offer at the very end, only if there is something worth offering; no
`続けて` anywhere. Mechanically:

- `Conductor.handle(LevelReady)` marks every level **after** a run's first
  `SILENT` on arrival: `if level is not None and self.first_level_done(run.run_id):
  mark_silent(level)`. `first_level_done` is `sum(1 for level in memory.levels
  if level.run_id == run_id) > 1` — true only once a *previous* level for this
  run exists, so the shallow answer itself stays `NEW`.
- `Conductor.report()` unconditionally blanks the hand-off:
  `next_objective = ""; step_count = step`, forcing `_bridge_rules` onto its
  "close short, no promise" branch for every report.
- `Conductor.handle(ResearchFinished)` queues `NOTICE_DEEPER_AVAILABLE`
  only when `run.focus == FOREGROUND and self.has_offerable(event.run_id)`.

### `SILENT` is overloaded — the subtlest trap here

`Memory.mark_silent` is called from two unrelated paths, indistinguishable
by `state` alone:

1. **`Conductor.handle(LevelReady)`, Mode B** — "retained deep result, not
   yet spoken."
2. **`Conductor.handle(SpeechFinished)`, pre-existing** — empty
   `spoken_text` means the report pass read this level and declined to
   speak it.

`Conductor._deep_levels(run_id)` tells them apart: every level of the run
**except the lowest-`serial`** one (arrival order — always the shallow
answer, whatever its own report pass did). `has_offerable()`/
`accept_offer()` both filter through `_deep_levels`, never `memory.levels`
directly.

**Guarding test:**
`tests/test_conductor.py::test_an_empty_first_level_report_is_not_offered_back`
(docstring states the exact bug).

**Warning:** "simplifying" `has_offerable`/`accept_offer` to scan all
`SILENT` levels reintroduces it — a shallow answer whose own report pass
legitimately came back empty would be offered back / promoted as if it
were the deeper content.

### Offer lifecycle

- **Set:** `Conductor.handle(ResearchFinished)` → `self.offered_run_id = event.run_id`.
- **Cleared/consumed, three places:** (1) `accept_offer()` — consumes it,
  promotes the silent deep levels to `NEW`; (2) `start_research()` —
  unconditional `None`, so a new question can't accept a stale offer; (3)
  `stop_everything()` — `None`, so a stop ends any offer too.
- **Consumed** from `handle_user_text`'s `"continue"` branch, only when
  `memory.last_partial()` is `None` (a cut-off report still takes
  priority). **Caveat found while verifying this doc, not in `plan.md`:**
  the LLM/`read_result` fallback (below) narrates a retained deep level
  **without** touching `offered_run_id` — so accepting by paraphrase
  (「もっと詳しく」) leaves the offer standing, and a later bare 「はい」 would
  re-trigger `accept_offer()` on content already answered. Not fixed here;
  flagged for whoever picks it up.

### The affirmation rule

`Attention._AFFIRM` (`app/core/attention.py`):

```python
_AFFIRM = re.compile(
    r"^(?:(?:はい|うん|ええ|お願いします|お願い|おねがいします|おねがい"
    r"|聞きたい|教えて|知りたい)[、。．！？!?\s]*)+$"
)
```

Anchored at **both** ends, group repeated — matches only a turn that is
*entirely* affirmation tokens plus punctuation, never a prefix. This matters
because `classify()` calls `_AFFIRM.match(text)`, and `.match()` only
anchors at the start by default — without the trailing `$` it would accept
anything merely *beginning* with a token. 「ええと、mpf_buf とは？」 opens with
`ええ`; 「教えて、戻り値は？」 opens with `教えて`; read as a yes under a prefix
match, either would consume the offer, have the Conductor read the deep
result back, and leave the real question unanswered.

**Guarding tests:**
`tests/test_attention.py::test_mode_b_affirmations_answer_the_deeper_research_offer`
(pins the positive cases and the `ええと`/`教えて`/`知りたい`-prefix negatives
against `classify()` directly), and
`tests/test_conductor.py::test_a_new_question_while_an_offer_stands_is_answered_not_swallowed`
(end-to-end).

**The LLM route via `read_result` is the deliberate fallback** for anything
the regex doesn't match — `ASSISTANT_INSTRUCTIONS` routes a paraphrased
acceptance through a normal reply turn, which can call `read_result`;
`Memory.find()` doesn't care that a level is `SILENT`.

### Knob budgets vs. the client watchdog

`app/rag/llm_wiki.py::VOICE_KNOBS`:

```python
"max_levels": 2,                       # drops stage 3 (anticipation) outright
"subagent_count": 2,                   # backend default 4
"subagent_max_steps": 3,               # backend default 5
"subagent_compile_wait_seconds": 15,   # backend default 30
"deep_deadline_seconds": 45,           # backend default 120
"deadline_seconds": 600,               # OVERALL backend run budget — NOT the client
                                        # watchdog below; don't conflate the two
```

Tightened so the deep stage finishes well inside `RAG_LEVEL_TIMEOUT_SECONDS`
(`150` in `.env.example`). Blowing that watchdog re-runs the **whole**
question via `retry()` — shallow stage included — so the user hears the
first answer twice. **Do not raise the subagent/deep knobs without also
raising `RAG_LEVEL_TIMEOUT_SECONDS`**, or this branch's core promise breaks
exactly the way it was built to avoid.

### The backend patch in `plan.md` §4 is NOT applied

Verified against `/Users/blazingbhavneek/Code/llm-wiki-dist`
(`llm-wiki-dist/graph/realtime.py`): `_RunState` has no `gap` field, and
`_needs_deeper_research` still returns a plain `bool`, not
`tuple[bool, str]` — none of §4's four edits (carrying the sufficiency
gate's `missing` string onto the deep stage) have landed. This branch works
without it: the deep stage runs its generic "read the rest of the document"
prompt rather than one scoped to what the gate said was missing. If the
deep content looks unfocused relative to the question, this is why.

---

## 8. Symptom → file table

| Symptom | Open first | Then check |
|---|---|---|
| Assistant talks too much / repeats itself | `core/conductor.py` — `PlanReady` branch (preview commented out, §9) | `agent/prompts.py::ASSISTANT_INSTRUCTIONS` (forbids narrating the tool call); `assistant.py::on_user_turn_completed` still raising `StopResponse` |
| It announces a search twice | `core/conductor.py::start_research` — `NOTICE_RESEARCHING` queued once, never per retry | `PlanReady`'s commented preview block — if re-enabled, that's announcement #2 |
| A greeting fires on load | `runtime/entrypoint.py` — commented `queue_notice(prompts.GREETING)` / `IdleTick()` pair (§9) | `web/http.py::token` — a fresh room per page load means an uncommented greeting fires every reload |
| The research panel shows a stale run | `frontend/src/lib/research.js::researchReducer` — the `agent_run_id` guard | `core/conductor.py`'s three `agent_run_id` stamp sites (§4) |
| The panel freezes mid-run | agent log for `[SCREEN DROP] topic=...` (`core/screen.py::_publish`) | `rag/llm_wiki.py::ResearchRun._watchdog` — `level_gap`/`plan_timeout` retries once then apologizes |
| A follow-up opens a redundant research run | `agent/prompts.py::ASSISTANT_INSTRUCTIONS` — "already being researched" routing | `core/memory.py::note_plan`/`await_pending` — confirm `note_plan` still runs unconditionally in `PlanReady`; `find()` must never say "not found" while any level exists |
| Speech overlaps or is cut off | `core/speaker.py` — `interrupt`/`duck`/`unduck`, commanded only by `Conductor` | `runtime/entrypoint.py`'s `interruption.discard_audio_if_uninterruptible=False` (§4); `Attention.state` |
| Agent present but deaf (orb red, nothing happens) | agent log: `grep -E "SESSION_ERROR\|SESSION_CLOSED"` (`runtime/producers.py`) | `App.jsx::handleAgentGone`/`screen.set_agent_status`; `OPERATIONS.md` §6 |
| STT/TTS/LLM/RAG swap doesn't take effect | confirm the `*_PROVIDER` env var and that `python -m app` was **restarted** | `app/<kind>/__init__.py::REGISTRY` — a typo raises `ValueError` listing valid names |
| **(Mode B)** deep result never offered | `core/conductor.py::has_offerable`/`_deep_levels` — did a second level even arrive, and does it fail `_is_no_information_result`? | `VOICE_KNOBS["max_levels"]` must be `2`; `run.focus == FOREGROUND` at `ResearchFinished` — a backgrounded run never offers |
| **(Mode B)** offer fires with nothing to say | `core/conductor.py::_is_no_information_result`/`_NO_INFORMATION_PATTERNS` | tighten the patterns to the backend's actual no-result wording |
| **(Mode B)** saying 「はい」 does nothing | `core/attention.py::_AFFIRM` — still matches bare「はい」→`"continue"`? | `handle_user_text`'s `"continue"` branch falls to `accept_offer()` only when `last_partial()` is `None`; confirm `offered_run_id` wasn't cleared by an intervening `start_research`/`stop_everything` |
| **(Mode B)** a new question gets swallowed | `core/attention.py::_AFFIRM` — still fully anchored (`^...$`), not loosened to prefix/`.search()`? | `tests/test_conductor.py::test_a_new_question_while_an_offer_stands_is_answered_not_swallowed` |

---

## 9. Deliberately commented out or skipped — do not "clean up"

**Commented-out code:**

| Where | What | Why |
|---|---|---|
| `runtime/entrypoint.py` | `conductor.queue_notice(prompts.GREETING)` + `inbox.put_nowait(IdleTick())` | Silences the greeting (a fresh room per page load would replay it every time). `prompts.GREETING` stays defined, unused. |
| `core/conductor.py`, `PlanReady` branch | the `self.pending.append(Pending("prompt", plan_preview_instructions(...)))` block | The model already announces the search itself; this said it twice. `self.memory.note_plan(...)` above it is **not** commented — load-bearing for `read_result`'s "already being researched" answer. |
| `rag/llm_wiki.py::VOICE_KNOBS` | `# "max_levels": 3,` | The pre-Mode-B value. Swap the two lines to fully revert to un-silenced three-stage behavior. |

**Skipped tests, `tests/test_conductor.py`:**

| Test | Reason | Why it must stay skipped |
|---|---|---|
| `test_plan_preview_only_for_the_foreground_run` | plan preview removed (`pre_branch_plan.md` P2a) | Asserts the removed preview LLM pass runs. |
| `test_queued_background_plan_preview_is_discarded` | same | same |
| `test_a_one_stage_plan_is_previewed_without_promising_a_next_step` | same | same |
| `test_the_spoken_preview_does_not_commit_to_a_stage_count` | same | same |
| `test_a_grown_plan_reaches_the_user_through_the_hand_off` | Mode B: `report()` blanks `next_objective` unconditionally (B3) | No hand-off sentence is ever generated for this test to find. |
| `test_two_reports_never_hand_off_with_the_same_sentence` | same | No hand-off sentence exists to compare against a previous one. |

**Do not "fix" any of these by restoring the behavior they assert** — for
the four preview tests, that means re-enabling the announcement Mode B (and
`pre_branch_plan.md` P2a, independently) deliberately removed; for the two
hand-off tests, it means giving `report()` a non-empty `next_objective`
again, reopening the `続けて` promise this branch exists to close.
