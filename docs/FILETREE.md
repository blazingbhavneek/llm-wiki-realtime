
# FILETREE.md — navigation map for `llm-wiki-realtime` (branch `shallow-only`)

A Japanese-speaking voice agent that answers questions about an internal wiki over LiveKit: one `Conductor` event loop decides everything, research streams back level-by-level over SSE and is spoken as it arrives, and every I/O dependency (STT/TTS/LLM/RAG) is a swappable provider. **This branch caps the backend to its first research stage only** (`VOICE_KNOBS["max_levels"] = 1`), which is the whole story of §7 below — read it before touching anything RAG-related.

This file is a map, not an explanation. The *why* lives in [`docs/DESIGN.md`](DESIGN.md) (architecture), [`docs/OPERATIONS.md`](OPERATIONS.md) (runbook) and [`docs/SETUP.md`](SETUP.md) (fresh-machine setup) — read those first and treat this as an index into them plus the parts they don't cover: exact file/symbol pointers, a symptom table, and what makes this branch different.

---

## 1. Annotated tree

Generated/vendored, never hand-edit, not itemised below: `.venv/`, `frontend/node_modules/`, `frontend/dist/` (build output — see §5), `**/__pycache__/`, `.pytest_cache/`, `uv.lock`, `frontend/package-lock.json`.

### `app/` — root

