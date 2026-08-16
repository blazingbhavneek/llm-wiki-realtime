"""STT providers, exercised the way a LiveKit ``AgentSession`` actually uses
them: through ``STTProvider.build()``, over a real streaming/batch call, with
the fixture in ``tests/fixtures/sample_ja.wav`` as the audio.

Unlike ``tests/test_providers.py`` this suite is not offline:

* ``nemotron`` is self-hosted, so its test builds the real NeMo-Speech.cpp
  binary's server on a scratch port from the model already on disk
  (``models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf``, built by
  ``scripts/build_asr_server.sh``) and skips if either is missing.
* ``voxtral`` is vLLM-hosted, so its test only runs when
  ``VOXTRAL_TEST_BASE_URL`` points at a real server; otherwise it skips. See
  ``tests/live/README.md`` for the full env var list.

Run explicitly - the filename deliberately does not start with ``test`` so
``python -m unittest discover -s tests -t .`` (the fast offline suite) never
imports it; heavy/real network imports happening incidentally during that
run is exactly the failure mode ``tests/test_providers.py``'s
``LazyRegistryTests`` exists to catch. Run this file with:

    python -m unittest discover -s tests/live -t . -p "live_*.py" -q
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from livekit.agents import stt, utils
from livekit.plugins import silero

from app.stt.base import STTSettings
from app.stt.nemotron import BINARY, DEFAULT_MODEL, NemotronProvider
from app.stt.voxtral import VoxtralProvider
from tests.live import live_helpers


def _nemotron_model_path() -> Path:
    # Same resolution NemotronProvider.serve()'s main() uses, so this test
    # honors a custom ASR_MODEL_PATH the same way the real server would.
    return Path(os.getenv("ASR_MODEL_PATH", str(DEFAULT_MODEL)))


_NEMOTRON_AVAILABLE = BINARY.exists() and _nemotron_model_path().exists()
_VOXTRAL_BASE_URL = os.getenv("VOXTRAL_TEST_BASE_URL")


@unittest.skipUnless(
    _NEMOTRON_AVAILABLE,
    f"NeMo-Speech.cpp binary or model missing (binary={BINARY}, "
    f"model={_nemotron_model_path()}); build with scripts/build_asr_server.sh",
)
class NemotronLiveTest(unittest.IsolatedAsyncioTestCase):
    """Boots the real ASR server on a scratch port and streams the fixture through it."""

    async def asyncSetUp(self) -> None:
        self.port = live_helpers.free_port()
        self.log_path = Path(tempfile.gettempdir()) / f"nemotron_live_{self.port}.log"
        self.proc = live_helpers.start_subprocess(
            [
                str(BINARY),
                "serve",
                "--asr-model",
                str(_nemotron_model_path()),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--no-ui",
            ],
            env=os.environ.copy(),
            log_path=self.log_path,
        )
        await live_helpers.wait_until_ready(
            f"http://127.0.0.1:{self.port}/v1/models",
            timeout=60.0,
            proc=self.proc,
            log_path=self.log_path,
        )

    async def asyncTearDown(self) -> None:
        live_helpers.stop_subprocess(self.proc)

    async def test_streams_a_final_transcript_through_build(self) -> None:
        vad = silero.VAD.load()
        settings = STTSettings(
            provider="nemotron",
            model=NemotronProvider.default_model,
            base_url=f"http://127.0.0.1:{self.port}/v1",
            api_key="EMPTY",
            language="ja-JP",
            sample_rate=NemotronProvider.native_sample_rate,
            automatic_punctuation=True,
        )

        # http_context.open() stands in for the job context an AgentSession
        # normally provides - see NemotronSTT._ensure_session().
        async with utils.http_context.open():
            stt_obj = NemotronProvider().build(settings, vad=vad)
            self.assertTrue(stt_obj.capabilities.streaming)
            self.assertTrue(stt_obj.capabilities.interim_results)

            stream = stt_obj.stream()
            finals: list[str] = []
            interims_seen = False

            async def collect() -> None:
                nonlocal interims_seen
                async for ev in stream:
                    if ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
                        interims_seen = True
                    elif ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                        finals.append(ev.alternatives[0].text)

            collector = asyncio.create_task(collect())
            try:
                frames = live_helpers.fixture_frames()
                frames += live_helpers.silence_frames(
                    sample_rate=frames[0].sample_rate, total_ms=1200
                )
                await live_helpers.push_realtime(stream, frames)
                stream.end_input()
                await asyncio.wait_for(collector, timeout=20.0)
            finally:
                if not collector.done():
                    collector.cancel()
                await stream.aclose()

        self.assertTrue(interims_seen, "no INTERIM_TRANSCRIPT before the final - streaming is broken")
        self.assertTrue(finals, "nemotron never emitted a FINAL_TRANSCRIPT for the fixture")
        transcript = "".join(finals)
        self.assertIn(
            live_helpers.FIXTURE_EXPECTED_SUBSTRING,
            transcript,
            f"unexpected transcript: {transcript!r}",
        )


@unittest.skipUnless(
    _VOXTRAL_BASE_URL,
    "set VOXTRAL_TEST_BASE_URL to a running vLLM Voxtral endpoint to run this test",
)
class VoxtralLiveTest(unittest.IsolatedAsyncioTestCase):
    """Streams the fixture through vLLM's ``/v1/realtime`` endpoint.

    Verified live against a vLLM box serving the "-Realtime-" checkpoint: its
    batch ``/v1/audio/transcriptions`` answers 400 to every request (see the
    module docstring in ``app/stt/voxtral.py`` for why - it's the wrong
    endpoint for that checkpoint, not a broken fixture), so this test forces
    ``STT_USE_REALTIME=true`` to exercise the path that checkpoint actually
    speaks: ``VoxtralRealtimeSTT`` against vLLM's own realtime protocol.
    """

    async def asyncSetUp(self) -> None:
        self._previous_use_realtime = os.environ.get("STT_USE_REALTIME")
        os.environ["STT_USE_REALTIME"] = "true"

    async def asyncTearDown(self) -> None:
        if self._previous_use_realtime is None:
            os.environ.pop("STT_USE_REALTIME", None)
        else:
            os.environ["STT_USE_REALTIME"] = self._previous_use_realtime

    async def test_streams_a_final_transcript_through_build(self) -> None:
        vad = silero.VAD.load()
        settings = STTSettings(
            provider="voxtral",
            model=os.getenv("VOXTRAL_TEST_MODEL", VoxtralProvider.default_model),
            base_url=_VOXTRAL_BASE_URL,
            api_key=os.getenv("VOXTRAL_TEST_API_KEY", "EMPTY"),
            language=os.getenv("VOXTRAL_TEST_LANGUAGE", VoxtralProvider.default_language),
            sample_rate=VoxtralProvider.native_sample_rate,
            automatic_punctuation=True,
        )

        async with utils.http_context.open():
            stt_obj = VoxtralProvider().build(settings, vad=vad)
            self.assertTrue(stt_obj.capabilities.streaming)

            stream = stt_obj.stream()
            finals: list[str] = []

            async def collect() -> None:
                # Unlike nemotron's server, vLLM's realtime protocol has no
                # "commit acknowledged" event, so this stream never signals
                # its own end - it stays open for the next utterance, same as
                # a real AgentSession would keep it open all session long.
                # Stop as soon as this one utterance's final has arrived
                # instead of waiting for the iterator to end on its own.
                async for ev in stream:
                    if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                        finals.append(ev.alternatives[0].text)
                        return

            collector = asyncio.create_task(collect())
            try:
                frames = live_helpers.fixture_frames()
                frames += live_helpers.silence_frames(
                    sample_rate=frames[0].sample_rate, total_ms=1200
                )
                await live_helpers.push_realtime(stream, frames)
                stream.end_input()
                await asyncio.wait_for(collector, timeout=30.0)
            finally:
                if not collector.done():
                    collector.cancel()
                await stream.aclose()

        # Not asserting *what* it says: vLLM's realtime protocol has no
        # language field at all (see app/stt/voxtral.py), so what a Japanese
        # clip transcribes as is exactly the thing this test exists to learn.
        self.assertTrue(finals, "voxtral realtime never emitted a FINAL_TRANSCRIPT for the fixture")
        self.assertTrue(
            "".join(finals).strip(), "voxtral realtime returned only empty transcripts"
        )


if __name__ == "__main__":
    unittest.main()
