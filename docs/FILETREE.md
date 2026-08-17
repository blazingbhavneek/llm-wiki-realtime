# FILETREE.md — navigation map

A Japanese-speaking voice agent that answers questions from an internal wiki over LiveKit: a browser
mic in, streaming ASR → one decision loop → an LLM with three tools → a wiki SSE research backend →
TTS out, with a browser side panel mirroring the same research stream. This file is the map for
finding the right symbol fast; the *why* lives in `docs/DESIGN.md` (architecture), `docs/OPERATIONS.md`
(day-to-day runbook) and `docs/SETUP.md` (new-machine bootstrap) — read those for rationale, this file
only points.

Generated/vendored and skipped below: `.venv/`, `frontend/node_modules/`, `frontend/dist/`,
`__pycache__/`, `uv.lock`, `frontend/package-lock.json`, `.pytest_cache/`, `models/`, `vendor/`, `certs/`.

---

## 1. Annotated tree

### `app/core/` — decisions only. No network, no provider imports (only `speaker.py` imports a LiveKit type).

| File | Owns | Must never |
|---|---|---|
| `conductor.py` | `Conductor` — the single event loop (`run`), the decision table (`handle`), the speech priority ladder (`speak_next`). Every behavior in the program is reachable from this file. | Block on I/O inside `handle`/`speak_next`; add a second place that decides what to say. |
| `events.py` | The dataclass vocabulary producers push into the inbox — 15 frozen dataclasses, no behavior. | Gain a method. If a behavior can't be expressed as one of these, it doesn't happen. |
| `attention.py` | `Attention` — orb-toggle gate (`OPEN`/`DORMANT`) and the plain-regex spoken commands (`stop`/`repeat`/`continue`/`close`). **No wake word, no idle timeout, no reply window** — see §9. | Decide whether to *speak*; it only gates whether incoming speech is a turn. |
| `memory.py` | `Memory`, `LevelResult`, `PendingObjective` — every research level ever received (spoken or not) and every objective still owed. | Call back into `ResearchPool`; it only knows the `RunLike` protocol. |
| `speaker.py` | `Speaker` — the single speech slot. Only file that touches `AgentSession`/TTS directly. | Decide *what* to say; `start_*` only launches and tracks. |
| `screen.py` | `Screen` — the one-way data-channel mirror to the browser (`research`, `attention` topics). | Hold state or decide; it is a wire. Drops are now logged (`[SCREEN DROP]`), not swallowed. |

### `app/agent/` — what the LLM is told and what it may call

| File | Owns | Must never |
|---|---|---|
| `assistant.py` | `Assistant(Agent)` — instructions + 3 tools attached; `on_user_turn_completed` unconditionally raises `StopResponse()`. | Let the pipeline's own `generate_reply` produce a spoken turn — Attention/Conductor always decide that instead. |
| `tools.py` | `research_wiki`, `read_result`, `stop_research`, `AssistantDeps`, `read_retained` (the plain-function body of `read_result`, unit-testable without a session). | Do work itself — a tool posts one event into the inbox and returns/raises. |
| `prompts.py` | Every Japanese string: `GREETING`, `NOTICE_*`, `ASSISTANT_INSTRUCTIONS` (the casual-vs-research router), `plan_preview_instructions` (unreachable, see §8), `report_instructions` + `_bridge_rules` (the level → speech prompt and hand-off logic). | Contain logic that changes *program* state — it only builds strings. |

### `app/stt/`, `app/tts/`, `app/llm/`, `app/rag/` — one external dependency each, registry + `base.py` + one file per engine + `README.md`