| File | Owns | Must never |
|---|---|---|
| `__init__.py` | Package docstring naming the layout | Contain logic |
| `__main__.py` | `python -m app` → `runtime.worker.main()` | Contain logic |
| `config.py` | `Settings`/`LiveKitSettings`/`WebSettings`/`TuningSettings`, env read once | Hold provider-specific settings (those live in each provider's `base.py`) |
| `log.py` | `dbg()` — structured `print`, because LiveKit owns subprocess logging config | Use the `logging` module |
| `timing.py` | `TURN_TIMING=1` per-turn latency stopwatch | Cost anything when `TURN_TIMING` is unset |

### `app/core/` — the decision layer (no network, no provider imports — `speaker.py` is the sole, deliberate exception, and only for TTS objects already built elsewhere)

| File | Owns | Must never |
|---|---|---|
| `conductor.py` | `Conductor` — the giga thread. `handle()` (decision table), `speak_next()` (priority ladder), `report()`, `position_of()`, `_is_no_information_result()` | `await` anything slow inside `run()`/`handle()`; let two places decide the same thing |
| `events.py` | Frozen dataclasses — the only vocabulary producers may speak | Contain behavior |
| `attention.py` | `Attention`/`Turn` — push-to-talk gate (orb button or typed text), regex command classifier (`stop`/`repeat`/`continue`/`close`) | Depend on the LLM for `stop` to work |
| `memory.py` | `Memory`, `LevelResult`, `PendingObjective` — every level ever received, the live plan's pending objectives, `find()` fuzzy lookup | Return "not found" from `find()` while any level exists; drop a `SILENT`/`REPORTED` level |
| `speaker.py` | `Speaker` — the only file touching `AgentSession`/TTS; `start_reply`/`start_report`/`start_notice` | Block the Conductor loop; create an interruptible speech |
| `screen.py` | `Screen` — the only file writing to the LiveKit data channel | Raise into the Conductor on a publish failure (logs `[SCREEN DROP]` instead) |

### `app/agent/` — what the LLM is told and may call

| File | Owns | Must never |
|---|---|---|
| `assistant.py` | `Assistant(Agent)`; `on_user_turn_completed` always `raise StopResponse()` | Let the pipeline's own reply reach the user |
| `tools.py` | `research_wiki`, `read_result`, `stop_research`, `AssistantDeps`, `read_retained()` | Do the work itself — each tool posts one inbox event and returns/raises |
| `prompts.py` | Every spoken string: `GREETING`, `NOTICE_*`, `ASSISTANT_INSTRUCTIONS`, `plan_preview_instructions` (unreachable, see §8), `report_instructions`/`_bridge_rules` (the hand-off logic) | Be bypassed by a literal string elsewhere in the app |

### `app/stt/`, `app/tts/`, `app/llm/`, `app/rag/` — provider packages (contracts detailed in §6)

| File | Owns | Must never |
|---|---|---|
| `*/base.py` | The provider ABC/Protocol + its `*Settings` dataclass | Import a sibling provider, or (stt/tts/llm) anything from `app.*` besides `os`/`dataclasses` |
| `*/__init__.py` | `REGISTRY` (import strings, not classes) + `build_*()` facade | Eagerly import an unselected provider's heavy deps |
| `stt/nemotron.py` | Client + `serve()` for NeMo-Speech.cpp's realtime WS; the doc on why `--endpointing` stays off | Trust `openai.STT` to speak this server's dialect (it can't — see the module docstring) |
| `stt/qwen.py` | Batch client pinned to `language=ja` for a vLLM Qwen3-ASR deployment | Accept a caller-supplied language override |
| `stt/voxtral.py` | Batch + realtime clients for vLLM-hosted Voxtral; verified-live notes | Assume the realtime path speaks the OpenAI Realtime API (it speaks vLLM's own flat protocol) |
| `tts/supertonic.py` | Client **and** the local ONNX server (`serve()`) | Import `onnxruntime`/`supertonic` at module scope outside `serve()`'s call path |
| `tts/qwen3.py` | Client only, vLLM-hosted | Assume `TTS_LANG` does anything (it's `TTS_INSTRUCTIONS` that pins the language here) |
| `llm/openai_compatible.py` | The one LLM provider (llama-server/vLLM/SGLang) | — |
| `rag/sse.py` | Generic line-oriented SSE parser | Know what a frame means |
| `rag/llm_wiki.py` | `ResearchRun`/`ResearchPool` (`LLMWikiBackend`), `VOICE_KNOBS`, the plan/level watchdog, the only file that talks to the real backend | Decide whether a finding is spoken, or whether a failure is retried — that's the Conductor's call |
| `rag/placeholder.py` | A stand-in **server** with canned answers, for testing without the real wiki | Register itself in `rag/REGISTRY` (it is not a backend) |
| `*/README.md` | Provider-swap docs, per-package | Be duplicated here — see §6 |

### `app/web/`

| File | Owns | Must never |
|---|---|---|
| `http.py` | FastAPI `app`: `/health`, `/`, mounts `frontend/dist` | Serve `frontend/src` |
| `tokens.py` | `/token`'s LiveKit room/dispatch/JWT minting | — |
| `tls.py` | Local dev cert generation, only when `TEXT_TEST_TLS` is set | Run when Caddy is the one terminating TLS |

### `app/runtime/` — the only layer that knows every module exists

| File | Owns | Must never |
|---|---|---|
| `entrypoint.py` | `entrypoint(ctx)` — builds providers/session/Conductor, wires callbacks, the (currently disabled) greeting | Contain a decision — hand everything to `Conductor` |
| `producers.py` | `attach()`/`idle_ticker()` — LiveKit session/room callbacks → inbox events | React to anything itself |
| `worker.py` | Process bootstrap: `prewarm`, `start_web_server`, `run_combined_server`, proxy scrubbing | — |

### `frontend/src/`

| File | Owns | Must never |
|---|---|---|
| `App.jsx` | Room connection/reconnect, orb state machine, mic + voice-gate wiring, text send, data-channel dispatch | — |
| `lib/audio.js` | `AudioBus` — Web Audio taps for mic/agent RMS levels and the local voice gate | Use `createMediaElementSource` on the agent track (would silence playback) |
| `lib/research.js` | `researchReducer` — client mirror of the SSE state machine, the `agent_run_id` guard (§4/§P3), the `DEV_MODE` demo script | Accept a frame from a different `agent_run_id` unless it's `type: 'ask'` |
| `components/ResearchPanel.jsx` | Renders `research` state | Render markdown/diagrams (bundle-weight rule, stated in the file) |
| `components/Transcript.jsx` | Conversation bubbles; dedupes streaming interims by `lk.segment_id` | — |
| `components/Composer.jsx` | Text input only | Contain mic control (lives on `WaveField`) |
| `components/WaveField.jsx` | The orb's WebGL visualiser and the mic button in one | — |

### `tests/`

| File | Owns |
|---|---|
| `fakes.py` | `FakeSpeaker`/`FakeScreen`/`FakePool`, `build()`, `feed()`, `level_event()` |
| `test_conductor.py` | One test per `DESIGN.md` acceptance check, the Mode-A cascade test, `PlanFrameTests` (raw frame → event). 4 skipped (§8) |
| `test_memory.py` | `Memory`, plan-pending bookkeeping, `read_retained` |
| `test_attention.py` | Push-to-talk gate + command regex |
| `test_sse.py` | SSE parser only |
| `test_providers.py` | Every registered provider resolves offline, declares capability flags, has env defaults — never calls `build()` |
| `live/` | `live_stt.py`, `live_tts.py`, `live_helpers.py` — real `build()` against real servers/endpoints. Filenames deliberately don't start with `test_`, so default discovery skips them (§5) |
| `fixtures/sample_ja.wav` | The live-test fixture |

### `scripts/`, `docs/`, root

| File | Owns |
|---|---|
| `scripts/build_asr_server.sh` | Vendors + builds NeMo-Speech.cpp for `stt/nemotron.py`'s `serve()` |
| `scripts/setup_https_livekit.sh` | One-shot env/cert/LiveKit/Caddy bootstrap |
| `docs/DESIGN.md` | Architecture — read first (has a stale section, see §9) |
| `docs/OPERATIONS.md` | Runbook: ports, start/stop order, troubleshooting |
| `docs/SETUP.md` | Fresh-machine setup, provider swap cheat sheet |
| `docs/REORGANIZATION.md` | The historical move to this layout; provider contracts in signature form |
| `pyproject.toml` | Deps, `[project.scripts]` (`wiki-agent`/`wiki-asr`/`wiki-tts`/`wiki-rag-stub`), hatchling build |
| `.env` / `.env.example` | Deployment config; `.env` has the real hosts, never commit secrets beyond what's already tracked |
| `Caddyfile`, `docker-compose.caddy.yml`, `livekit.yaml` | TLS termination + LiveKit server, Docker |
| `plan.md`, `pre_branch_plan.md` | The design docs for the two quieting modes — mined for §7/§9 below |

---

## 2. Control flow

```
 browser mic/text ─┐                                  ┌─> Speaker.start_* (TTS)
 LiveKit callbacks ─┼─> app/runtime/producers.py ──┐   │      (uninterruptible; §4)
 SSE research frame ┤   (translate, never decide)  │   │
 (ResearchRun task) ┘                               v   │
                                             ┌── inbox: asyncio.Queue ──┐
                                             │  (single consumer)       │
                                             v                          │
                                   Conductor.run(): while True:         │
                                     event = await inbox.get()          │
                                     await self.handle(event)  ─────────┤ owns: Attention, Speaker,
                                     await self.speak_next()   ─────────┘ ResearchPool, Memory, Screen
                                             │
                          screen.publish_research({...}, agent_run_id=…)
                                             │
                                             v
                                frontend/src/lib/research.js reducer
                                (rejects frames from another agent_run_id)
```

| Hop | File · symbol |
|---|---|
| Spoken/typed turn → inbox event | `app/runtime/producers.py::attach` (`_on_transcript`, `_on_text_input`) posts `UserSaidText` |
| Single decision point | `app/core/conductor.py::Conductor.handle` — one `if isinstance(event, …)` per event type |
| Accepted turn → command or LLM | `Conductor.handle_user_text` → `Attention.accept` → `stop`/`repeat`/`continue`/`close`/`none`; `none` calls `Speaker.start_reply` |
| Priority ladder, after every event | `Conductor.speak_next` — pending queue, then foreground partial, then foreground new (see §4: background is never auto-spoken on this branch) |
| Research request → SSE run | `app/agent/tools.py::research_wiki` posts `ResearchRequested` → `Conductor.start_research` → `app/rag/llm_wiki.py::ResearchPool.start` |
| Research streams back | `ResearchRun._consume`/`_on_frame` posts `PlanReady`/`PlanRevised`/`LevelReady`/`ResearchProgress`/`ResearchFinished`/`ResearchFailed` into the inbox |
| Frames mirrored to the browser | `Conductor.handle` (the `ResearchProgress` branch) → `app/core/screen.py::Screen.publish_research`, stamped `agent_run_id` |

---

## 3. Symptom → file table

| Symptom | Open this first |
|---|---|
| Assistant talks too much / repeats a fact | `app/core/conductor.py::Conductor.report` (`may_skip`, `_is_no_information_result`) and `app/agent/prompts.py::report_instructions`'s `skip`/`spoken_so_far` blocks |
| It announces a search twice (「調べます」then a plan preview) | The plan preview is already removed on this branch — `app/core/conductor.py`'s `PlanReady` handler, the commented `self.pending.append(...)` block (see §9 P2a). If this symptom is back, someone uncommented it |
| One question still produces two spoken preambles (model's own + the fixed notice) | `app/core/conductor.py::start_research` (unconditional `Pending("notice", prompts.NOTICE_RESEARCHING, …)`) vs. the model's own preamble suppressed by `app/agent/prompts.py::ASSISTANT_INSTRUCTIONS`'s last sentence. `pre_branch_plan.md` §P2b–d proposed rotating/dropping the notice; **neither was taken** on this branch — `NOTICE_RESEARCHING` is still one fixed sentence |
| Greeting fires on every page load | `app/runtime/entrypoint.py` — `conductor.queue_notice(prompts.GREETING)` / `inbox.put_nowait(IdleTick())` are commented out on purpose (§9 P1); `/token` mints a fresh room every load (`app/web/http.py::token`) so an active greeting would fire every time |
| Research panel shows a stale/previous run | `frontend/src/lib/research.js::researchReducer`'s `agent_run_id` guard; `app/core/conductor.py`'s stamping on every `screen.publish_research` call (`ResearchProgress`, `start_research`, the `ResearchFailed`→retry branch) |
| Panel freezes mid-run and never recovers | A retry that doesn't republish `ask` — `app/core/conductor.py`'s `ResearchFailed` branch (`can_retry`/`retry`), tested by `tests/test_conductor.py::test_a_retry_republishes_the_ask_frame_with_the_run_id` |
| Panel's question/plan update but level bodies never fill in | `app/core/screen.py::Screen._publish` — check stdout for `[SCREEN DROP]` (LiveKit's reliable data channel caps a packet near 15 KB) |
| A follow-up opens a redundant research run instead of reading memory | `app/core/memory.py::Memory.find`/`await_pending` (must never say "not found" while any level exists) and `app/agent/prompts.py::ASSISTANT_INSTRUCTIONS`'s follow-up routing paragraph |
| A background finding is never mentioned even though it arrived | By design on this codebase now, not a bug — `Conductor.speak_next` only auto-reports the **foreground** ladder; a background level is retained but only reachable via `read_result` (§9 contradicts `DESIGN.md` §5.3 — see there) |
| Speech overlaps the user, or cuts off wrong | `app/core/conductor.py::interrupt_current` and the `UserStartedSpeaking` branch of `handle` (OPEN → interrupt, DORMANT → duck); `app/core/speaker.py::Speaker.interrupt/duck/unduck` |
| Agent is present (participant in room) but deaf | `app/runtime/producers.py`'s `_on_session_error`/`_on_session_close` → `Screen.set_agent_status` → `frontend/src/App.jsx::handleAgentGone`. Check log for `SESSION_ERROR`/`SESSION_CLOSED` (`docs/OPERATIONS.md` §6) |
| Swapping STT/TTS/LLM/RAG | §6 below — one env var + the package `REGISTRY` |
| The `続けて` hand-off is promised but the continuation never comes | Should not happen on this branch by construction — see §7's cascade. If it does, something broke the cascade; start at `app/rag/llm_wiki.py::VOICE_KNOBS["max_levels"]` |

---

## 4. Invariants an editor must not break

1. **Single-consumer inbox, no locks.** `Conductor.run()` is `event = await inbox.get(); await handle(event); await speak_next()` — the only decision point in the program. Anything slow (SSE reads, LLM generation, TTS playout) is a detached task that posts back into the inbox; nothing else may hold state that `handle`/`speak_next` also reads without going through an event.
2. **`StopResponse` in the tools, and always in `on_user_turn_completed`.** `app/agent/tools.py`'s `research_wiki`/`stop_research` raise `StopResponse()` after posting one event, because "a prompt alone cannot reliably stop a model from voicing a speculative 'I'll look that up' before the answer exists" (the tool's own comment). `app/agent/assistant.py::Assistant.on_user_turn_completed` raises it **unconditionally**, every turn — the pipeline's own reply must never reach the user; `Conductor` calls `Speaker.start_reply` explicitly instead.
3. **Foreground vs. background run focus.** `events.FOREGROUND`/`BACKGROUND`; a new question moves the previous foreground run to background (`Conductor.start_research` → `ResearchPool.move_to_background` + `Memory.set_focus`). A background run is **never cancelled** for being superseded — it keeps streaming and keeps being remembered. On this codebase, as currently implemented, a background level is **never auto-spoken** (`Conductor.speak_next` only walks the foreground ladder); it is reachable only through an explicit `read_result` follow-up. This is a real change from `DESIGN.md` §5.3's six-step ladder — see §9.
4. **What `SILENT` means in `Memory`.** The level exists, will not be spoken, and stays forever reachable via `Memory.find`/`read_result`. Not deletion. Set by: `Conductor.report`'s no-new-information skip, `SpeechFinished` with empty `spoken_text` (the report LLM pass declined to speak it), and `Conductor.stop_everything` (every `NEW`/`PARTIAL` level → `SILENT`).
5. **Watchdog/retry budget vs. the backend's own stage budget.** `RAG_PLAN_TIMEOUT_SECONDS` (falls back to `RAG_INITIAL_PLAN_TIMEOUT_SECONDS`, this deployment's `.env` sets it to `10`) bounds the wait for the `plan` frame; `RAG_LEVEL_TIMEOUT_SECONDS` (`.env`: `150`, code default `20`) bounds the gap between level frames once planning starts; `RAG_STREAM_MAX_RETRIES` (`1` ⇒ two attempts total) is spent in `ResearchPool.can_retry`/`retry`. `VOICE_KNOBS["deadline_seconds"]` (`600`, in `app/rag/llm_wiki.py`) is the *backend's* own per-run budget, sent in the request body, not a client timeout. If the client watchdog is tightened below what a legitimate stage can take, a live stage looks like a hang and gets retried — replaying and re-speaking content the user already heard (see `pre_branch_plan.md` Mode B "Risk to watch").
6. **Why mirrored frames carry `agent_run_id`.** The backend's own SSE frames carry no run id and every run reuses `level_1`/`level_2`/`level_3`. `Conductor` stamps the *local* run id on every `screen.publish_research` call (the `ResearchProgress` handler, `start_research`, and the `ResearchFailed`→retry branch) so `frontend/src/lib/research.js`'s reducer can reject a superseded/retried run's straggling frames instead of corrupting the panel — this is `pre_branch_plan.md` §P3's fix, already applied on this branch.
7. **Why speeches are created uninterruptible.** `app/runtime/entrypoint.py`'s `turn_handling={"interruption": {"discard_audio_if_uninterruptible": False}}` plus every `Speaker.start_*` call passing `allow_interruptions=False`: so the Conductor — not the LiveKit pipeline — decides what a nearby voice means. Otherwise LiveKit substitutes silence into the STT while an "interruptible" speech plays, making a real barge-in impossible to hear (the entrypoint's own comment).

---

## 5. How to run things

Full runbook: `docs/OPERATIONS.md`. Two facts a fresh agent will get wrong:

- **`uv run pytest` cannot install here.** `uv.lock` pins `onnxruntime==1.28.0`, which ships wheels only for `macosx_14_0_arm64`+ — nothing for this machine's macOS 13. Tests were actually run from a separately built venv that resolved `onnxruntime==1.23.2` instead. `tests/live/` holds live-only helpers (`live_stt.py`, `live_tts.py`, `live_helpers.py`) that are **not** collected by default discovery — none of them match the `test_*.py` pattern, on purpose (`tests/live/README.md`).
- **`app/web/http.py` serves `frontend/dist`, not `frontend/src`.** A frontend change is invisible until `cd frontend && npm run build` runs again.

```bash
python -m unittest discover -s tests -t . -q          # offline suite (once the venv above is active)
python -m unittest discover -s tests/live -t . -p "live_*.py" -q   # live provider tests, explicit pattern
cd frontend && npm run build                            # required after any frontend/src change
```

---

## 6. Provider seams

| Layer | Registry symbol | Selector env var | File |
|---|---|---|---|
| STT | `app.stt.REGISTRY` / `get_provider` | `STT_PROVIDER` (`nemotron`, `qwen`, `voxtral`) | `app/stt/__init__.py` |
| TTS | `app.tts.REGISTRY` / `get_provider` | `TTS_PROVIDER` (`supertonic`, `qwen3`) | `app/tts/__init__.py` |
| LLM | `app.llm.REGISTRY` / `get_provider` | `LLM_PROVIDER` (`openai_compatible`) | `app/llm/__init__.py` |
| RAG | `app.rag.REGISTRY` / `get_backend` | `RAG_PROVIDER` (`llm_wiki`) | `app/rag/__init__.py` |

This deployment's `.env` currently runs `STT_PROVIDER=qwen`, `TTS_PROVIDER=qwen3` — **not** the code defaults (`nemotron`/`supertonic`). Check `.env` before assuming the default engine is what's live. Per-provider env blocks and swap steps: `app/stt/README.md`, `app/tts/README.md`, `app/llm/README.md`, `app/rag/README.md` (do not duplicate here).

---

## 7. What this branch is: `shallow-only` (Mode A of `plan.md` §2)

**The one line that defines the mode** — `app/rag/llm_wiki.py::VOICE_KNOBS["max_levels"] = 1` (the old value, `3`, sits commented directly above it). With it set to `1`, the backend plans and runs only its `fast` stage; the `deep` (3-hop subgraph read) and `anticipation` (look up terms the answer mentioned but didn't explain) stages never run, and neither does the sufficiency-gate LLM call that would decide to add them.

**The cascade** — verified against this branch's code, each link real:

1. Backend plans one stage → no `plan_update` frame is ever emitted for this run.
2. `ResearchRun.planned_levels` (in `app/rag/llm_wiki.py`) therefore never grows past length 1, and `Memory.note_plan`/`Conductor`'s bookkeeping never sees a second stage.
3. `Conductor.position_of` (`app/core/conductor.py`) computes `next_objective = ""` for that single level, because there is no `index + 1` entry in `planned`.
4. `app/agent/prompts.py::_bridge_rules` receives an empty `next_objective` and takes its first branch — 「最後の段階では次の調査を予告せず、質問への答えを短く締めてください。」— instead of the 「続けて、〇〇を確認します」 hand-off.

**The guarding test:** `tests/test_conductor.py::test_a_single_stage_plan_closes_short_with_no_hand_off` — its own leading comment spells out this exact cascade and it asserts the hand-off phrase is absent from the report prompt. An editor "fixing" any link in the chain above (e.g. re-adding `plan_update` handling that fabricates a synthetic next stage, or changing `position_of`'s empty-plan behavior) will make this test fail, which is the point.

**Known cost, stated plainly:** open-ended questions ("which functions do X", "how do A and B differ") get only what the one shallow stage found, with no path to more except the user asking again. That is the trade this mode buys, deliberately.

**Optional steps from `plan.md` §2 not taken on this branch:** A3 (shortening `NOTICE_RESEARCHING` to `"調べます。"`) — the notice is still the original, longer sentence. A2 (dropping the plan preview) **was** taken — see §9.

**To revert to full research (`plan.md` §2 A1):** in `app/rag/llm_wiki.py`, comment out `"max_levels": 1,` and uncomment `"max_levels": 3,`. That undoes the whole cascade above by itself; A2/A3 are independent and can stay either way.

**What `deep-silent` (Mode B) does instead** (a sibling branch, not this one): keeps `max_levels = 2` (fast + deep, anticipation dropped outright), speaks only the first level, retains the rest silently, and offers a one-line "want more detail?" at the end instead of narrating a hand-off. See `plan.md` §3 for the full mechanism if working on that branch instead.

---

## 8. Deliberately commented out — do not delete

This codebase prefers commenting to deleting so a change reverts by uncommenting. A tidy-up pass that removes any of these silently changes behavior:

| Where | What | Restores |
|---|---|---|
| `app/runtime/entrypoint.py` | `conductor.queue_notice(prompts.GREETING)` and the `inbox.put_nowait(IdleTick())` right after it | The startup greeting (`pre_branch_plan.md` P1 — disabled because `/token` mints a fresh room every reload, so it fired on every page load) |
| `app/core/conductor.py`, `PlanReady` branch | The `if run.focus == FOREGROUND and event.planned_levels: self.pending.append(Pending("prompt", prompts.plan_preview_instructions(...)))` block | The spoken plan preview (`pre_branch_plan.md` P2a / `plan.md` A2 — removed because it said a second time what the model's own preamble already said) |
| `app/rag/llm_wiki.py`, `VOICE_KNOBS` | `# "max_levels": 3,` | Full three-stage research (§7 — the whole shallow-only mode hinges on this one line staying commented) |

**Also unreachable, but intentionally kept:** `app/agent/prompts.py::plan_preview_instructions` — a fully live function with zero call sites now that the block above is commented out. It is not dead code to delete; it is what the commented block calls if uncommented, and it still has its own (skipped) tests.

**Tests skipped with `@unittest.skip`, all in `tests/test_conductor.py`, all `"plan preview removed; see pre_branch_plan.md P2a"`:**

- `test_plan_preview_only_for_the_foreground_run`
- `test_queued_background_plan_preview_is_discarded`
- `test_a_one_stage_plan_is_previewed_without_promising_a_next_step`
- `test_the_spoken_preview_does_not_commit_to_a_stage_count`

All four exercise `plan_preview_instructions`/the commented `Pending("prompt", ...)` call. Un-skip them only alongside restoring the P2a block above.

---

## 9. Where this file contradicts `DESIGN.md` — reported, not silently worked around

- **`DESIGN.md` §4.1** describes `Attention` with wake words (`モーヴィ`/`movi`/…), a 20s idle timeout, and a 5s post-report reply window. The actual `app/core/attention.py` has **none of these** — it is a pure push-to-talk toggle (orb button held, or typed text), with no wake word to strip and no timeout at all. Confirmed by the module's own docstring ("There is no wake word... no idle timeout, no reply window") and by `tests/test_attention.py::test_orb_stays_open_until_pressed_again` ("no idle timeout: a long silence must not close it"). This is a full redesign of Attention since `DESIGN.md` was written, not a bug.
- **`DESIGN.md` §5.3**'s `speak_next` ladder documents six rules, including two for background levels (partial-resume-with-attribution, then new-if-still-relevant, with an `is_still_relevant`/`attribute=True` mechanism). The current `Conductor.speak_next` (`app/core/conductor.py`) implements only the first four — it explicitly stops after the foreground ladder ("Background findings remain in Memory for an explicit `read_result` follow-up, but a newer question must never cause them to be spoken unprompted"). `Conductor.report`'s `attribute` parameter and `prompts.report_instructions`'s attribution lead-in still exist but have no live caller. Confirmed intentional by `tests/test_conductor.py::test_background_partial_is_not_resumed_without_an_explicit_request` and `::test_background_finding_is_retained_but_not_spoken`.
- **`events.py::IdleTick`**'s docstring says "Drives the attention timeout and nothing else." Since `Attention` no longer has a timeout (see above), `Conductor.handle`'s `IdleTick` branch does not call anything on `Attention` — it only unducks a ducked speaker and republishes the current attention state. The docstring is stale.

None of the above needed to be verified against `plan.md`/`pre_branch_plan.md` claims beyond what's cited — everything else in this document was checked directly against the code in this worktree.
