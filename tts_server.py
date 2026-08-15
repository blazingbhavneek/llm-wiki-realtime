"""OpenAI-compatible ``/v1/audio/speech`` server around the local Supertonic-3 TTS SDK.

Runs the model directly in this process (ONNX Runtime, CPU) so ``server.py``'s
``openai.TTS`` client can point ``TTS_BASE_URL`` at this server the same way it
would point at a real OpenAI-compatible endpoint.
"""

from __future__ import annotations

import base64
import io
import json
import os
import uuid
from typing import Any

import numpy as np
import soundfile as sf
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from huggingface_hub import snapshot_download
from supertonic import TTS

load_dotenv(override=True)

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
    buf = io.BytesIO()
    sf.write(buf, wav.reshape(-1), sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def pcm16_bytes(wav: np.ndarray, src_rate: int, dst_rate: int = 24000) -> bytes:
    """Raw PCM16LE mono, resampled to ``dst_rate`` (linear interpolation is
    plenty for speech and keeps this dependency-free beyond numpy)."""
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