| Package | Registry symbol | Env selector | Providers |
|---|---|---|---|
| `app/stt/` | `app.stt.REGISTRY`, `build_stt(vad=...)` | `STT_PROVIDER` (default `nemotron`) | `nemotron.py` (self-hosted WebSocket, `serve()` = `python -m app.stt.nemotron`), `qwen.py` (vLLM, forced `ja`), `voxtral.py` (vLLM, batch or realtime via `STT_USE_REALTIME`) |
| `app/tts/` | `app.tts.REGISTRY`, `build_tts_pair()` | `TTS_PROVIDER` (default `supertonic`) | `supertonic.py` (self-hosted ONNX, `serve()` = `python -m app.tts.supertonic`), `qwen3.py` (vLLM, `pcm`@24kHz) |
| `app/llm/` | `app.llm.REGISTRY`, `build_llm()` | `LLM_PROVIDER` (default `openai_compatible`) | `openai_compatible.py` (llama-server/vLLM/SGLang, anything `/v1/chat/completions`) |
| `app/rag/` | `app.rag.REGISTRY`, `build_research_pool(inbox)` | `RAG_PROVIDER` (default `llm_wiki`) | `llm_wiki.py` (`ResearchRun`/`ResearchPool`, the only file that talks to the real backend); `sse.py` (generic SSE line parser, knows nothing about frame meaning); `placeholder.py` (a **server**, not a registered backend — dummy answers for `python -m app.rag.placeholder`) |

Each `base.py` defines the contract (`STTProvider`/`TTSProvider`/`LLMProvider`, `ResearchBackend`/`ResearchRun` as `Protocol`s) and must never import a concrete provider — registries map to **import strings**, resolved lazily, so an unselected engine's dependencies (e.g. `onnxruntime` for `supertonic`) never load. Full per-engine env tables live in each package's own `README.md` — do not duplicate them here.

### `app/web/` and `app/runtime/` — the only layer that knows every module exists

| File | Owns | Must never |
|---|---|---|
| `web/http.py` | FastAPI `app`, `/health`, `/`, `/token`, and mounts `frontend/dist` as static. | Serve `frontend/src` — it only serves the **built** output; see §5. |
| `web/tokens.py` | `create_room_token` — LiveKit room create, agent dispatch, JWT mint. | — |
| `web/tls.py` | `ensure_local_https_certificate` — local dev cert only; unused when Caddy terminates TLS. | — |
| `runtime/entrypoint.py` | `entrypoint(ctx)` — builds every provider, the `AgentSession`, `Speaker`, `Conductor`, wires the two long-lived tasks (`conductor.run()`, `idle_ticker`). | Contain a decision — it wires and gets out of the way. |
| `runtime/producers.py` | `attach(...)` — every LiveKit/session/room callback, translated 1:1 into an inbox event; `idle_ticker` (1 Hz `IdleTick`). | React to anything itself; a producer never branches on content. |
| `runtime/worker.py` | Process bootstrap: `prewarm` (loads Silero VAD), `main()`/`run_combined_server()` (uvicorn thread + LiveKit worker in one process), proxy scrubbing. | — |

### `app/` root

`config.py` (`Settings.from_env()`, typed, read once — provider-specific settings stay in each provider package instead), `log.py` (`dbg()` — structured stdout, since livekit-agents owns logging config), `timing.py` (`TURN_TIMING=1` latency stopwatch, stages `eos→stt_final→accepted→llm_request→llm_first→audio_out`), `__main__.py` (`python -m app` → `runtime.worker.main`).

### `frontend/src/`

