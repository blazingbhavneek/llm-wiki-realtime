# Repository reorganization — target file tree

Status: **proposal only. No code has been changed.** This document is the map to
follow when the move actually happens.

The goal is the one stated by the design in `plan.md` but never enforced by the
layout: **decisions live in one place, and everything that talks to the outside
world is a swappable provider behind a base class.** Today `server.py` alone
builds the STT, the LLM, the TTS, the FastAPI app, the TLS certificate, the
LiveKit worker and the session wiring — so swapping Supertonic for Qwen3, or
Nemotron for Voxtral, means editing the same 568-line file that owns the web
server.

---

## 1. The four rules the layout enforces

1. **`core/` decides, providers do I/O.** Nothing under `app/core/` may import
   `livekit.plugins`, `httpx`, `aiohttp`, or any provider module. It already
   almost holds: only `speaker.py` breaks it (it builds `StreamAdapter`s).
2. **One external dependency = one provider package.** STT, TTS, LLM and the
   wiki/RAG backend each get `base.py` (the contract), one file per
   implementation, `__init__.py` (the registry), and a `README.md` that says how
   to add the next one.
3. **A provider file owns everything about that model** — the client the agent
   talks to *and*, when we host the model ourselves, the server that hosts it.
   Adding a TTS engine is then always exactly "add one file, add one registry
   line". Supertonic's ONNX server (`tts_server.py`) and Nemotron's binary
   launcher (`asr_server.py`) move *into* their provider files as `serve()`.
4. **Only `app/runtime/` knows all the modules exist.** It reads the settings,
   asks each registry for a provider, and hands the built objects to `core/`.

---

## 2. The tree

