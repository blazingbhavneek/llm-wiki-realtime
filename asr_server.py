"""Launches the local ASR server (OpenAI-compatible ``/v1/audio/transcriptions``).

The engine itself is NVIDIA/NeMo-Speech.cpp, a compiled C++ binary built once
with ``scripts/build_asr_server.sh`` (CPU backend, so it never touches the GPU
reserved for the LLM). This just execs it with this project's model/env
conventions so ``uv run asr_server.py`` sits alongside ``uv run tts_server.py``
and ``uv run server.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent
BINARY = ROOT / "vendor" / "NeMo-Speech.cpp" / "build" / "cpu-server" / "bin" / "nemo-speech"
DEFAULT_MODEL = ROOT / "models" / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"


def main() -> None:
    if not BINARY.exists():
        raise SystemExit(
            f"ASR engine binary not found at {BINARY}\n"
            "Build it once with: bash scripts/build_asr_server.sh"
        )

    model_path = Path(os.getenv("ASR_MODEL_PATH", str(DEFAULT_MODEL)))
    if not model_path.exists():
        raise SystemExit(f"ASR model not found at {model_path}")

    args = [
        str(BINARY),
        "serve",
        "--asr-model",
        str(model_path),
        "--host",
        os.getenv("ASR_SERVER_HOST", "0.0.0.0"),
        "--port",
        os.getenv("ASR_SERVER_PORT", "8003"),
        "--no-ui",
        # Endpointing is deliberately left off. It would emit a final at every
        # trailing-silence pause, and server.py treats each final as a whole
        # user utterance, so one spoken question would arrive as several.
        # nemo_stt.NemotronSTT commits on VAD end-of-speech instead, which is
        # the same turn boundary the batch pipeline had.
    ]
    os.execv(str(BINARY), args)


if __name__ == "__main__":
    main()