| File | Owns | Must never |
|---|---|---|
| `App.jsx` | The whole UI + LiveKit `Room` lifecycle: orb press → `publishData({type:'listen'})` on the `attention` topic, agent-presence tracking (`agentPresent` ≠ `connected`), transcript text-stream upsert by `lk.segment_id`, dev-mode (`VITE_APP_MODE`) fixture playback. | Treat `connected` as "agent is listening" — always gate on `ready = connected && agentPresent`. |
| `lib/research.js` | `researchReducer` over mirrored SSE frames; `emptyResearch()`; the run-id guard (`event.agent_run_id !== state.agentRunId` → drop, except `ask`); dev demo script (`DEMO_SCRIPT`/`runDemo`). | Trust an unstamped frame's `level_id` as unique across runs — the backend reuses `level_1`/`level_2`/`level_3` every run. |
| `components/ResearchPanel.jsx` | Renders the plan/levels tree. Deliberately no Markdown/diagram rendering. | Add a renderer — it's a status view, not a document viewer. |
| `components/Transcript.jsx` | Chat bubble list, autoscroll, "途中で中断" badge. | — |
| `components/Composer.jsx` | The text input row only — the mic control lives on `WaveField`, not here. | — |
| `components/WaveField.jsx` | The orb: WebGL1 shader visualizer, doubles as the mic button. | Use WebGL2 features / dynamic array indexing — must compile with no fallback path. |
| `lib/audio.js` | `AudioBus` — Web Audio analysis (mic RMS gate that mutes the published track until real speech, agent-track leveling for the orb, dev loopback). | Use `createMediaElementSource` on the agent `<audio>` — it hijacks playback; `MediaStreamSource` only. |
| `main.jsx`, `index.css` | React mount point; global styles/Tailwind tokens. | — |

### `tests/`

| File | Covers |
|---|---|
| `fakes.py` | `FakeSpeaker`/`FakeScreen`/`FakePool`, `build()` (wires a real `Conductor` with fakes), `feed()` (drives `handle` + `speak_next` per event, matching the real loop), `level_event()`, `AsyncLines`. |
| `test_conductor.py` | One test per DESIGN.md acceptance check *as currently implemented* — barge-in, background retention (never auto-spoken), stop-without-LLM, retry + `ask` republish, plan-preview suppression (skipped, see §8), no-information skip logic, plan-revision hand-off honesty. |
| `test_memory.py` | `Memory` read/write surface, `find` fuzzy matching, `await_pending`, `read_retained`/`NO_RETAINED_RESULT`. |
| `test_attention.py` | Orb gate only — confirms no wake-word stripping (`"nothing is stripped now that there is no wake word to remove"`). |
| `test_sse.py` | `iter_sse_events` transport parsing, independent of frame meaning. |
| `test_providers.py` | Every `REGISTRY` entry resolves offline; declares every capability flag its base requires; registry laziness (`qwen3` must not drag `supertonic`/`onnxruntime` into `sys.modules`). Never calls `build()`. |
| `live/` | `live_stt.py`, `live_tts.py`, `live_helpers.py` — **not** collected by `unittest discover -p "test*.py"` or by pytest's default `test_*.py` pattern, on purpose (importing them would pull heavy/network deps into the fast suite). Run explicitly: `python -m unittest discover -s tests/live -t . -p "live_*.py" -q`. |
| `fixtures/` | `sample_ja.wav` — synthesized fixture speech for the live STT suite; regeneration recipe in its own `README.md`. |

### `scripts/`

`build_asr_server.sh` — vendors + builds NVIDIA/NeMo-Speech.cpp for `nemotron` (idempotent). `setup_https_livekit.sh` — one-shot bootstrap: `.env` HTTPS/LiveKit values, trustme cert, `docker compose up`, frontend build (idempotent, re-run after any IP change).

### `docs/` and root config

`DESIGN.md` (architecture — **partially stale**, see §9), `OPERATIONS.md` (day-to-day runbook), `SETUP.md` (new-machine bootstrap), `REORGANIZATION.md` (the reorg plan this tree now matches — its own header still says "proposal only, no code changed", which is no longer true). `.env`/`.env.example` (all deployment config, single source — `.env` is *not* gitignored despite `DESIGN.md` §10 recommending it be). `Caddyfile`/`docker-compose.caddy.yml`/`livekit.yaml` (TLS + LiveKit container config). `pyproject.toml` (deps, `[project.scripts]` console entry points, `hatchling` build). `plan.md`/`pre_branch_plan.md` (see §7).