```
llm-wiki-realtime/
├── README.md                        NEW   (pyproject already declares it; the file does not exist)
├── pyproject.toml                   EDIT  build-system + optional extras (see §7.6)
├── uv.lock
├── .env                             stays at root — python-dotenv and docker compose both expect it here
├── .env.example                     NEW   redacted copy, safe to commit; .env keeps the real hosts
├── .gitignore
│
├── app/                             ← every importable line of Python
│   ├── __init__.py
│   ├── __main__.py                  NEW   `python -m app` → runtime.worker.main()
│   ├── config.py                    NEW   typed Settings tree, read from env once (§5)
│   ├── log.py                       from server.py: dbg()
│   ├── timing.py                    MOVE  timing.py, unchanged
│   │
│   ├── core/                        the decision layer — no network, no provider imports
│   │   ├── __init__.py
│   │   ├── conductor.py             MOVE  conductor.py            (the single event loop)
│   │   ├── events.py                MOVE  events.py              (the vocabulary)
│   │   ├── attention.py             MOVE  attention.py           (orb gate + spoken commands)
│   │   ├── memory.py                MOVE  memory.py              (every level ever received)
│   │   ├── speaker.py               MOVE  speaker.py  — minus TTS construction (§7.1)
│   │   └── screen.py                MOVE  screen.py             (data channel to the browser)
│   │
│   ├── agent/                       what the LLM is told, and what it may call
│   │   ├── __init__.py
│   │   ├── assistant.py             from agent.py: Assistant(Agent)
│   │   ├── tools.py                 from agent.py: research_wiki / read_result / stop_research, AssistantDeps
│   │   └── prompts.py               from agent.py: GREETING, NOTICE_*, ASSISTANT_INSTRUCTIONS,
│   │                                              plan_preview_instructions, report_instructions
│   │                                              (every Japanese string in one file)
│   │
│   ├── stt/
│   │   ├── __init__.py              registry + build_stt()
│   │   ├── base.py                  NEW   STTProvider, STTSettings, capability flags
│   │   ├── nemotron.py              MOVE  nemo_stt.py + asr_server.py (client + serve())
│   │   ├── voxtral.py               NEW   vLLM-hosted realtime STT (from the pre-main history, §7.3)
│   │   └── README.md                NEW   how to add an STT model
│   │
│   ├── tts/
│   │   ├── __init__.py              registry + build_tts() / build_tts_pair()
│   │   ├── base.py                  NEW   TTSProvider, TTSSettings, capability flags
│   │   ├── supertonic.py            MOVE  tts_server.py + the openai.TTS block in server.py
│   │   ├── qwen3.py                 MOVE  the openai.TTS block from the main branch (vLLM)
│   │   └── README.md                NEW   how to add a TTS model
│   │
│   ├── llm/
│   │   ├── __init__.py              registry + build_llm()
│   │   ├── base.py                  NEW   LLMProvider, LLMSettings
│   │   ├── openai_compatible.py     MOVE  the openai.LLM block in server.py
│   │   │                                  (llama-server, vLLM, SGLang — anything OpenAI-shaped)
│   │   └── README.md                NEW
│   │
│   ├── rag/                         the wiki research backend
│   │   ├── __init__.py              registry + build_research_pool()
│   │   ├── base.py                  NEW   ResearchBackend / ResearchRun protocols
│   │   ├── sse.py                   from research.py: iter_sse_events, frame decoding
│   │   ├── llm_wiki.py              from research.py: ResearchRun, ResearchPool, URL builders, VOICE_KNOBS
│   │   ├── placeholder.py           MOVE  llm_wiki_placeholder.py
│   │   └── README.md                NEW
│   │
│   ├── web/                         the browser-facing HTTP side
│   │   ├── __init__.py
│   │   ├── http.py                  from server.py: FastAPI app, /health, /, static mount
│   │   ├── tokens.py                from server.py: /token — room create, dispatch, JWT
│   │   └── tls.py                   from server.py: ensure_local_https_certificate
│   │
│   └── runtime/                     the only layer that knows every module
│       ├── __init__.py
│       ├── entrypoint.py            from server.py: entrypoint(ctx) — build providers, session, Conductor
│       ├── producers.py             from server.py: session/room callbacks → inbox events
│       └── worker.py                from server.py: prewarm, worker_load, proxy scrub,
│                                                    start_web_server, run_combined_server, main()
│
├── frontend/                        unchanged
│   └── src/{App.jsx, components/, lib/}
│
├── tests/
│   ├── __init__.py
│   ├── fakes.py                     from test_realtime_modules.py: FakeSpeaker, FakeScreen, FakePool, build()
│   ├── test_attention.py            \
│   ├── test_memory.py                >  from test_realtime_modules.py, split by subject
│   ├── test_conductor.py            /
│   ├── test_sse.py                  from test_realtime_modules.py: SseTests
│   └── test_providers.py            NEW   every registered provider builds offline (§7.5)
│
├── docs/
│   ├── DESIGN.md                    MOVE  plan.md
│   ├── OPERATIONS.md                MOVE  commands.txt (ports, start order, troubleshooting)
│   └── REORGANIZATION.md            this file
│
├── Caddyfile                        unchanged — see §9.2 before moving these
├── docker-compose.caddy.yml         unchanged
├── livekit.yaml                     unchanged
├── scripts/
│   ├── build_asr_server.sh          unchanged
│   └── setup_https_livekit.sh       unchanged
│
├── models/                          gitignored — GGUF weights
├── vendor/                          gitignored — NeMo-Speech.cpp checkout
└── certs/                           gitignored — generated TLS material
```

`app/` is a placeholder name; `wiki_voice/`, `movi/` or `realtime/` all work.
It is one `git mv` plus one search-and-replace to change, so pick it before
step 1 of §8, not after.

---

## 3. Where each current file goes

