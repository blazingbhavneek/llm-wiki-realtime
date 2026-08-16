# llm-wiki-realtime

A Japanese-speaking voice agent that answers questions about an internal wiki.
The browser talks to a [LiveKit](https://livekit.io) room over WebRTC; the agent
listens through a local streaming ASR, asks an OpenAI-compatible LLM, and calls
the wiki's deep-research backend, which streams its answer back level by level
over SSE. Findings are spoken as they arrive rather than after the whole search
finishes, so a long question produces a running answer instead of a long
silence. Every component — speech recognition, speech synthesis, the LLM, the
wiki backend — is a provider behind a base class, selected by one environment
variable.

## Layout

Everything importable lives under `app/`. `core/` decides; the provider packages
do I/O; `runtime/` is the only layer that knows all of them exist.

```
app/
├── __main__.py     `python -m app` → runtime.worker.main()
├── config.py       the typed Settings tree, read from the environment once
├── log.py          dbg()
├── timing.py       per-turn latency instrumentation (TURN_TIMING=1)
├── core/           the decision layer — no network, no provider imports
│                     conductor (the single event loop), events (the vocabulary),
│                     attention (the orb gate), memory (every level ever received),
│                     speaker (what is said), screen (the data channel to the browser)
├── agent/          what the LLM is told, and what it may call
│                     assistant, tools (research_wiki / read_result / stop_research), prompts
├── stt/            speech-to-text providers: nemotron (local), voxtral (vLLM)
├── tts/            text-to-speech providers: supertonic (local), qwen3 (vLLM)
├── llm/            chat-completions providers: openai_compatible
├── rag/            the wiki research backend: llm_wiki, plus placeholder (a local stub server)
├── web/            the browser-facing HTTP side: http (FastAPI), tokens (/token), tls
└── runtime/        entrypoint (build the providers and the session), producers
                    (session/room callbacks → inbox events), worker (the bootstrap)
```

`frontend/` is the browser client (Vite/React); the agent's web server serves
its build from `frontend/dist`.

## Run it

Order matters — the agent goes last. All addresses live in `.env`.

```bash
docker compose -f docker-compose.caddy.yml up -d   # LiveKit + the TLS proxy
# the LLM (llama-server or similar) is started outside this repo, on :8000

uv run python -m app.stt.nemotron     # ASR      :8003
uv run python -m app.tts.supertonic   # TTS      :8004
uv run python -m app.rag.placeholder  # RAG stub :8005
uv run python -m app                  # agent + web :51028   <- LAST
```

Then open <https://localhost:51027> — the Caddy port, not 51028.

Once `uv sync` has installed the project these are also available as console
scripts: `uv run wiki-asr`, `uv run wiki-tts`, `uv run wiki-rag-stub`,
`uv run wiki-agent`. Hosting Supertonic locally needs the ONNX stack:
`uv sync --extra supertonic`.

Tests (offline, no GPU and no network):

```bash
python -m unittest discover -s tests -t . -q
```

Every registered STT/TTS provider also has a live test that exercises its
real `build()` result — the self-hosted engines against the actual model on
disk, the vLLM-hosted ones against an endpoint you point at — see
[`tests/live/README.md`](tests/live/README.md):

```bash
python -m unittest discover -s tests/live -t . -p "live_*.py" -q
```

## Switch a component

One variable per package, in `.env`. Nothing else changes — the model id, the
voice, the audio format, the sample rate and the language handling come from
the provider class, not from the wiring.

| what | variable | registered names | default | how to add one |
|---|---|---|---|---|
| speech recognition | `STT_PROVIDER` | `nemotron`, `voxtral` | `nemotron` | [`app/stt/README.md`](app/stt/README.md) |
| speech synthesis | `TTS_PROVIDER` | `supertonic`, `qwen3` | `supertonic` | [`app/tts/README.md`](app/tts/README.md) |
| the LLM | `LLM_PROVIDER` | `openai_compatible` | `openai_compatible` | [`app/llm/README.md`](app/llm/README.md) |
| the wiki backend | `RAG_PROVIDER` | `llm_wiki` | `llm_wiki` | [`app/rag/README.md`](app/rag/README.md) |

Switching an *endpoint* stays what it always was (`TTS_BASE_URL=…`). Adding an
engine is one file plus one registry line; each package README says exactly
what a new provider must declare, and `tests/test_providers.py` checks that it
did — offline.

`.env.example` is the redacted copy of a working `.env`, with every comment
kept. Copy it, fill in the hosts, and read
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) before starting anything.

## Where to read next

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — the runbook: ports, start and
  stop order, certificates, moving to another machine, debug flags, and the
  troubleshooting entries for every failure this stack has actually produced.
- [`docs/DESIGN.md`](docs/DESIGN.md) — why the architecture is what it is: the
  single event loop, why findings are spoken as they stream, and what the
  Conductor guarantees.
- [`docs/REORGANIZATION.md`](docs/REORGANIZATION.md) — the map this layout was
  moved to, and the provider contracts in signature form.