---

## 2. Control flow

```
 mic / typed text            SSE research frames               TTS playout
       │                            │                                │
       ▼                            ▼                                ▼
runtime/producers.py     app/rag/llm_wiki.py::ResearchRun    core/speaker.py::Speaker
 (translate only)          (opens stream, decodes frames)     (start_*/_watch tasks)
       │                            │                                │
       └──────────────┬─────────────┴───────────────┬────────────────┘
                       ▼                             ▼
                  asyncio.Queue  ◄──── single inbox, single consumer
                       │
                       ▼
        core/conductor.py :: Conductor.run()
          while True: event = inbox.get()
                       await self.handle(event)   ← THE decision point
                       await self.speak_next()    ← priority ladder, every event
                       │              │
          ┌────────────┼──────────────┼───────────────┐
          ▼            ▼              ▼                ▼
     Attention      Memory        Speaker           Screen
   (turn gate)   (level store)  (speech slot)   (browser mirror,
                                                  stamps agent_run_id)
```

1. **Turn in.** `runtime/producers.py::attach` registers every LiveKit callback (`user_state_changed`, `user_input_transcribed`, room `data_received` for the orb toggle, room `TextInputEvent`) and turns each into `UserStartedSpeaking`/`UserStoppedSpeaking`/`UserSaidText`/`ListenButtonChanged` on the inbox. Nothing here branches on content.
2. **`Conductor.handle`** (`app/core/conductor.py`) is the single `match`-style `if isinstance` ladder — every event type in `app/core/events.py` has exactly one handler here. Accepted user text goes through `Attention.accept` → `Conductor.handle_user_text`, which resolves plain-regex commands (`stop`/`repeat`/`continue`/`close`) before ever reaching the LLM, then falls through to `Speaker.start_reply`.
3. **Research call.** The LLM's `research_wiki` tool call posts `ResearchRequested` and raises `StopResponse()` (see §4). `Conductor.start_research` backgrounds any current foreground run, opens a new `ResearchRun` via the pool, publishes a synthetic `{"type":"ask", ..., "agent_run_id": run.run_id}` frame to reset the panel immediately, and queues the fixed `NOTICE_RESEARCHING`.
4. **Research streams back.** `ResearchRun._consume` in `app/rag/llm_wiki.py` reads SSE frames (`app/rag/sse.py::iter_sse_events`), and for every frame both (a) pushes a typed event (`PlanReady`/`PlanRevised`/`LevelReady`/`ResearchFailed`/`ResearchFinished`) *and* (b) pushes the raw frame as `ResearchProgress` so the Conductor can mirror it verbatim.
5. **`Conductor.speak_next`** runs after *every* event (not just research ones) — the priority ladder, top wins: (1) speaker already busy → return, (2) user mid-speech → return, (3) the `pending` deque (fixed notices; foreground-only, dropped if their run was superseded), (4) `Memory.next_partial(FOREGROUND)` — resume a cut-off sentence, (5) `Memory.next_new(FOREGROUND)` — the next arrived level. **There is no background-reporting tier** in the current code (see §9) — a backgrounded run's findings sit in `Memory` at whatever state they were in and are only reachable through `read_result`.
6. **Frames to the browser.** `Conductor.handle`'s `ResearchProgress` branch (and the `start_research`/retry publish sites) call `Screen.publish_research({**frame, "agent_run_id": run.run_id})` — **only when `run.focus == FOREGROUND`** — so a superseded run's late frames never pull the panel backward. `frontend/src/lib/research.js::researchReducer` refuses any frame whose `agent_run_id` differs from the run it's currently tracking, except `type: "ask"`, which resets state.

---

## 3. Symptom → file table