| today | tomorrow | kind |
|---|---|---|
| `conductor.py` | `app/core/conductor.py` | pure move |
| `events.py` | `app/core/events.py` | pure move |
| `attention.py` | `app/core/attention.py` | pure move |
| `memory.py` | `app/core/memory.py` | pure move |
| `screen.py` | `app/core/screen.py` | pure move |
| `timing.py` | `app/timing.py` | pure move |
| `speaker.py` | `app/core/speaker.py` | move + drop the TTS build (§7.1) |
| `agent.py` | `app/agent/{assistant,tools,prompts}.py` | split, no logic change |
| `nemo_stt.py` | `app/stt/nemotron.py` | move + wrap in a provider class |
| `asr_server.py` | `app/stt/nemotron.py::serve()` | fold in |
| `tts_server.py` | `app/tts/supertonic.py::serve()` + the app it serves | fold in |
| `research.py` | `app/rag/sse.py` + `app/rag/llm_wiki.py` | split |
| `llm_wiki_placeholder.py` | `app/rag/placeholder.py` | move |
| `server.py` → FastAPI/token/TLS | `app/web/{http,tokens,tls}.py` | split |
| `server.py` → `entrypoint`, callbacks | `app/runtime/{entrypoint,producers}.py` | split |
| `server.py` → worker/uvicorn bootstrap | `app/runtime/worker.py` | split |
| `server.py` → `dbg`, `env_bool` | `app/log.py`, `app/config.py` | split |
| `test_realtime_modules.py` | `tests/*.py` | split |
| `plan.md` | `docs/DESIGN.md` | move |
| `commands.txt` | `docs/OPERATIONS.md` | move + rename the run commands (§7.6) |
| main branch `server.py` TTS block | `app/tts/qwen3.py` | port |
| main branch `server.py` STT block | `app/stt/voxtral.py` | port (§7.3) |
| main branch `delivery.py`, `scheduler.py`, `rag_client.py` | — | superseded by `core/` + `rag/`; do not port |

Nothing is deleted until step 7 of §8, when the last import of a root module is
gone.

---

## 4. The provider contract

Signatures only — the point is the shape, not the implementation.

### 4.1 `app/tts/base.py`

```python
@dataclass(frozen=True)
class TTSSettings:
    provider: str
    model: str
    voice: str
    base_url: str
    api_key: str
    language: str            # "ja" — supertonic's lang code, qwen's locale hint
    instructions: str        # forwarded as the request's `instructions` field
    response_format: str     # "wav" | "pcm" — provider default unless overridden
    speed: float
    reply_min_chars: int     # the two sentence thresholds Speaker needs
    report_min_chars: int
    stream_context_chars: int


class TTSProvider(abc.ABC):
    name: ClassVar[str]                     # the registry key, e.g. "supertonic"
    hosted_by: ClassVar[str]                # "self" | "vllm" | "remote"

    default_model: ClassVar[str]
    default_voice: ClassVar[str]
    default_base_url: ClassVar[str]
    default_response_format: ClassVar[str]  # supertonic "wav" @44.1k, qwen3 "pcm" @24k
    native_sample_rate: ClassVar[int]
    streams_audio: ClassVar[bool]           # False for both today → StreamAdapter + long sentences
    honors_instructions: ClassVar[bool]     # supertonic: False, it reads TTS_LANG server-side

    @classmethod
    def settings_from_env(cls) -> TTSSettings: ...

    @abc.abstractmethod
    def build(self, settings: TTSSettings) -> livekit.agents.tts.TTS:
        """The raw TTS. Sentence batching is applied by the module facade."""

    def serve(self, settings: TTSSettings) -> None:
        """Host this model locally. Only for hosted_by == 'self'."""
        raise NotImplementedError
```

The two class attributes that matter most are `default_response_format` and
`native_sample_rate`: today `server.py` hardcodes `response_format="wav"` with a
comment explaining that Supertonic emits 44.1 kHz, while main hardcodes `"pcm"`
for Qwen. That is per-model knowledge sitting in the wiring, and it is exactly
the kind of thing that fails as garbled audio rather than as an error.

### 4.2 `app/tts/__init__.py`

```python
REGISTRY: dict[str, str] = {
    "supertonic": "app.tts.supertonic:SupertonicTTS",
    "qwen3":      "app.tts.qwen3:Qwen3TTS",
}

def get_provider(name: str) -> type[TTSProvider]: ...      # importlib, lazy
def build_tts(settings: TTSSettings | None = None) -> tts.TTS: ...
def build_tts_pair(settings=None) -> dict[str, tts.TTS]:   # {"reply": …, "report": …}
```

The registry maps to **import strings, not classes**, so importing `app.tts`
does not drag `supertonic`/`soundfile`/`onnxruntime` into the agent process when
`TTS_PROVIDER=qwen3`. That laziness is a requirement, not a nicety.

