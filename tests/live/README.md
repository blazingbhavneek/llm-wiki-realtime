# Live provider tests

`tests/test_providers.py` checks the offline half of every provider: it
resolves, its settings have defaults, it declares its capability flags. It
never calls `build()`, because building one needs a live LiveKit job context
and, for the self-hosted engines, an actual model on disk.

This directory is the other half: each provider's `build()` result exercised
the way `app.runtime.entrypoint` actually uses it — streamed or batch
audio in, a real `livekit.agents.stt.STT` / `tts.TTS` on the other end. It is
**not** part of the fast offline suite. `python -m unittest discover -s tests
-t .` *does* recurse into this directory (it's a package under `tests/`), but
its default file pattern is `test*.py`, and `live_stt.py` / `live_tts.py`
deliberately don't start with `test` — so that command never imports them.
That matters beyond speed: `live_tts.py`'s availability check imports
`supertonic` directly to decide whether to skip, and `tests/test_providers.py`
has a test (`LazyRegistryTests`) asserting that resolving the `qwen3` provider
never drags `supertonic` into `sys.modules` — an incidental import during the
same discovery run would trip it. Run this suite explicitly, with its own
pattern:

```bash
python -m unittest discover -s tests/live -t . -p "live_*.py" -q
```

## What runs, and what skips

| test | provider | runs when | skips when |
|---|---|---|---|
| `live_stt.NemotronLiveTest` | `nemotron` (self-hosted) | the built binary and GGUF model are on disk | `scripts/build_asr_server.sh` hasn't been run, or the model file is missing |
| `live_stt.VoxtralLiveTest` | `voxtral` (vLLM) | `VOXTRAL_TEST_BASE_URL` is set | the var is unset |
| `live_tts.SupertonicLiveTest` | `supertonic` (self-hosted) | the `supertonic` package is installed (`uv sync --extra supertonic`) and the `Supertone/supertonic-3` model is cached (or `SUPERTONIC_MODEL_DIR` points at it) | either is missing |
| `live_tts.Qwen3LiveTest` | `qwen3` (vLLM) | `QWEN3_TEST_BASE_URL` is set | the var is unset |

The self-hosted tests boot the real server (`nemo-speech`, or
`python -m app.tts.supertonic`) as a subprocess on a scratch port, so they
never collide with a deployment already running on `:8003`/`:8004`, and tear
it down afterward. The vLLM-hosted tests never start anything — they expect
you already have Voxtral / Qwen3-TTS running behind vLLM and point at it.

## Env vars for the vLLM-hosted providers

Deliberately separate from this project's own `STT_BASE_URL` / `TTS_BASE_URL`
(§`.env`), so you can point a test at a vLLM box without touching — or being
affected by — the deployment config, and so both a local and a remote
provider can be tested in the same run.

| var | provider | meaning | default |
|---|---|---|---|
| `VOXTRAL_TEST_BASE_URL` | voxtral | OpenAI-shaped base URL of the running server | none — unset skips the test |
| `VOXTRAL_TEST_MODEL` | voxtral | model id sent to the endpoint | `VoxtralProvider.default_model` |
| `VOXTRAL_TEST_API_KEY` | voxtral | bearer token | `EMPTY` |
| `VOXTRAL_TEST_LANGUAGE` | voxtral | language pin | `VoxtralProvider.default_language` |
| `QWEN3_TEST_BASE_URL` | qwen3 | OpenAI-shaped base URL of the running server | none — unset skips the test |
| `QWEN3_TEST_MODEL` | qwen3 | model id | `Qwen3TTS.default_model` |
| `QWEN3_TEST_VOICE` | qwen3 | voice id | `Qwen3TTS.default_voice` |
| `QWEN3_TEST_API_KEY` | qwen3 | bearer token | `EMPTY` |
| `QWEN3_TEST_INSTRUCTIONS` | qwen3 | forwarded as the request's `instructions` field | the Japanese "speak only Japanese" sentence |

`ASR_MODEL_PATH` and `SUPERTONIC_MODEL_DIR` (already documented in
`app/stt/README.md` and `app/tts/README.md`) work here the same way they do
for the real servers — the nemotron test resolves the model path exactly the
way `NemotronProvider.serve()`'s launcher does.

## What each test actually checks

* **nemotron** — builds a `NemotronSTT` with a real `silero.VAD`, opens a
  `.stream()`, pushes `tests/fixtures/sample_ja.wav` at realtime pace plus
  trailing silence, and asserts it sees at least one `INTERIM_TRANSCRIPT`
  before a `FINAL_TRANSCRIPT` whose text contains `テスト` — i.e. that the
  whole VAD-commits-the-turn design in `app/stt/nemotron.py`'s module
  docstring actually behaves the way that docstring claims.
* **voxtral** — builds the batch (`STT_USE_REALTIME` unset) `openai.STT` and
  calls `.recognize()` once with the fixture as a single buffer, asserting a
  non-empty transcript comes back. It does not assert *what* the transcript
  says: per `app/stt/voxtral.py`'s own docstring, this endpoint's behavior
  has never been verified, and this test is partly how you verify it.
* **supertonic** — builds the real `openai.TTS` client against a freshly
  booted local server and calls `.synthesize()`, asserting non-empty audio at
  a plausible duration.
* **qwen3** — the same `.synthesize()` call against your vLLM endpoint.

Both TTS tests assert the returned sample rate is 24000 Hz, not each
provider's `native_sample_rate` — `livekit.plugins.openai.TTS` always decodes
to its own fixed output rate, so 24000 is what a real agent session actually
receives regardless of what the wire format was.