| Symptom | Open first | Why |
|---|---|---|
| Assistant talks too much / repeats itself before answering | `app/core/conductor.py::Conductor.start_research` (queues `NOTICE_RESEARCHING`) + `app/agent/assistant.py::Assistant.on_user_turn_completed` | The model's own pre-tool-call preamble ("〜を調べますね") reaches TTS *before* `StopResponse()` fires — `StopResponse` only stops what comes after the tool call, not the sentence in front of it. Run with `LIVEKIT_AGENT_LOG_LEVEL=DEBUG` to see whether a preamble is actually spoken (see `pre_branch_plan.md` P2b) before deciding whether to touch the notice. |
| It announces a search twice | `app/core/conductor.py::Conductor.start_research` (fixed `NOTICE_RESEARCHING`, unconditional) | The plan-preview LLM pass that used to say it a second time is already commented out (`PlanReady` branch, §8) — if you still hear two announcements, the second is the model's own preamble, not this code. |
| A greeting fires on every page load | `app/runtime/entrypoint.py` (near the bottom, both lines commented) | `queue_notice(prompts.GREETING)` + the `IdleTick()` kick are intentionally commented out (§8) — a fresh room is minted per page load (`app/web/http.py::/token`), which used to fire the greeting every reload. |
| The research panel shows a stale/previous run | `frontend/src/lib/research.js::researchReducer` (the `agent_run_id` guard) and `app/core/conductor.py`'s three `screen.publish_research(...)` call sites | Every mirrored frame must carry the local `agent_run_id`; a frame from a backgrounded or superseded run is dropped except `type:"ask"`. If frames are dropped silently, check `[SCREEN DROP]` in the agent log — `Screen._publish` (`app/core/screen.py`) now logs instead of swallowing. |
| The panel freezes mid-run and never recovers | `app/core/conductor.py`'s `ResearchFailed` branch, `app/rag/llm_wiki.py::ResearchRun._watchdog` | A plan/level-gap timeout retries the *whole question* under the same local `run_id`; the retry branch republishes the `ask` frame (`agent_run_id` intact) specifically so the panel doesn't keep rejecting the replayed `level_1`/plan v1 as stale. |
| It re-answers a question it already answered | `app/core/memory.py::Memory.find` (fuzzy match order: exact → substring → token overlap → most recent) and `app/agent/prompts.py::ASSISTANT_INSTRUCTIONS` (the casual/research router text) | `find` is deliberately generous — it almost never returns "not found," because that outcome pushes the LLM into a redundant `research_wiki` call, which is judged the more expensive mistake. |
| A follow-up opens a redundant research run for something already in flight | `app/core/memory.py::Memory.await_pending` / `note_plan`, `app/agent/tools.py::read_retained` (`RESEARCH_IN_FLIGHT` string) | The live plan's still-unarrived objectives are tracked in `Memory._pending`; `read_result` answers "already being looked into" instead of `NO_RETAINED_RESULT`, which is what stops the model from calling `research_wiki` again. |
| Speech overlaps or user is cut off mid-question | `app/core/conductor.py::handle` (`UserStartedSpeaking` branch: interrupt if `OPEN`, duck if `DORMANT`+busy), `app/core/speaker.py::Speaker.interrupt`/`duck` | Two-tier barge-in: addressed speech always wins immediately; unaddressed nearby speech only ducks volume and never interrupts. If reports keep getting cut when they shouldn't, check `Attention.state` transitions in `app/core/attention.py`, not `Speaker`. |
| Speech cuts off and never resumes ("続けて" does nothing) | `app/core/conductor.py::handle_user_text` (`"continue"` branch calls `Memory.last_partial()`, **not focus-scoped**) vs `Conductor.speak_next` (`next_partial(FOREGROUND)` **is** focus-scoped) | By design (`tests/test_conductor.py::test_background_partial_is_not_resumed_without_an_explicit_request`): a PARTIAL level from a *backgrounded* run is found by `last_partial()` but the ladder only auto-resumes a FOREGROUND partial, so a stray 続けて after switching topics can silently do nothing. |
| The orb shows listening/agent-present but the agent never answers ("present but deaf") | `app/runtime/producers.py` (`_on_session_error`/`_on_session_close`), `app/core/screen.py::Screen.set_agent_status`, `frontend/src/App.jsx::handleAgentGone` | LiveKit closes the session after `SESSION_MAX_UNRECOVERABLE_ERRORS` (default 10) consecutive STT/LLM/TTS failures; the orb is drawn from the *browser's own mic state*, so it stays red over a dead agent unless `agent_status` is published. Check the log for `SESSION_ERROR`/`SESSION_CLOSED` (`docs/OPERATIONS.md` §6). |
| STT/TTS/LLM/RAG provider swap misbehaves | `app/{stt,tts,llm,rag}/__init__.py` (`REGISTRY`, `get_provider`), the specific provider file, and that package's `README.md` §7 "gotchas" | Each provider declares capability flags (`native_sample_rate`, `default_response_format`, `finals_are_utterances`, `language_is_locale`, …) that fail as *garbled audio or silent misbehavior*, not as an exception — the READMEs list every gotcha already paid for. |