`build_tts_pair` is where `speaker.py`'s two `StreamAdapter` configurations move
to: the *policy* (conversation wants first audio fast, a report wants one
coherent synthesis) stays in the design; the *thresholds* become provider
defaults, because the right value depends on whether that engine streams.

### 4.3 `app/stt/base.py`

```python
@dataclass(frozen=True)
class STTSettings:
    provider: str
    model: str
    base_url: str
    api_key: str
    language: str                 # nemotron needs the full locale "ja-JP"
    sample_rate: int
    automatic_punctuation: bool


class STTProvider(abc.ABC):
    name: ClassVar[str]
    hosted_by: ClassVar[str]

    default_model: ClassVar[str]
    default_base_url: ClassVar[str]
    default_language: ClassVar[str]
    native_sample_rate: ClassVar[int]

    requires_vad: ClassVar[bool]            # nemotron True — it commits on end-of-speech
    emits_interim: ClassVar[bool]
    finals_are_utterances: ClassVar[bool]   # ← the flag the Conductor depends on
    language_is_locale: ClassVar[bool]      # nemotron: "ja" silently means auto-detect

    @classmethod
    def settings_from_env(cls) -> STTSettings: ...

    @abc.abstractmethod
    def build(self, settings: STTSettings, *, vad=None) -> livekit.agents.stt.STT: ...

    def serve(self, settings: STTSettings) -> None: ...   # nemotron: exec the vendored binary
```

`finals_are_utterances` deserves its own line in the contract because it is the
assumption `server.py`'s `_on_transcript` is built on ("every final is a
complete utterance") and the reason `asr_server.py` disables the engine's own
endpointing. A provider that finalizes on server-side silence instead will
fragment one spoken question into several `UserSaidText` events — a bug that
looks like a bad ASR model, not like a bad integration. Declaring it lets
`runtime/producers.py` refuse or aggregate, instead of every future provider
rediscovering it. The long docstring at the top of `nemo_stt.py` is the record
of learning this the hard way; it moves with the file.

### 4.4 `app/llm/base.py`

```python
@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str
    api_key: str

class LLMProvider(abc.ABC):
    name: ClassVar[str]
    default_model / default_base_url: ClassVar[str]
    supports_tools: ClassVar[bool]          # the assistant's three tools need this
    @abc.abstractmethod
    def build(self, settings: LLMSettings) -> livekit.agents.llm.LLM: ...
```

One implementation today (`openai_compatible.py`) covers llama-server, vLLM and
SGLang. Switching from Gemma on llama-server to anything else stays a
`LLM_MODEL` + `LLM_BASE_URL` edit; the package exists so a non-OpenAI-shaped
provider does not have to re-open `runtime/entrypoint.py`.

### 4.5 `app/rag/base.py`

```python
class ResearchRun(Protocol):
    run_id: str; question: str; focus: str
    planned_levels: list[dict[str, Any]]
    superseded_at_report: int
    finished: bool
    def start(self) -> None: ...
    async def cancel(self) -> None: ...

class ResearchBackend(Protocol):
    def start(self, question: str) -> ResearchRun: ...
    def get(self, run_id: str) -> ResearchRun | None: ...
    def foreground_run(self) -> ResearchRun | None: ...
    def move_to_background(self, run: ResearchRun) -> None: ...
    def can_retry(self, run: ResearchRun) -> bool: ...
    def retry(self, run: ResearchRun) -> None: ...
    async def cancel_all(self) -> None: ...
```

This is the surface `Conductor` already uses — writing it down costs nothing and
turns the core layer's dependency on a research backend into a contract rather
than an accident.

`app/rag/placeholder.py` is deliberately **not** registered as a backend. It is
a local stand-in *server* that the `llm_wiki` backend talks to when
`LLM_WIKI_BASE_URL` points at it, which is exactly how `.env` is configured
today; registering it would invent behaviour that has never existed.

---

## 5. Switching components

`app/config.py` reads the environment **once** into a typed tree, replacing the
`os.getenv` calls scattered through `server.py`:

