# `app/rag/` — the wiki research backend

## 1. The contract

`build_research_pool(inbox)` returns the object the Conductor holds as
`self.pool`: a `ResearchBackend`. The Conductor asks it to `start(question)`,
`get(run_id)`, `foreground_run()`, `move_to_background(run)`, `can_retry(run)` /
`retry(run)` and `cancel_all()`; it never touches the network itself. The
backend's runs push `ResearchProgress` / `PlanReady` / `PlanRevised` /
`LevelReady` / `ResearchFinished` / `ResearchFailed` into the inbox and decide
nothing —
whether a finding is spoken, and whether a failure is retried, is the
Conductor's call.

Both halves are written down as `typing.Protocol`s in `base.py`
(`ResearchRun`, `ResearchBackend`). `app.core.conductor` imports them under
`TYPE_CHECKING` only, so the core layer never imports a provider at runtime —
`app/runtime/` builds the pool and hands it over. `base.py` imports nothing but
`typing`.

`sse.py` is the transport underneath: a line-oriented Server-Sent Events parser
that knows nothing about what the frames mean. `llm_wiki.py` is the only file
that talks to the real backend.

## 2. Choosing one

| what you switch | variable | values | default |
|---|---|---|---|
| wiki backend | `RAG_PROVIDER` | `llm_wiki` | `llm_wiki` |

There is one backend today, so the variable exists for the second one. The
registry maps names to **import strings**, resolved lazily, so importing
`app.rag` does not pull `httpx` into a process that only wants the protocols.

The `llm_wiki` env block:

| variable | default | what it does |
|---|---|---|
| `LLM_WIKI_BASE_URL` | `http://10.160.152.38:8000` | host of the wiki service; trailing `/` stripped |
| `LLM_WIKI_PREFIX` | `/llm-wiki` | path prefix the service is mounted under |
| `LLM_WIKI_DATABASE` | `moove_wiki` | which wiki to ask |
| `LLM_WIKI_REALTIME_URL` | — | overrides the **stream** URL wholesale; see §7 |
| `RAG_PLAN_TIMEOUT_SECONDS` | `5` | watchdog budget until the `plan` frame arrives |
| `RAG_INITIAL_PLAN_TIMEOUT_SECONDS` | `5` | older name for the same thing; used only when `RAG_PLAN_TIMEOUT_SECONDS` is unset |
| `RAG_LEVEL_TIMEOUT_SECONDS` | `20` | watchdog budget between two frames once planning is done |
| `RAG_STREAM_MAX_RETRIES` | `1` | how many extra attempts `can_retry` will grant one question |

The stream URL is
`{BASE_URL}/{PREFIX}/{DATABASE}/api/ask/realtime/stream`, and cancel is
`{BASE_URL}/{PREFIX}/{DATABASE}/api/agent-runs/{run_id}/stop`.

Request knobs live in `VOICE_KNOBS` in `llm_wiki.py`, sent with every question.
They are the endpoint's own defaults, deliberately — the comment above them
records why the previous, smaller values were wrong.

## 3. The frames

One backend, so the table worth having is the endpoint's SSE vocabulary.
**Every** frame, including the ones below that do nothing else, also produces a
`ResearchProgress(run_id, frame)` — that is what feeds the on-screen trace.

| frame | what the run does with it | inbox event |
|---|---|---|
| `run` | records `run_id` as `remote_run_id`, which is what a later cancel is addressed to | `ResearchProgress` |
| `plan` | marks the plan seen (the watchdog switches to the level budget), stores `levels`, state → `researching` | `ResearchProgress`, `PlanReady` |
| `plan_update` | replaces `planned_levels` **only if `version` is higher**; a stale re-broadcast is ignored | `ResearchProgress`, `PlanRevised` — only when the revision was accepted |
| `level_start` | nothing but feed the watchdog — proof the backend is alive between levels | `ResearchProgress` |
| `level` | counts one received level | `ResearchProgress`, `LevelReady` |
| `done` | state → `done`, ends the stream | `ResearchProgress`, `ResearchFinished(status)` |
| `error` | state → `failed`, ends the stream | `ResearchProgress`, `ResearchFailed("stream_error: …")` |
| `cancelled` | state → `cancelled`, ends the stream | `ResearchProgress`, `ResearchFinished("partial")` |

Two endings are not frames at all. If the stream closes with no terminal frame,
the run reports `ResearchFinished("partial")` when at least one `level` arrived
and `ResearchFailed("stream_error: closed early")` when none did. If a watchdog
fires, the run reports `ResearchFailed("plan_timeout")` or
`ResearchFailed("level_gap")` and tells the backend to stop.

## 4. Adding a second backend

Two Protocols and one registry line:

1. copy `llm_wiki.py` to `<name>.py` and implement the two Protocols in
   `base.py` — a run class with `run_id` / `question` / `focus` /
   `planned_levels` / `superseded_at_report` / `finished` / `start()` /
   `cancel()`, and a pool class with the eight methods plus a `runs` dict;