---

## 4. Invariants an editor must not break

- **Single-consumer inbox, no locks.** Exactly one `await self.inbox.get()` loop (`Conductor.run`). Every producer only ever calls `inbox.put_nowait(...)`; nothing outside `Conductor.handle` is allowed to decide. Adding a second consumer, or a callback that acts instead of posting an event, breaks the whole reasoning model in `docs/DESIGN.md` §1.
- **`StopResponse` in tools is deliberate, and asymmetric.** `research_wiki` and `stop_research` (`app/agent/tools.py`) raise `StopResponse()` after posting their event — a prompt alone cannot reliably stop the model from voicing a speculative "let me check" before the tool's real answer exists. `read_result` does **not** raise it: its return value must feed back into the *same* generation so the model can answer with the retained evidence in one turn (`ASSISTANT_INSTRUCTIONS`: "同じ応答で結論から答えてください"). `Assistant.on_user_turn_completed` also unconditionally raises `StopResponse()` — the pipeline's own reply is *always* suppressed; only `Conductor`-initiated `Speaker.start_reply`/`start_report` may produce spoken output.
- **Foreground vs. background focus is orthogonal to run state.** A backgrounded run (`ResearchPool.move_to_background`) is never cancelled for being superseded — it keeps streaming and keeps being remembered in `Memory`. In the *current* ladder (`Conductor.speak_next`), background levels are never spoken unprompted (contrast `docs/DESIGN.md` §5.3 — see §9 below); they exist purely so `read_result` can answer a later explicit follow-up.
- **`SILENT` in `Memory` is overloaded — three unrelated reasons land in the same state**, and all three stay `find`-able forever: (1) `Conductor.report` skipping a no-new-information boilerplate result once the question is already answered (`_is_no_information_result` + `may_skip`); (2) `SpeechFinished` with empty `spoken_text` — the report LLM pass itself judged the level not worth saying; (3) `Conductor.stop_everything` mass-silencing every outstanding `NEW`/`PARTIAL` level on a user "止めて". Do not assume `SILENT` means "background and stale" — check *why* at the call site.
- **The watchdog/retry relationship.** `RAG_LEVEL_TIMEOUT_SECONDS` (gap-between-frames watchdog, `.env` currently `150`) and `RAG_STREAM_MAX_RETRIES` (currently `1`, so 2 attempts total) live in `app/rag/llm_wiki.py`; a retry re-runs the *entire question* from `plan` again under a fresh backend run id but the same local `run_id`. This is safe against double-speaking only because `Memory.remember`'s fingerprint dedup (`(run_id, text)`) drops an exact-text replay — but it is **not** safe against timing out again if the client's timeout is shorter than the backend's own per-stage budget (subagent counts/steps/compile-wait/deep-deadline, all in `VOICE_KNOBS`, `llm_wiki.py`). Raising one without checking the other reproduces the failure `plan.md` §3 "Risk to watch" describes.
- **`agent_run_id` on every mirrored frame** exists because the backend's own SSE frames carry no run identity and every run reuses the literal ids `level_1`/`level_2`/`level_3` — see §3 "stale panel" and `app/core/conductor.py`'s three `screen.publish_research` sites plus `frontend/src/lib/research.js`'s guard.
- **Every speech is created uninterruptible** (`allow_interruptions=False` on all three `Speaker.start_*` calls, plus `turn_handling.interruption.discard_audio_if_uninterruptible=False` in `runtime/entrypoint.py`). This is intentional: LiveKit otherwise substitutes silence into the STT stream while an "uninterruptible" speech plays, which would make a real barge-in inaudible. Interruption is instead driven entirely by `Conductor.interrupt_current` reacting to `UserStartedSpeaking`/accepted text — never by the pipeline's own turn-detection.

