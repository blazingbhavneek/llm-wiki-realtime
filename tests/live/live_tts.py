"""TTS providers, exercised the way ``app.core.speaker`` actually uses them:
through ``TTSProvider.build()`` and a real ``.synthesize()`` call.

Unlike ``tests/test_providers.py`` this suite is not offline:

* ``supertonic`` is self-hosted, so its test boots the real
  ``app.tts.supertonic`` server on a scratch port from the ONNX model already
  on disk (the Supertone/supertonic-3 HF cache, or ``SUPERTONIC_MODEL_DIR``)
  and skips if neither the ``supertonic`` package nor the model is available.
* ``qwen3`` is vLLM-hosted, so its test only runs when ``QWEN3_TEST_BASE_URL``
  points at a real server; otherwise it skips. See ``tests/live/README.md``
  for the full env var list.

Run explicitly - the filename deliberately does not start with ``test`` so
``python -m unittest discover -s tests -t .`` (the fast offline suite) never
imports it; this file's Supertonic availability check imports ``supertonic``
directly, which would otherwise falsely trip
``tests/test_providers.py``'s ``LazyRegistryTests``. Run this file with:

    python -m unittest discover -s tests/live -t . -p "live_*.py" -q
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from livekit.agents import tts as lk_tts
from livekit.agents import utils

from app.tts.base import TTSSettings
from app.tts.qwen3 import Qwen3TTS
from app.tts.supertonic import SupertonicTTS
from tests.live import live_helpers


def _supertonic_available() -> bool:
    try:
        import supertonic  # noqa: F401
        from huggingface_hub import snapshot_download
    except ImportError:
        return False

    model_dir = os.getenv("SUPERTONIC_MODEL_DIR")
    if model_dir:
        return Path(model_dir).exists()
    try:
        snapshot_download(repo_id="Supertone/supertonic-3", local_files_only=True)
    except Exception:
        return False
    return True


async def _collect_audio(tts_obj: lk_tts.TTS, text: str) -> tuple[bytes, int]:
    stream = tts_obj.synthesize(text)
    chunks: list[bytes] = []
    sample_rate = 0
    try:
        async for ev in stream:
            sample_rate = ev.frame.sample_rate
            chunks.append(ev.frame.data.tobytes())
    finally:
        await stream.aclose()
    return b"".join(chunks), sample_rate


_SUPERTONIC_AVAILABLE = _supertonic_available()
_QWEN3_BASE_URL = os.getenv("QWEN3_TEST_BASE_URL")
_SAMPLE_TEXT = "こんにちは、これはテストです。"


@unittest.skipUnless(
    _SUPERTONIC_AVAILABLE,
    "supertonic package or the Supertone/supertonic-3 model is not available locally; "
    "`uv sync --extra supertonic` and download the model, or set SUPERTONIC_MODEL_DIR",
)
class SupertonicLiveTest(unittest.IsolatedAsyncioTestCase):
    """Boots the real ONNX-backed TTS server on a scratch port and synthesizes through it."""

    async def asyncSetUp(self) -> None:
        self.port = live_helpers.free_port()
        self.log_path = Path(tempfile.gettempdir()) / f"supertonic_live_{self.port}.log"
        env = os.environ.copy()
        env["TTS_SERVER_HOST"] = "127.0.0.1"
        env["TTS_SERVER_PORT"] = str(self.port)
        self.proc = live_helpers.start_subprocess(
            [sys.executable, "-m", "app.tts.supertonic"],
            env=env,
            log_path=self.log_path,
        )
        await live_helpers.wait_until_ready(
            f"http://127.0.0.1:{self.port}/health",
            timeout=60.0,
            proc=self.proc,
            log_path=self.log_path,
        )

    async def asyncTearDown(self) -> None:
        live_helpers.stop_subprocess(self.proc)

    async def test_synthesizes_audio_through_build(self) -> None:
        settings = TTSSettings(
            provider="supertonic",
            model=SupertonicTTS.default_model,
            voice=SupertonicTTS.default_voice,
            base_url=f"http://127.0.0.1:{self.port}/v1",
            api_key="EMPTY",
            language="ja",
            instructions="",
            response_format=SupertonicTTS.default_response_format,
            speed=1.05,
            reply_min_chars=SupertonicTTS.default_reply_min_chars,
            report_min_chars=SupertonicTTS.default_report_min_chars,
            stream_context_chars=240,
        )

        async with utils.http_context.open():
            tts_obj = SupertonicTTS().build(settings)
            self.assertFalse(tts_obj.capabilities.streaming)
            audio, sample_rate = await _collect_audio(tts_obj, _SAMPLE_TEXT)

        self.assertGreater(len(audio), 0, "supertonic returned no audio for the sample text")
        # openai.TTS always decodes to its own fixed output rate regardless of
        # the provider's native sample rate - see livekit.plugins.openai.tts.SAMPLE_RATE.
        self.assertEqual(sample_rate, 24000)
        seconds = (len(audio) / 2) / sample_rate
        self.assertGreater(seconds, 0.5, f"suspiciously short synthesis: {seconds:.2f}s")


@unittest.skipUnless(
    _QWEN3_BASE_URL,
    "set QWEN3_TEST_BASE_URL to a running vLLM Qwen3-TTS endpoint to run this test",
)
class Qwen3LiveTest(unittest.IsolatedAsyncioTestCase):
    """Synthesizes against a real vLLM-hosted Qwen3-TTS endpoint."""

    async def test_synthesizes_audio_through_build(self) -> None:
        settings = TTSSettings(
            provider="qwen3",
            model=os.getenv("QWEN3_TEST_MODEL", Qwen3TTS.default_model),
            voice=os.getenv("QWEN3_TEST_VOICE", Qwen3TTS.default_voice),
            base_url=_QWEN3_BASE_URL,
            api_key=os.getenv("QWEN3_TEST_API_KEY", "EMPTY"),
            language="ja",
            instructions=os.getenv(
                "QWEN3_TEST_INSTRUCTIONS", "日本語のみで発話してください。"
            ),
            response_format=Qwen3TTS.default_response_format,
            speed=1.0,
            reply_min_chars=Qwen3TTS.default_reply_min_chars,
            report_min_chars=Qwen3TTS.default_report_min_chars,
            stream_context_chars=240,
        )

        async with utils.http_context.open():
            tts_obj = Qwen3TTS().build(settings)
            self.assertFalse(tts_obj.capabilities.streaming)
            audio, sample_rate = await _collect_audio(tts_obj, _SAMPLE_TEXT)

        self.assertGreater(len(audio), 0, "qwen3 returned no audio for the sample text")
        self.assertEqual(sample_rate, 24000)


if __name__ == "__main__":
    unittest.main()