2. push the same inbox events from `app.core.events`, with the same meanings —
   the Conductor's behaviour is defined by them, not by your transport;
3. add one line to `REGISTRY` in `__init__.py`, mapping a name to
   `"app.rag.<name>:<Class>"`;
4. document its env block in this README and in `.env.example`.

`ResearchBackend` is `@runtime_checkable`, so `isinstance(pool,
ResearchBackend)` is a cheap smoke test in `tests/test_providers.py`.

`llm_wiki.py` exports its pool under two names: the class is still
`ResearchPool` (as every call site has always called it) and `LLMWikiBackend`
is an alias, which is the name the registry uses.

## 5. The placeholder is a server, not a backend

```
uv run python -m app.rag.placeholder     # :8005
```

`placeholder.py` is **not a registered backend and must never become one.** It
is a local FastAPI stand-in for the *remote service* — it serves the same
`run -> plan -> level_start -> level -> done` SSE shape and the same stop
endpoint, so the real `llm_wiki` backend talks to it unchanged when
`LLM_WIKI_BASE_URL` points at it. That is exactly how `.env` is configured
today:

```
LLM_WIKI_BASE_URL=http://127.0.0.1:8005
LLM_WIKI_PREFIX=/llm-wiki
LLM_WIKI_DATABASE=placeholder
```

Registering it as a backend would invent a code path that has never existed and
has never been tested.

It exists so a `research_wiki` tool call completes instead of erroring out while
the real wiki host is unreachable — useful for exercising the whole
ASR → LLM → report → TTS loop. **It is never wiki content**: the answer text is
a fixed dummy string, in every response, for every question. Never point it at
anything real. Its own knobs are `LLM_WIKI_PLACEHOLDER_HOST` (default
`0.0.0.0`), `LLM_WIKI_PLACEHOLDER_PORT` (`8005`) and
`LLM_WIKI_PLACEHOLDER_LOG_LEVEL` (`info`).

## 6. Verifying without LiveKit

Hit the endpoint directly — the stream is the whole contract:

```bash
curl -N -X POST "$LLM_WIKI_BASE_URL/llm-wiki/$LLM_WIKI_DATABASE/api/ask/realtime/stream" \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -d '{"question":"テスト","max_levels":3,"max_queries_per_level":2}'
```

You should see `run`, then `plan` within the plan budget, then
`level_start` / `level` pairs, then `done`. If `plan` never arrives, the
watchdog's `plan_timeout` is doing its job and the problem is upstream.

`tests/test_sse.py` covers the parser (comments, multi-line `data`, the EOF
flush, the `event:` name filling in a missing `type`) with no network at all,
and `tests/test_providers.py` builds every registered backend offline.

## 7. Gotchas already paid for

- **The watchdog terminates when the stream terminates, not when the plan looks
  complete.** A `partial` run legitimately ends with fewer levels than planned.
  An earlier version only stopped once every planned level had arrived, so it
  re-ran the whole question 20 s after it had already answered it.
- **Two budgets, not one.** Before the `plan` frame the watchdog allows
  `RAG_PLAN_TIMEOUT_SECONDS`; after it, every subsequent frame — `plan_update`,
  `level_start`, `level` — resets a `RAG_LEVEL_TIMEOUT_SECONDS` gap timer. A
  slow *plan* and a slow *level* are different failures and want different
  numbers; `.env` currently runs 10 s and 30 s against the placeholder.
- **A `plan_update` is a real event, not just a screen frame.** `planned_levels`
  on the run is read live by the report pass, but `Memory` keeps its own copy of
  what the run still owes — that is what answers "we are already looking into
  that" instead of opening a second run. For a while the revision only reached
  the screen, so that answer was given off a plan the backend had already
  replaced. `PlanRevised` carries it into `Memory`, and only when the run
  accepted the revision, so a stale re-broadcast cannot resurrect a dropped
  objective. It is deliberately **not** a second `PlanReady`: the first plan is
  announced out loud, a revision never is.
- **`LLM_WIKI_REALTIME_URL` overrides the stream URL only.** Cancel is still
  built from `LLM_WIKI_BASE_URL` / `_PREFIX` / `_DATABASE`. Point the override
  at a different host and stops go to the old one, silently.
- **A superseded run is never cancelled.** `move_to_background` only flips
  `focus`; the run finishes and is remembered. Its findings are still delivered,
  unless enough reports have passed for the Conductor to judge them stale.
- **Cancellation is local first.** `cancel()` stops the local task and only then
  fires the remote stop as a detached task, because nobody should wait on the
  backend acknowledging it. The remote stop is best-effort and its failures are
  swallowed.
- **`run_id` is local and survives a retry**; `remote_run_id` comes from the
  `run` frame and is new on every attempt. The inbox events carry the local one.