```python
@dataclass(frozen=True)
class Settings:
    livekit: LiveKitSettings         # url, keys, agent name, dispatch, error budget
    web:     WebSettings             # host, port, TLS, cert dir, access token
    tuning:  TuningSettings          # VAD_MIN_SILENCE_SECONDS
    stt_provider: str                # STT_PROVIDER
    tts_provider: str                # TTS_PROVIDER
    llm_provider: str                # LLM_PROVIDER
    rag_provider: str                # RAG_PROVIDER

    @classmethod
    def from_env(cls) -> "Settings": ...
```

Provider settings are deliberately **not** here. Each provider package owns its
own env block through `settings_from_env()`, with the defaults living on the
provider class — so adding an engine never touches `config.py`, and a provider
package stays importable and testable on its own.

`.env` stays the single deployment file and **every existing variable keeps its
name**. Four new selector variables are the whole switching story:

| what you switch | variable | values | default |
|---|---|---|---|
| TTS engine | `TTS_PROVIDER` | `supertonic`, `qwen3` | `supertonic` |
| STT engine | `STT_PROVIDER` | `nemotron`, `voxtral` | `nemotron` |
| LLM | `LLM_PROVIDER` | `openai_compatible` | `openai_compatible` |
| wiki backend | `RAG_PROVIDER` | `llm_wiki` | `llm_wiki` |

Switching an *endpoint* remains what it is today — `TTS_BASE_URL=…`. Switching
an *engine* is now one line instead of an edit to `server.py`, and the
per-engine defaults (model id, voice, audio format, sample rate, language
handling) come from the provider class rather than from the `os.getenv` default
argument in the wiring.

---

## 6. What the provider READMEs must contain

`app/tts/README.md`, `app/stt/README.md`, `app/llm/README.md` and
`app/rag/README.md` all follow one shape:

1. **The contract** — one paragraph: what `build()` returns and who calls it.
2. **Choosing one** — the selector variable and the env table per provider.
3. **The providers table** — name · hosted by · endpoint · audio format /
   sample rate · streaming · voices or languages · what pins the language.
4. **Adding a vLLM-hosted model** — the short path:
   1. copy `qwen3.py` to `<name>.py`;
   2. subclass the base, set `hosted_by = "vllm"`;
   3. fill in the defaults and capability flags — *`default_response_format`
      and `native_sample_rate` are the two that fail as noise rather than as an
      error*;
   4. add one line to `REGISTRY` in `__init__.py`;
   5. document its env block in this README and in `.env.example`.
5. **Adding a locally-hosted model** — same, plus: implement `serve()`, add the
   heavy dependencies as an optional extra in `pyproject.toml`, and add the
   launch line to `docs/OPERATIONS.md`.
6. **Verifying without LiveKit** — the `curl` against `/v1/audio/speech` (or
   `/v1/audio/transcriptions`), and `tests/test_providers.py`.
7. **Gotchas already paid for** — kept verbatim from the existing comments,
   because each cost a debugging session:
   - Supertonic returns native 44.1 kHz WAV; `"pcm"` would be read as raw 24 kHz.
   - Supertonic ignores `instructions`; its language pin is `TTS_LANG`,
     server-side.
   - Neither TTS streams, so a short sentence threshold turns one reply into
     many independent syntheses and prosody resets mid-answer.
   - Nemotron matches the language prompt by exact locale: `ja` is silently
     auto-detect, `ja-JP` is Japanese.
   - `livekit.plugins.openai.STT`'s realtime path speaks OpenAI's nested session
     shape; NeMo-Speech.cpp reads a flat one, and neither side errors.

---

## 7. The parts that are not a pure move

The user-visible behaviour must not change. These are the only edits the move
requires.

### 7.1 `speaker.py` loses its TTS construction

Today `Speaker.__init__` takes `base_tts` and builds two `StreamAdapter`s.
After the move it takes the pair already built:

```python
Speaker(session, assistant, tts_pair, inbox)     # tts_pair = app.tts.build_tts_pair()
```

Six lines move out, `_sentence_tokenizer` goes with them, and `core/` stops
importing `livekit.agents.tts`.

### 7.2 `nemo_stt.py` gains a provider wrapper