---

## 5. Running things — the two facts a fresh agent will get wrong

1. **`uv run pytest` cannot install on this machine.** `uv.lock` pins `onnxruntime==1.28.0`, whose wheels cover `macosx_14_0_arm64`+ only — this machine is macOS 13, so `uv sync` (which `uv run` performs implicitly) fails before pytest even starts, and `pytest` itself is not a declared project dependency either way. Tests were instead run from a separately built venv that resolved `onnxruntime==1.23.2`. `tests/live/` holds live-only helpers (`live_helpers.py`, `live_stt.py`, `live_tts.py`) that are **not** collected by any default test runner — none of their filenames start with `test`, on purpose (see `tests/live/README.md`); run them explicitly with `python -m unittest discover -s tests/live -t . -p "live_*.py" -q`.
2. **`app/web/http.py` serves `frontend/dist`, not the sources.** A frontend edit under `frontend/src/` is invisible until `cd frontend && npm run build` regenerates `frontend/dist`. There is no dev-server proxy wired into the agent process for this repo's normal run mode.

Current `.env` on this checkout runs `STT_PROVIDER=qwen`, `TTS_PROVIDER=qwen3` (both vLLM-hosted — no local `nemotron`/`supertonic` server needed here), `LLM_PROVIDER=openai_compatible`, `RAG_PROVIDER=llm_wiki`; `RAG_LEVEL_TIMEOUT_SECONDS=150`, `RAG_STREAM_MAX_RETRIES=1`. Full boot order and troubleshooting: `docs/OPERATIONS.md` §1, §6.

---

## 6. Branch context

This branch (`realtime-asr-and-orb-attention`) is the **shared base** both quieting modes branch from (`plan.md`, commit `f3dac8a`). `pre_branch_plan.md`'s three fixes (P1 no greeting, P2a no plan preview, P3 `agent_run_id` stamping) are **already applied here** — verified against `entrypoint.py`, `conductor.py`, and `research.js`/`App.jsx`.

Two branches exist off this one, per `plan.md`:

- **`shallow-only` (Mode A):** client-only change — `VOICE_KNOBS["max_levels"]` drops from `3` to `1` in `app/rag/llm_wiki.py`, so the backend's deep/anticipation stages are never requested. Answers once from the shallow pass, then stops; no path to "more" except asking again.
- **`deep-silent` (Mode B):** shrinks the deep-stage budget, drops the anticipation stage, marks levels after the first as `SILENT` instead of speaking them, and adds an end-of-run offer ("さらに詳しい内容も調べてあります。お聴きになりますか？") with a regex + LLM yes-path to promote the silenced levels back to `NEW`.

`plan.md` §4 describes a **shared backend patch to `llm-wiki-dist`** (carrying the sufficiency gate's `missing`-gap string through to the deep stage's prompt instead of discarding it) that both branches depend on for full effect but that is **not yet applied** to `llm-wiki-dist` as of this writing.

---

