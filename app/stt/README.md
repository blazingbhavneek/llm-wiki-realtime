# Speech-to-text providers

## 1. The contract

Every engine subclasses `STTProvider` (`base.py`) and implements
`build(settings, *, vad=None) -> livekit.agents.stt.STT`. The runtime layer never
constructs an STT itself: it calls `app.stt.build_stt(vad=session_vad)`, which
reads `STT_PROVIDER`, resolves the class through `REGISTRY`, fills an
`STTSettings` from the environment (`settings_from_env`) and hands back the
LiveKit STT the `AgentSession` is given. A provider whose `requires_vad` is True
**must** be passed the same VAD instance the session uses — it is what marks the
end of a turn — and `build()` raises `ValueError` without it. When we host the
model ourselves the provider also owns the server: `serve()`, reachable as
`python -m app.stt.<provider>`.

## 2. Choosing one

`STT_PROVIDER` — `nemotron` (default) or `voxtral`.

Everything else is shared, and each variable falls back to the selected
provider's class defaults:

| variable | meaning | default |
|---|---|---|
| `STT_PROVIDER` | which engine | `nemotron` |
| `STT_MODEL` | model id sent to the endpoint | provider `default_model` |
| `STT_BASE_URL` | OpenAI-shaped base URL | provider `default_base_url` |
| `STT_API_KEY` | bearer token; `EMPTY` means "send none" | `EMPTY` |
| `STT_LANGUAGE` | language pin — **read §7** | provider `default_language` |
| `STT_SAMPLE_RATE` | rate declared to the engine | provider `native_sample_rate` |
| `STT_AUTOMATIC_PUNCTUATION` | punctuation restoration (nemotron) | `true` |
| `STT_USE_REALTIME` | voxtral only: realtime WS instead of batch | `false` |

Nemotron's own server reads three more: `ASR_MODEL_PATH`, `ASR_SERVER_HOST`,
`ASR_SERVER_PORT` (`0.0.0.0:8003`).

## 3. The providers

| name | hosted by | endpoint | sample rate | streaming / interim | VAD required | what pins the language |
|---|---|---|---|---|---|---|
| `nemotron` | self — NeMo-Speech.cpp binary, CPU | `http://127.0.0.1:8003/v1` (`/realtime` WS + `/audio/transcriptions`) | 16 kHz, the model's native rate | yes / yes | **yes** — commits on VAD end-of-speech | `STT_LANGUAGE`, as an exact locale (`ja-JP`) |
| `voxtral` | vLLM | `http://127.0.0.1:8001/v1` | 16 kHz (plugin's realtime path sends 24 kHz PCM) | only if `STT_USE_REALTIME=true` | yes in batch mode (LiveKit's `StreamAdapter` segments) | `STT_LANGUAGE`, base tag only — the plugin truncates a locale, and its default is `en` |

`finals_are_utterances` is True for `nemotron` and for `voxtral` in batch mode;
it is False for `voxtral` in realtime mode until someone proves otherwise
against a live server. See §7.

## 4. Adding a vLLM-hosted model

1. copy `voxtral.py` to `<name>.py`;
2. subclass `STTProvider`, set `hosted_by = "vllm"`;
3. fill in the defaults and the capability flags — `native_sample_rate`,
   `language_is_locale` and `finals_are_utterances` are the three that fail as
   *bad transcripts* rather than as an error;
4. add one line to `REGISTRY` in `__init__.py`;
5. document its env block in this README and in `.env.example`.

## 5. Adding a locally-hosted model

Same five steps with `hosted_by = "self"`, plus:

6. implement `serve()` in the same file — client and server for one engine live
   together — and a `main()` / `if __name__ == "__main__"` so
   `uv run python -m app.stt.<name>` starts it;
7. add the heavy dependencies as an optional extra in `pyproject.toml`;
8. add the launch line and the port to `docs/OPERATIONS.md`.

## 6. Verifying without LiveKit

Build the engine once, then run it:

```bash
bash scripts/build_asr_server.sh          # one-off: compiles NeMo-Speech.cpp (CPU)
uv run python -m app.stt.nemotron         # ASR on :8003
```

Transcribe a file straight through the OpenAI-compatible batch route — no
LiveKit, no agent, no VAD:

```bash
curl -s http://127.0.0.1:8003/v1/audio/transcriptions \
  -F file=@sample.wav \
  -F model=nvidia/nemotron-3.5-asr-streaming-0.6b \
  -F language=ja-JP \
  -F automatic_punctuation=true
```

Send `language=ja` to the same command and compare: identical output to sending
no language at all means the locale lookup missed and the engine auto-detected
(§7). `tests/test_providers.py` covers the offline half — every registered
provider imports, builds settings, and declares all of its capability flags.

`NEMO_STT_DEBUG=1` traces the realtime client's own WebSocket dialog on stdout.

## 7. Gotchas already paid for

- **Nemotron matches its language prompt by exact locale.** The GGUF ships
  `ja-JP` and `ja-JA` but no bare `ja`; an unknown key falls back to `auto`
  language detection. `ja` is therefore silently auto-detect and `ja-JP` is
  Japanese — confirmed against the running server, where `language=ja` returns
  output byte-identical to sending no language at all. Because
  `livekit.plugins.openai.STT` sends `LanguageCode.language` — the base tag only
  — a configured `ja-JP` arrived there as `ja`, and every turn was language-
  detected. That is the reason `NemotronSTT` exists.
- **The two realtime dialects do not match, and neither side errors.**
  `openai.STT`'s `use_realtime=True` path speaks OpenAI's nested session shape
  (`session.audio.input.format.rate`, `session.audio.input.transcription.language`)
  while NeMo-Speech.cpp reads a flat one (`session.sample_rate`,
  `session.language`). No field lands: the server holds its 16 kHz default while
  being fed the plugin's 24 kHz PCM, and the result looks like a bad ASR model.
- **Something has to decide when an utterance ends.** LiveKit never flushes an
  STT stream — `audio_recognition` only ever calls `push_frame`, so one stream
  stays open for the whole session. The server's `--endpointing` would emit a
  final at every trailing-silence pause, and this project treats each final as a
  complete user utterance, so one spoken question would arrive as several
  (jfk.wav splits into four). So `--endpointing` stays off and the plugin commits
  on VAD end-of-speech instead: one speech segment, one final, one turn — the
  same boundary the old batch `StreamAdapter` pipeline had, and unlike
  session-level turn detection it keeps working while an uninterruptible speech
  plays, which is what barge-in needs.
- **A comment claiming "this streams" is not evidence that it streams.** The
  first Voxtral config carried exactly that comment while running as batch
  transcription, because `openai.STT` defaults to `use_realtime=False`. Check
  `capabilities.streaming`, not the comment.