The `NemotronSTT` class is unchanged. `app/stt/nemotron.py` adds a ~20-line
`NemotronSTTProvider` around it holding the defaults and capability flags, plus
`serve()` — the body of today's `asr_server.py`, including the comment
explaining why `--endpointing` stays off.

### 7.3 `voxtral.py` is mostly new code

Worth being explicit: there is **no Voxtral implementation to move**. It exists
in exactly one commit — `5738322` *"first commit, needs live testing"*
(2026-08-06) — and its entire footprint is a default string:

```python
# This endpoint is backed by a realtime STT model. Passing it directly
# exposes interim transcripts instead of forcing utterance-level batching.
stt = openai.STT(
    model=os.getenv("STT_MODEL", "mistralai/Voxtral-Mini-4B-Realtime-2602"),
    base_url=os.getenv("STT_BASE_URL", "http://127.0.0.1:8001/v1"),
    api_key=os.getenv("STT_API_KEY", "EMPTY"),
)
```

**That comment was never true of that code.** `openai.STT` takes
`use_realtime: bool = False` and declares
`STTCapabilities(streaming=use_realtime, interim_results=use_realtime)`, and
`Agent.stt_node` wraps any STT whose `capabilities.streaming` is false in
`stt.StreamAdapter(stt=…, vad=activity.vad)`. With `use_realtime` left unset,
that config ran as **batch transcription, VAD-segmented** — one
`POST /v1/audio/transcriptions` per speech segment — which is the opposite of
what the comment claims. `plan.md` at that commit flags it as unverified in so
many words: *"confirm the deployed OpenAI-compatible STT actually exposes
streaming via `openai.STT`; if it does not, use the provider's Voxtral realtime
plugin rather than restoring the batch `StreamAdapter`."* It was never
confirmed. The next commit (`7fbabc2`) replaced the default with
`nvidia/nemotron-3.5-asr-streaming-0.6b` on the shared GPU box
(`10.160.144.101:51026`), still through plain `openai.STT`, so `main` inherited
the same batch path — and the current branch's whole reason for existing is
`nemo_stt.NemotronSTT`, which speaks the server's realtime WebSocket directly
because `openai.STT` could not.

So `app/stt/voxtral.py` is a new provider, and the history is a warning rather
than a starting point. Two viable shapes:

- **Batch** (`use_realtime=False`, what the first commit actually did):
  `requires_vad = True`, `emits_interim = False`,
  `finals_are_utterances = True`. Compatible with the Conductor as-is, and the
  slowest option — every turn costs a from-scratch decode.
- **Realtime** (`use_realtime=True`): `emits_interim = True`, and
  `finals_are_utterances = False` if the vLLM endpoint does its own
  endpointing — in which case `runtime/producers.py` must aggregate finals, or
  the provider must commit on VAD the way `NemotronSTT` does.

Set the flags from what the deployed endpoint actually does. The one lesson the
first commit already paid for is that a comment asserting "this streams" is not
evidence that it streams.

### 7.4 `research.py` splits, `VOICE_KNOBS` moves with it

`iter_sse_events` and `_decode_frame` are generic transport → `rag/sse.py`.
`ResearchRun`, `ResearchPool`, the URL builders, the timeouts and `VOICE_KNOBS`
are llm-wiki specifics → `rag/llm_wiki.py`.

### 7.5 Tests split, and gain one

`test_realtime_modules.py`'s fakes are shared, so they move to `tests/fakes.py`
and each `*Tests` class gets its own file. `tests/test_providers.py` is new and
cheap: for every name in every `REGISTRY`, import it, build settings from a
fake env, assert `build()` returns the right LiveKit base type and that the
capability flags are all declared. It catches a half-registered provider without
a GPU, a model file, or a network.

### 7.6 Run commands change

`uv run server.py` and its three siblings become module invocations. No
packaging change is needed for these:

```
uv run python -m app                      # agent + web        (was: uv run server.py)
uv run python -m app.stt.nemotron         # ASR :8003          (was: uv run asr_server.py)
uv run python -m app.tts.supertonic       # TTS :8004          (was: uv run tts_server.py)
uv run python -m app.rag.placeholder      # RAG stub :8005     (was: uv run llm_wiki_placeholder.py)
```