## 7. Deliberately commented out — do not delete in a tidy-up pass

| Location | What | Why kept |
|---|---|---|
| `app/runtime/entrypoint.py`, near the end | `conductor.queue_notice(prompts.GREETING)` + `inbox.put_nowait(IdleTick())` | Restores the on-connect greeting (P1). `prompts.GREETING` itself is still defined and otherwise unused. |
| `app/core/conductor.py`, `PlanReady` branch | The `self.pending.append(Pending("prompt", prompts.plan_preview_instructions(...)))` block | Restores the spoken plan-preview turn (P2a / Mode A's A2 / Mode B's B6). `self.memory.note_plan(run, ...)` just above it is **not** commented — removing that instead breaks `read_result`'s "already being researched" answer. |
| `app/agent/prompts.py::plan_preview_instructions` | The whole function | Currently **unreachable** — its only call site is the commented block above. A dead-code sweep would delete the one thing that makes restoring the preview a one-line uncomment. |
| `tests/test_conductor.py` | `test_plan_preview_only_for_the_foreground_run`, `test_queued_background_plan_preview_is_discarded` | `@unittest.skip("plan preview removed; see pre_branch_plan.md P2a")` — kept, not deleted, so re-enabling the preview is "un-skip" not "re-write". |

---

## 8. Where this file disagrees with existing docs

Verified against the current source, and worth flagging back rather than silently overriding:

- **`docs/DESIGN.md` §4.1 (Attention) is stale.** It describes wake words (`モーヴィ`/`movi`/…), a 20s idle timeout, and a 5s post-report reply window. The current `app/core/attention.py` implements none of this — its own docstring states "There is no wake word, and nothing else opens or closes attention on the user's behalf - no idle timeout, no reply window," and `tests/test_attention.py` explicitly asserts "nothing is stripped now that there is no wake word to remove." The design moved to push-to-talk-only (the browser orb) at some point after `DESIGN.md` was written.
- **`docs/DESIGN.md` §5.3 (`speak_next` ladder) is stale.** It lists six priority tiers including two for background levels (partial-with-attribution, new-with-relevance-check via an `is_still_relevant` method). The current `Conductor.speak_next` has four tiers and stops after foreground-new; there is no `is_still_relevant` method anywhere in `app/core/conductor.py`, and background levels are never spoken unprompted (confirmed by `tests/test_conductor.py::test_background_finding_is_retained_but_not_spoken` and `::test_background_partial_is_not_resumed_without_an_explicit_request`).
- **`docs/DESIGN.md` §3 (event vocabulary) is incomplete.** It lists 12 events; the current `app/core/events.py` has 15 — `UserStoppedSpeaking`, `ResearchStopRequested`, `PlanRevised`, and `ResearchProgress` are undocumented there, and `SpeechFinished`/`SpeechInterrupted` now carry `spoken_text` (with `spoken_char_count` as a derived property on the latter) rather than the shapes shown.
- **`docs/DESIGN.md` §2/§11 describe a flat, single-file layout** (`conductor.py`, `speaker.py`, `research.py`, `agent.py`, `server.py` at the repo root). The repository has since been reorganized to match `docs/REORGANIZATION.md`'s target tree (confirmed file-by-file above) — but `REORGANIZATION.md` itself still opens with "Status: proposal only. No code has been changed," which is no longer accurate either.

Everything else cross-checked against `plan.md` and `pre_branch_plan.md` (the specific line numbers, prompt strings, and function names they cite) matched the current source at the time of writing, except where §6/§7 above note a plan step not yet taken.

Not verified / left out: exact current behavior of `app/stt/qwen.py` and `app/stt/voxtral.py` under a live vLLM endpoint (no network access from this pass — see each package's `README.md` §7 for the documented gotchas instead), and the precise commands used to build the separately-resolved `onnxruntime==1.23.2` test venv referenced in §5 (told, not independently reproduced here).
