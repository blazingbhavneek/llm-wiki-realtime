"""Supertonic-3: the provider the agent talks to, and the server that hosts it.

The second half of this file is an OpenAI-compatible ``/v1/audio/speech``
server around the local Supertonic-3 TTS SDK. It runs the model directly in
that process (ONNX Runtime, CPU) so the ``openai.TTS`` client below can point
``TTS_BASE_URL`` at this server the same way it would point at a real
OpenAI-compatible endpoint. ``serve()`` — what ``python -m app.tts.supertonic``
runs — is that server.

The heavy dependencies (supertonic/onnxruntime, soundfile, numpy,
huggingface_hub, uvicorn) are imported inside the functions that need them, so
``build()`` — the path the agent process takes — pulls in none of them. Only
the host needs them installed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import uuid
from typing import TYPE_CHECKING, Any

# FastAPI is the one server import that stays at module scope: the routes below
# are registered on ``app`` at import time, and FastAPI resolves their
# annotations against this module's globals. It costs nothing on the client
# path - the agent process already runs a FastAPI web server. The imports that
# would cost something (supertonic/onnxruntime, soundfile, numpy,
# huggingface_hub, uvicorn) live inside the functions that use them, so
# ``build()`` pulls in none of them.
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.tts.base import TTSProvider, TTSSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from livekit.agents import tts as lk_tts
    from supertonic import TTS


class SupertonicTTS(TTSProvider):
    name = "supertonic"
    hosted_by = "self"

    default_model = "supertonic-3"
    default_voice = "F1"
    default_base_url = "http://127.0.0.1:8004/v1"
    default_response_format = "wav"
    native_sample_rate = 44100
    streams_audio = False
    honors_instructions = False
    supports_voice_listing = False  # no GET /audio/voices; voices come from get_voice_style

    default_reply_min_chars = 30
    default_report_min_chars = 180

    def build(self, settings: TTSSettings) -> "lk_tts.TTS":
        from livekit.plugins import openai

        return openai.TTS(
            model=settings.model,
            voice=settings.voice,
            base_url=settings.base_url,
            api_key=settings.api_key,
            # tts_server.py returns Supertonic's native 44.1kHz WAV; "wav" lets
            # LiveKit's own decoder resample it, unlike "pcm" which assumes raw
            # 24kHz samples already on the wire.
            response_format=settings.response_format,
            # The OpenAI-compatible adapter forwards this as the request's
            # `instructions` field. tts_server.py ignores it - Supertonic's
            # language pin is TTS_LANG, read server-side - but it's harmless to
            # keep sending for parity with other OpenAI-compatible TTS servers.
            instructions=settings.instructions,
        )

    def serve(self, settings: TTSSettings) -> None:
        main()


# --- the server ------------------------------------------------------------

app = FastAPI()

DEFAULT_LANG = os.getenv("TTS_LANG", "ja")
DEFAULT_VOICE = os.getenv("TTS_VOICE", "F1")
DEFAULT_STEPS = int(os.getenv("TTS_STEPS", "8"))

_engine: TTS | None = None
_voice_styles: dict[str, Any] = {}


def get_engine() -> TTS:
    """Load the ONNX pipeline once, from the model already on disk.

    ``local_files_only=True`` resolves the standard HF cache without a network
    call, reusing whatever the user already downloaded to
    ``Supertone/supertonic-3`` instead of Supertonic's own duplicate
    ``~/.cache/supertonic3`` download path.
    """
    from huggingface_hub import snapshot_download
    from supertonic import TTS

    global _engine
    if _engine is None:
        model_dir = os.getenv("SUPERTONIC_MODEL_DIR") or snapshot_download(
            repo_id="Supertone/supertonic-3", local_files_only=True
        )
        _engine = TTS(model="supertonic-3", model_dir=model_dir, auto_download=False)
    return _engine


def get_voice_style(name: str) -> Any:
    style = _voice_styles.get(name)
    if style is None:
        style = get_engine().get_voice_style(name)
        _voice_styles[name] = style
    return style


def wav_bytes(wav: np.ndarray, sample_rate: int) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, wav.reshape(-1), sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def pcm16_bytes(wav: np.ndarray, src_rate: int, dst_rate: int = 24000) -> bytes:
    """Raw PCM16LE mono, resampled to ``dst_rate`` (linear interpolation is
    plenty for speech and keeps this dependency-free beyond numpy)."""
    import numpy as np

    samples = wav.reshape(-1).astype(np.float32)
    if src_rate != dst_rate and samples.size:
        duration = samples.shape[0] / src_rate
        x_old = np.linspace(0.0, duration, num=samples.shape[0], endpoint=False)
        x_new = np.linspace(0.0, duration, num=int(round(duration * dst_rate)), endpoint=False)
        samples = np.interp(x_new, x_old, samples).astype(np.float32)
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict:
    return {
        "object": "list",
        "data": [{"id": "supertonic-3", "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/audio/speech")
async def audio_speech(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    text = (payload.get("input") or payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="'input' is required")

    response_format = str(payload.get("response_format") or "wav").lower()
    if response_format not in ("wav", "pcm"):
        raise HTTPException(status_code=400, detail="response_format must be 'wav' or 'pcm'")

    voice_name = payload.get("voice") or DEFAULT_VOICE
    try:
        style = get_voice_style(voice_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"unknown voice {voice_name!r}") from exc

    speed = float(payload.get("speed") or 1.05)
    lang = payload.get("lang") or DEFAULT_LANG
    stream_format = payload.get("stream_format") or "audio"

    engine = get_engine()
    wav, _duration = engine.synthesize(
        text=text,
        voice_style=style,
        total_steps=DEFAULT_STEPS,
        speed=speed,
        lang=lang,
    )

    if response_format == "pcm":
        audio_bytes = pcm16_bytes(wav, engine.sample_rate)
        mime = "audio/pcm"
    else:
        audio_bytes = wav_bytes(wav, engine.sample_rate)
        mime = "audio/wav"

    request_id = uuid.uuid4().hex

    if stream_format == "sse":

        async def events():
            delta = {
                "type": "speech.audio.delta",
                "delta": base64.b64encode(audio_bytes).decode("ascii"),
            }
            yield f"data: {json.dumps(delta)}\n\n"
            done = {"type": "speech.audio.done", "usage": {"input_tokens": 0, "output_tokens": 0}}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"x-request-id": request_id}
        )

    return Response(content=audio_bytes, media_type=mime, headers={"x-request-id": request_id})


def main() -> None:
    # Deliberately not at import time: this module is imported by the agent
    # process too, and load_dotenv(override=True) there would clobber the
    # environment the worker was started with.
    from dotenv import load_dotenv

    load_dotenv(override=True)

    import uvicorn

    # Warm the model at boot rather than on the first request.
    get_engine()
    uvicorn.run(
        app,
        host=os.getenv("TTS_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("TTS_SERVER_PORT", "8004")),
        log_level=os.getenv("TTS_SERVER_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
