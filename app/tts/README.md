# TTS providers

## 1. The contract

Every engine is a `TTSProvider` subclass (`base.py`). It declares its defaults
and capability flags as class attributes, and `build(settings)` returns a raw
`livekit.agents.tts.TTS` — no sentence batching, no policy. `settings_from_env()`
is concrete on the base: it reads the `TTS_*` variables and falls back to
*that provider's* `default_*`, so switching engines switches the defaults with
it. A provider that hosts its own model also implements `serve()`.

The callers are `build_tts()` and `build_tts_pair()` in `__init__.py`.
`app.core.speaker` takes the pair already built and installs the right one per
speech; it never imports a provider, a plugin, or `livekit.agents.tts`.

The registry maps names to **import strings**, not classes, and resolves them
with `importlib`. Importing `app.tts` must not drag
`supertonic`/`soundfile`/`onnxruntime` into the agent process when
`TTS_PROVIDER=qwen3`.

## 2. Choosing one

`TTS_PROVIDER` selects the engine; default `supertonic`.

| variable | meaning | default |
|---|---|---|
| `TTS_PROVIDER` | `supertonic` \| `qwen3` | `supertonic` |
| `TTS_MODEL` | model id | provider's `default_model` |
| `TTS_VOICE` | voice id | provider's `default_voice` |
| `TTS_BASE_URL` | OpenAI-compatible endpoint | provider's `default_base_url` |
| `TTS_API_KEY` | bearer token (both endpoints ignore it) | `EMPTY` |
| `TTS_LANG` | language code; Supertonic's server-side pin | `ja` |
| `TTS_INSTRUCTIONS` | sent as the request's `instructions` field | the Japanese "speak only Japanese" sentence |
| `TTS_RESPONSE_FORMAT` | `wav` \| `pcm` | provider's `default_response_format` |
| `TTS_SPEED` | synthesis speed | `1.05` |
| `TTS_REPLY_MIN_SENTENCE_CHARS` | sentence threshold for conversation | provider's `default_reply_min_chars` |
| `TTS_REPORT_MIN_SENTENCE_CHARS` | sentence threshold for research prose | provider's `default_report_min_chars` |
| `TTS_STREAM_CONTEXT_CHARS` | tokenizer lookahead | `240` |
| `TTS_SERVER_HOST` / `TTS_SERVER_PORT` / `TTS_SERVER_LOG_LEVEL` | Supertonic's own server only | `0.0.0.0` / `8004` / `info` |
| `TTS_STEPS`, `SUPERTONIC_MODEL_DIR` | Supertonic's own server only | `8`, HF cache |

Switching an *endpoint* is still `TTS_BASE_URL=…`. Switching an *engine* is
`TTS_PROVIDER=…`, and the model id, voice, audio format, sample rate and
sentence thresholds come with it.

## 3. The providers

| name | hosted by | endpoint (default) | format / sample rate | streams | voices | language pin |
|---|---|---|---|---|---|---|
| `supertonic` | self — `app/tts/supertonic.py::serve()`, ONNX Runtime on CPU | `http://127.0.0.1:8004/v1` | `wav` / 44 100 Hz | no | `F1`, and whatever else `supertonic-3` ships (`get_voice_style`) | `TTS_LANG`, read server-side; `instructions` is ignored |
| `qwen3` | vLLM on the shared GPU box (outside this repo) | `http://10.160.144.101:51027/v1` | `pcm` / 24 000 Hz | no | `Ono_Anna` and the other Qwen custom voices | `instructions`, honored by the server |

## 4. Adding a vLLM-hosted model

1. Copy `qwen3.py` to `<name>.py`.
2. Subclass `TTSProvider`, set `name = "<name>"` and `hosted_by = "vllm"`.
3. Fill in the defaults and capability flags. **`default_response_format` and
   `native_sample_rate` are the two that fail as noise rather than as an
   error** — everything else raises something you can read in a log. Check what
   the endpoint actually returns before guessing; the sample rate must be the
   one the bytes are in, not the one you want.
4. Add one line to `REGISTRY` in `__init__.py`:
   `"<name>": "app.tts.<name>:<Name>TTS"`.
5. Document its env block in section 2 of this README and in `.env.example`.

## 5. Adding a locally-hosted model

Steps 1–5 above with `hosted_by = "self"`, plus:

6. Implement `serve()` in the same file — the client and the server that hosts
   it live together, so "add a model" stays "add one file, add one registry
   line". `supertonic.py` is the worked example: an OpenAI-compatible
   `/v1/audio/speech` app plus a `main()`, with the model stack imported inside
   the functions that use it so the agent process never loads it.
7. Add the heavy dependencies as an optional extra in `pyproject.toml`
   (Supertonic's are `supertonic`, `soundfile`, `numpy`, `huggingface_hub`).
8. Add the launch line to `docs/OPERATIONS.md` and to the port table.

## 6. Verifying without LiveKit

Run the local engine:

```
uv run python -m app.tts.supertonic          # TTS :8004
```

Hit the endpoint directly — this is the same request the `openai.TTS` client
makes, so it isolates the engine from the agent:

```
curl -s http://127.0.0.1:8004/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"supertonic-3","voice":"F1","input":"こんにちは、テストです。","response_format":"wav"}' \
  -o /tmp/tts.wav && file /tmp/tts.wav
```

`file` should report a 44.1 kHz WAV. Point the same `curl` at
`TTS_BASE_URL` to check a remote engine; `GET /v1/models` and `GET /health` are
the cheap liveness checks (the vLLM host serves `/v1/models` too).

`tests/test_providers.py` covers the offline half: every registered provider
imports, builds settings from a fake environment, and declares every capability
flag.

## 7. Gotchas already paid for

- **Supertonic returns its native 44.1 kHz WAV.** `"wav"` lets LiveKit's own
  decoder resample it. `"pcm"` would hand LiveKit raw samples it assumes are
  24 kHz, and 44.1 kHz audio read as 24 kHz plays slow and deep — audible
  garbage, not an error. That is why `default_response_format` and
  `native_sample_rate` are per-provider class attributes.
- **Supertonic ignores `instructions`.** The OpenAI-compatible adapter forwards
  it as the request's `instructions` field and the server drops it: Supertonic's
  language pin is `TTS_LANG`, read server-side, because the SDK takes a `lang`
  code rather than a text instruction. Sending it anyway is harmless and keeps
  parity with other OpenAI-compatible servers — but changing `TTS_INSTRUCTIONS`
  will not change Supertonic's language. Qwen servers that implement
  `instructions` do honor it, and use it to disable automatic language
  selection for otherwise Japanese text.
- **Neither engine streams audio.** Both are non-streaming
  `POST /v1/audio/speech` endpoints behind a `StreamAdapter`, so the sentence
  threshold decides how many independent synthesis requests one reply becomes.
  A short threshold turns one answer into many, and language and prosody reset
  mid-answer. Hence two configurations, not one: `reply` wants first audio
  fast (30 chars for Supertonic), `report` wants one coherent synthesis with
  stable prosody (180). Qwen used 180 for everything.
- **The port used to disagree with itself.** `tts_server.py`'s `main()`
  defaulted to `8004` while `server.py`'s client defaulted to `8002`; only
  `.env` setting `TTS_BASE_URL=http://127.0.0.1:8004/v1` kept the stack
  working. `SupertonicTTS.default_base_url` is now `http://127.0.0.1:8004/v1`,
  matching the server and `.env`.
