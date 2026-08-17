# Setting up llm-wiki-realtime on a new machine

This is the "I just cloned this on my work PC, now what" doc. It covers
everything **[`docs/OPERATIONS.md`](OPERATIONS.md)** assumes is already done:
installing things, getting model files onto the disk, standing up Docker/
LiveKit/Caddy for the first time, and picking which ASR/TTS engine to run.

Once a machine is set up, go back to `OPERATIONS.md` — it's the day-to-day
runbook (start order, stop order, troubleshooting) and there's no point
duplicating it here. This doc is the parts before that becomes true, plus a
detailed cheat sheet for swapping components, since that's the thing you said
you'll actually need on the work PC.

**Edit this file freely.** It's yours — when a knob changes or you find a new
gotcha, add it here instead of relying on memory.

---

## 0. The shape of it, in one picture

```
 Browser
    |  https:// + wss://  (ONLY port it ever touches)
    v
 Caddy :51027  (Docker, TLS termination)
    |-- /rtc*  --> livekit-server :7880  (Docker, WebRTC signalling + media)
    '-- else   --> python -m app :51028  (plain HTTP, the agent + web server)
                        |
                        |-- talks to --> ASR server   (nemotron :8003, or a vLLM box)
                        |-- talks to --> TTS server   (supertonic :8004, or a vLLM box)
                        |-- talks to --> LLM server   (llama-server / vLLM :8000, outside this repo)
                        '-- talks to --> RAG backend  (placeholder :8005, or the real wiki service)
```

Every one of those addresses is one line in `.env`. Nothing is auto-detected
except the LAN IP the bootstrap script guesses on first run.

---

## 1. Install prerequisites (once per machine)

| tool                                         | why                                                                                  | check                      |
| -------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------- |
| **Docker + `docker compose` plugin** | runs LiveKit and Caddy                                                               | `docker compose version` |
| **`uv`**                             | Python package/venv manager this project uses                                        | `uv --version`           |
| **Node.js + npm**                      | builds the frontend (`frontend/`)                                                  | `node --version`         |
| **git**                                | cloning, and`scripts/build_asr_server.sh` vendors NeMo-Speech.cpp via git          |                            |
| **A C++ toolchain + cmake**            | only if you're building`nemotron` (the local ASR engine) — gcc/clang, cmake, make | `cmake --version`        |

Arch: `sudo pacman -S docker docker-compose cmake base-devel nodejs npm`
Debian/Ubuntu: `sudo apt install docker.io docker-compose-plugin cmake build-essential nodejs npm`

`scripts/setup_https_livekit.sh` (section 3 below) checks for Docker itself
and tells you what's missing, so don't stress over getting this list perfect
up front.

---

## 2. Clone and install

```bash
git clone <this repo's url> llm-wiki-realtime
cd llm-wiki-realtime

uv sync                        # base install
# add --extra supertonic on whichever machine actually runs Supertonic locally —
# it pulls in onnxruntime/soundfile/numpy/huggingface_hub, which the agent
# process itself never needs:
uv sync --extra supertonic
```

---

## 3. One-time bootstrap: env, HTTPS cert, LiveKit, Caddy

```bash
cp .env.example .env
scripts/setup_https_livekit.sh          # no argument = auto-detect this machine's LAN IP
# or pin it explicitly:
scripts/setup_https_livekit.sh 10.0.0.50
```