Optionally add `[project.scripts]` for `uv run wiki-agent` / `wiki-asr` /
`wiki-tts` — that needs a `[build-system]` block, which `pyproject.toml`
currently lacks entirely, plus `[tool.hatch.build.targets.wheel] packages =
["app"]`. Worth doing at the same time as adding the optional extras
(`supertonic`, `soundfile`, `numpy`, `huggingface_hub` are only needed when
hosting Supertonic locally), but it is a separate decision from the move.

`docs/OPERATIONS.md` §1's port table and start order are otherwise still
correct, and its `pkill` warning gets *better*: the "`server\.py$` also matches
`asr_server.py`" trap disappears once the processes are `python -m app.*`.

---

## 8. Migration order

Each step ends with a repository that starts and passes tests, so a step can be
committed on its own and a bad one reverted alone.

1. Pick the package name. Create `app/`, `app/core/`, move the six pure-move
   files with `git mv`, fix the intra-core imports, `python -m unittest`.
2. Move `timing.py`, add `log.py` and `config.py` (`Settings.from_env` written
   but not yet used by anything).
3. Split `agent.py` into `app/agent/{prompts,tools,assistant}.py`. Text only —
   no logic moves.
4. Create `app/tts/` and `app/stt/` with `base.py`, `__init__.py` and the two
   current providers (`supertonic.py`, `nemotron.py`, absorbing `tts_server.py`
   and `asr_server.py`). `server.py` starts calling `build_tts_pair()` /
   `build_stt()`. Write both READMEs now, while the reasons are fresh.
5. Add `qwen3.py` and `voxtral.py`; add `app/llm/`. Verify the switch by running
   once with `TTS_PROVIDER=qwen3` against the vLLM host.
6. Create `app/rag/` from `research.py` + `llm_wiki_placeholder.py`.
7. Split `server.py` into `app/web/` + `app/runtime/`, delete the root modules,
   update `docs/OPERATIONS.md` and the run commands.
8. Split the tests, add `tests/test_providers.py`, write the root `README.md`,
   move `plan.md` → `docs/DESIGN.md`.

Steps 1–3 are mechanical and safe. Step 4 is the one that earns the whole
exercise. Steps 5–8 can each wait.

---

## 9. Decisions to make before starting

### 9.1 The five-file rule for provider modules

The tree keeps `tts/` and `stt/` at exactly the five files requested
(`__init__`, `base`, two providers, `README`). The cost is that
`tts/supertonic.py` ends up around 250 lines, because it holds both the client
and the FastAPI server that hosts the ONNX model. That is defensible —
everything about Supertonic is in one file, and "add a model = add a file" stays
literally true. If a provider ever outgrows it, promote just that one to a
package (`tts/supertonic/{__init__,client,server}.py`) without touching the
registry, which points at `app.tts.supertonic:SupertonicTTS` either way.

### 9.2 Leave the deploy files at the root (recommended)

Moving `Caddyfile`, `docker-compose.caddy.yml`, `livekit.yaml` and `scripts/`
into a `deploy/` directory is tempting and costs more than it looks:

- `docker compose -f deploy/docker-compose.caddy.yml` makes `deploy/` the
  project directory, so compose reads `deploy/.env` — `PUBLIC_HOST` would stop
  resolving and the stack would fail its `:?set PUBLIC_HOST in .env` guard.
  Fixable with `--env-file .env --project-directory .` on every invocation.
- The compose volume `./certs:/etc/caddy/certs:ro` resolves relative to the
  compose file, so it would need to become `../certs`.
- `scripts/setup_https_livekit.sh` hardcodes `$ROOT/Caddyfile`,
  `$ROOT/docker-compose.caddy.yml`, `$ROOT/certs`.
- Every command in `docs/OPERATIONS.md` gains a path prefix.

Four edits and a new footgun, for tidiness. The Python reorganization is the
part with real value; do this one later or not at all.

### 9.3 `.env` stays at the root

`python-dotenv`'s `load_dotenv(override=True)` and docker compose both look
there, and the file's own header calls it the deployment configuration. Add the
four `*_PROVIDER` lines to it and commit a redacted `.env.example` alongside.