This one script (read it — it's short and safe to re-run) does all of the
following:

1. Checks Docker is installed and running (starts the daemon via `systemctl`
   if it isn't, and falls back to `sudo docker` if your user isn't in the
   `docker` group yet).
2. Writes `PUBLIC_HOST`, `PUBLIC_LIVEKIT_URL`, `TEXT_TEST_HTTPS_HOSTS`,
   `LIVEKIT_NODE_IP` and a couple of port/TLS defaults into `.env`.
3. Generates a fresh self-signed HTTPS certificate (via `trustme`) covering
   `localhost`, `127.0.0.1`, and whatever host you gave it, and writes it to
   `certs/`.
4. Starts `livekit-server` + Caddy (`docker compose -f docker-compose.caddy.yml up -d`)
   and waits for LiveKit to answer.
5. Writes `frontend/.env.development.local` so Vite's dev server can also
   serve HTTPS with the same cert.
6. Builds the frontend (`npm install && npm run build`) so `python -m app`
   has something to serve at `/`.

**What it does NOT do** — you still do these yourself:

- Trust the CA in your browser (section 4 below).
- Start the ASR/TTS/LLM/RAG servers and the agent itself (section 5–6).

Re-run it any time `PUBLIC_HOST` needs to change (moving machines, new LAN
IP) — see `OPERATIONS.md` section 4 for the mechanics of *why*.

---

## 4. Trust the certificate (once per machine)

```
CA file: certs/text-test-ca.pem

Firefox: Settings -> Privacy & Security -> Certificates -> View Certificates
         -> Authorities -> Import -> tick "Trust this CA to identify websites"
Chrome / system trust on Arch:
         sudo trust anchor certs/text-test-ca.pem
Chrome / system trust on Debian/Ubuntu:
         sudo cp certs/text-test-ca.pem /usr/local/share/ca-certificates/text-test-ca.crt
         sudo update-ca-certificates
```

Clicking through the browser warning instead of importing the CA does **not**
work here: Firefox refuses to open a `wss://` WebSocket through an untrusted
cert even after you've clicked past the page warning, so the mic silently
never connects. Import the CA properly.

---

## 5. Get each model server actually runnable

Everything below is a choice of *engine*; which one loads is one `.env`
variable per component (`STT_PROVIDER`, `TTS_PROVIDER`, ...). This section is
about getting the **default** engines runnable at all; section 6/7 cover
switching to something else.

### LLM — outside this repo entirely

Anything that speaks `/v1/chat/completions` and supports tool calling
(llama-server, vLLM, SGLang). Point `LLM_BASE_URL` / `LLM_MODEL` at it. No
setup lives in this repo.

### ASR — default `nemotron` (self-hosted, CPU)

```bash
bash scripts/build_asr_server.sh      # once: clones+builds NVIDIA/NeMo-Speech.cpp into vendor/
```

You also need the model file itself at
`models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf` (repo-root `models/`
directory — create it if it doesn't exist). **This repo does not fetch it for
you** — there's no download step in `build_asr_server.sh`. If you already have
it on another machine, copy it over; otherwise get the GGUF from wherever
NeMo-Speech.cpp's own docs point (or set `ASR_MODEL_PATH` to wherever you put
it, if not the default path).

```bash
uv run python -m app.stt.nemotron     # :8003, once the binary + model exist
```

### TTS — default `supertonic` (self-hosted, CPU, ONNX)

```bash
uv sync --extra supertonic            # if you haven't already (section 2)
```

**The weights are also not auto-downloaded.** `supertonic.py` calls
`huggingface_hub.snapshot_download(..., local_files_only=True)` — it refuses
to hit the network and just fails if the model isn't already cached. Get it
onto the machine first:

```bash
uv run huggingface-cli download Supertone/supertonic-3
# or, if you keep model files somewhere specific:
# SUPERTONIC_MODEL_DIR=/path/to/supertonic-3   (set in .env)
```

```bash
uv run python -m app.tts.supertonic   # :8004
```

### RAG — `llm_wiki`, pointed at either the placeholder or the real wiki

For a first smoke test (no real wiki backend needed), point at the bundled
stub — it's already what `.env.example` sets:

```bash
uv run python -m app.rag.placeholder  # :8005 — dummy answers, never real content
```

When you're ready to hit the real wiki backend, edit `.env`:

```
LLM_WIKI_BASE_URL=http://<the wiki service's host>:8000
LLM_WIKI_PREFIX=/llm-wiki
LLM_WIKI_DATABASE=moove_wiki          # or whatever database name the real service uses
```

and don't start `app.rag.placeholder` at all.

---

## 6. Boot order

Order matters — see `OPERATIONS.md` section 1 for why (short version: the
agent gives up after enough consecutive failures against a component that
isn't up yet, so start it last).

```bash
docker compose -f docker-compose.caddy.yml up -d   # if not already up from section 3

uv run python -m app.stt.nemotron     &   # or whichever ASR you picked (section 7)
uv run python -m app.tts.supertonic   &   # or whichever TTS you picked (section 8)
uv run python -m app.rag.placeholder  &   # or point .env at the real wiki instead

uv run python -m app                      # LAST — agent + web server, :51028
```

Then open `https://<PUBLIC_HOST>:51027` — never `:51028` directly (see
`OPERATIONS.md`'s TROUBLESHOOTING section for what that looks like when you
get it wrong).

---

## 7. Switching the ASR engine

`STT_PROVIDER` in `.env` picks the engine. Everything else about it — model
id, endpoint, sample rate — comes from that provider's own defaults, so you
only need to touch the handful of variables below.

### `nemotron` (default) — self-hosted, CPU

Already covered in section 5. No further config needed beyond `STT_LANGUAGE`
if you want a locale other than `ja-JP` — and it must be a **full locale**
(`ja-JP`), not a bare `ja`, or Nemotron silently falls back to language
auto-detection.

### `voxtral` batch or realtime

```
STT_PROVIDER=voxtral
STT_BASE_URL=http://<vllm-box>:<port>/v1
STT_MODEL=<the model id vLLM is serving>
STT_LANGUAGE=ja                 # base tag only here, not a locale
```

Despite the name, `voxtral.py`'s **batch mode** (the default —
`STT_USE_REALTIME` unset) is a plain OpenAI-compatible
`POST /v1/audio/transcriptions` client. Point `STT_MODEL`/`STT_BASE_URL` at a
non-realtime Voxtral model to use it.

```
STT_USE_REALTIME=true
```

turns on the *other* path, which speaks vLLM's own bespoke `/v1/realtime`
WebSocket protocol (not the OpenAI Realtime API — see the big comment at the
top of `app/stt/voxtral.py` if you're curious why that distinction matters).
This generalizes to another model **only if vLLM serves that model through
the same realtime entrypoint** — test it before relying on it. If it doesn't
work, `app/stt/README.md` section 4 walks through copying `voxtral.py` to a
new file as a real second provider (5 small steps).

### `qwen` — VAD-segmented Qwen3-ASR, forced Japanese

```
STT_PROVIDER=qwen
STT_BASE_URL=http://10.160.144.101:51027/v1
STT_API_KEY=EMPTY
STT_MODEL=Qwen/Qwen3-ASR-1.7B
```

This provider uses the normal batch transcription endpoint after LiveKit has
ended an utterance with VAD. It always sends the vLLM endpoint's
`language=ja` request value; `STT_LANGUAGE` cannot override it. It
therefore has no interim transcripts, but it does not auto-detect into another
language.

**Verify a new ASR endpoint before wiring it into the agent:**

```bash
curl -s http://<host>:<port>/v1/audio/transcriptions \
  -F file=@sample.wav -F model=<model id> -F language=ja
```

or the repo's own live test, which builds the real client and checks it
against a fixture WAV:

```bash
VOXTRAL_TEST_BASE_URL=http://<host>:<port>/v1 VOXTRAL_TEST_MODEL=<model id> \
  python -m unittest tests.live.live_stt.VoxtralLiveTest -v
```

---

## 8. Switching the TTS engine (Supertonic → Qwen3), voices, and custom voices

```
TTS_PROVIDER=qwen3
TTS_BASE_URL=http://<vllm-box>:<port>/v1
TTS_MODEL=Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
TTS_VOICE=Ono_Anna
```

That's the whole switch — the audio format (`pcm` @ 24kHz) and sentence
thresholds come from `Qwen3TTS`'s own defaults. You no longer need to run
`app.tts.supertonic` at all once this is set.

### Picking the Japanese voice

`TTS_VOICE` is just the voice-id string forwarded to the server as-is.
`Ono_Anna` is this project's default; any other voice name the server has
registered works the same way — ask whoever runs the vLLM box what's
available, or check `GET /v1/models` / whatever listing endpoint that Qwen3
deployment exposes.

### **Important: the language knob is different for Qwen3 than for Supertonic**

This is the single easiest thing to get wrong switching engines:

| engine         | which variable actually pins the language                                                         | what happens to the other one                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `supertonic` | `TTS_LANG` (read server-side as an SDK `lang=` code)                                          | `TTS_INSTRUCTIONS` is sent but the server **ignores** it                                              |
| `qwen3`      | `TTS_INSTRUCTIONS` (forwarded as the request's `instructions` field, which the server honors) | `TTS_LANG` is **never even sent** — `Qwen3TTS.build()` doesn't pass a `language` argument at all |

So when you're on `qwen3`, the thing keeping it from drifting into English
mid-sentence is `TTS_INSTRUCTIONS`, not `TTS_LANG`. The default in
`.env.example` is already a "speak only Japanese, don't switch languages"
sentence — leave it as-is unless you have a specific reason to change the
wording, and if speech starts drifting languages, that's the variable to look
at.

### Using a custom / cloned reference voice

There is **no reference-voice upload mechanism in this repo** — `TTS_VOICE`
only ever sends a name string. Registering a new or cloned voice (giving it a
name Qwen3-TTS will recognize) happens **on the vLLM/Qwen3-TTS server itself**,
which this repo doesn't host and doesn't control (it's "the shared GPU box" —
whoever administers that box handles voice registration, per the
CustomVoice model's own docs). Once a voice is registered there under some
name, using it here is just:

```
TTS_VOICE=<the name it was registered under>
```

**Verify before wiring it in:**

```bash
curl -s http://<host>:<port>/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice","voice":"Ono_Anna","input":"こんにちは","response_format":"pcm"}' \
  -o /tmp/tts.raw

QWEN3_TEST_BASE_URL=http://<host>:<port>/v1 QWEN3_TEST_VOICE=Ono_Anna \
  python -m unittest tests.live.live_tts.Qwen3LiveTest -v
```

---

## 9. All the other knobs

Everything below is a `.env` variable (or a one-off env var on the
`python -m app` command). Full per-provider tables live in each package's
README (`app/stt/README.md`, `app/tts/README.md`, `app/llm/README.md`,
`app/rag/README.md`) — this is the cross-cutting stuff.

| variable                             | what it controls                                                                                                                                                                                                                     | default      |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ |
| `VAD_MIN_SILENCE_SECONDS`          | trailing silence before a turn counts as "over." Lower = snappier, cuts people off; higher = feels slow. The single biggest controllable chunk of end-to-end latency.                                                                | `0.55`     |
| `VAD_ACTIVATION_THRESHOLD`         | Silero confidence required before audio starts a speech turn. Raise it to ignore distant voices and room noise; lower it if close, quiet speech is missed.                                                                            | `0.70`     |
| `SESSION_MAX_UNRECOVERABLE_ERRORS` | consecutive STT/LLM/TTS failures the session tolerates before LiveKit closes it. LiveKit's own default (3) is too tight for a hand-started stack — a backend that comes up a minute late can burn through it before the first word. | `10`       |
| `TURN_TIMING=1`                    | per-turn latency breakdown on`python -m app`'s stdout                                                                                                                                                                              | off          |
| `NEMO_STT_DEBUG=1`                 | traces Nemotron's realtime WebSocket dialog                                                                                                                                                                                          | off          |
| `LIVEKIT_AGENT_LOG_LEVEL=DEBUG`    | shows tool-call execution (which function, what args) — invisible at the prod default`INFO`                                                                                                                                       | `INFO`     |
| `RAG_PLAN_TIMEOUT_SECONDS`         | watchdog budget until the wiki's`plan` frame arrives                                                                                                                                                                               | `5`        |
| `RAG_LEVEL_TIMEOUT_SECONDS`        | watchdog gap budget between frames once planning is done                                                                                                                                                                             | `20`       |
| `RAG_STREAM_MAX_RETRIES`           | extra attempts granted to one question on failure                                                                                                                                                                                    | `1`        |
| `TEXT_TEST_ACCESS_TOKEN`           | if set, gates`GET /token` behind `Authorization: Bearer <this>` — leave unset for local/LAN use                                                                                                                                 | unset (open) |
| `LIVEKIT_AGENT_HTTP_PORT`          | the agent worker's own internal health port                                                                                                                                                                                          | `8081`     |

### The one gotcha that isn't in any README: LiveKit's API key lives in two places

`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` in `.env` is what `python -m app`
uses to **mint** tokens for the browser. But the actual `livekit-server`
container reads its accepted keys from **`livekit.yaml`** (`keys: devkey: secret`), which is mounted into the container as a static file — it does
**not** read `.env`. If you change the key/secret pair in `.env` for a
non-local deployment, you must also edit the `keys:` block in `livekit.yaml`
to match and recreate the container:

```bash
docker compose -f docker-compose.caddy.yml up -d --force-recreate
```

Otherwise every token the agent hands the browser gets rejected by a server
still trusting the old secret, which looks like a connection failure with no
obviously related cause.

---

## 10. Moving to another machine, or splitting services across machines

Covered in full in `OPERATIONS.md` section 4 — `PUBLIC_HOST` /
`LIVEKIT_NODE_IP` need to be the address the *browser* can reach, every
`*_BASE_URL` needs to be reachable *from the agent machine*, and every
self-hosted server needs to bind `0.0.0.0`. Re-run
`scripts/setup_https_livekit.sh <new-ip>` after any address change.

---

## 11. Stopping everything, and troubleshooting

Also in `OPERATIONS.md` (sections 2 and 6) — the kill commands, the footguns
around `pkill` patterns matching more than you meant, and the fix for every
failure mode this stack has actually produced (cert warnings, "wait_pc_connection
timed out", the orb going deaf mid-session, two servers fighting over one
port). Read it before re-deriving a fix from scratch.
