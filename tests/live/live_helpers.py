"""Shared plumbing for the live provider suite: process lifecycle, readiness
polling, and turning the checked-in fixture into the ``rtc.AudioFrame``s a
LiveKit STT stream actually consumes.

Kept dependency-light on purpose: reading the fixture uses stdlib ``wave``
rather than soundfile/numpy, so the STT half of this suite (nemotron,
voxtral) needs nothing beyond this project's core dependencies. Only the
Supertonic TTS test needs the ``supertonic`` extra, because that is the
engine under test, not a fixture-loading concern.
"""

from __future__ import annotations

import array
import asyncio
import subprocess
import socket
import time
import wave
from pathlib import Path

import aiohttp
from livekit import rtc
from livekit.agents.stt import RecognizeStream

FIXTURE_WAV = Path(__file__).resolve().parents[1] / "fixtures" / "sample_ja.wav"

# What the fixture says (synthesized once from this project's own Supertonic
# voice - see tests/fixtures/README.md). "テスト" is the one loanword every ASR
# engine that hears the clip at all renders correctly, so live STT tests check
# for that substring rather than a brittle exact match on the whole sentence.
FIXTURE_EXPECTED_SUBSTRING = "テスト"


def free_port() -> int:
    """An ephemeral TCP port, so a live test server never collides with the
    real deployment's :8003/:8004."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_subprocess(args: list[str], *, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Popen dup()s the fd for the child; our handle is safe to close right
    # after the call instead of leaking it for the rest of the test.
    with log_path.open("w") as log_file:
        return subprocess.Popen(args, env=env, stdout=log_file, stderr=subprocess.STDOUT)


def stop_subprocess(proc: subprocess.Popen | None, *, timeout: float = 10.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


async def wait_until_ready(
    url: str, *, timeout: float, proc: subprocess.Popen, log_path: Path
) -> None:
    """Poll ``url`` until it answers 200, or fail with the server's own log.

    Checking ``proc.poll()`` too means a server that crashes on startup fails
    fast with its stderr attached, instead of this spinning until the timeout.
    """
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text(errors="replace")[-4000:] if log_path.exists() else ""
                raise AssertionError(f"server exited early (code {proc.returncode}):\n{tail}")
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
                    if resp.status == 200:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.2)
    tail = log_path.read_text(errors="replace")[-4000:] if log_path.exists() else ""
    raise AssertionError(f"{url} never became ready within {timeout}s:\n{tail}")


def _read_mono_pcm16(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path} is not 16-bit PCM")
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
        sample_rate = wav.getframerate()
    if channels == 1:
        return frames, sample_rate
    # Fold to mono by keeping every channel-th sample, stdlib-only.
    samples = array.array("h", frames)
    return samples[::channels].tobytes(), sample_rate


def fixture_frames(*, chunk_ms: int = 20) -> list[rtc.AudioFrame]:
    """The checked-in fixture, chunked into realtime-sized frames.

    ``RecognizeStream`` resamples on push, so these are handed over at the
    fixture's own 44.1kHz - the same thing a real session hands a provider
    whose native rate differs from the room's.
    """
    pcm, sample_rate = _read_mono_pcm16(FIXTURE_WAV)
    bytes_per_sample = 2
    chunk_samples = max(1, sample_rate * chunk_ms // 1000)
    chunk_bytes = chunk_samples * bytes_per_sample
    frames = []
    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset : offset + chunk_bytes]
        if not chunk:
            continue
        samples_per_channel = len(chunk) // bytes_per_sample
        frames.append(
            rtc.AudioFrame(
                chunk, sample_rate=sample_rate, num_channels=1, samples_per_channel=samples_per_channel
            )
        )
    return frames


def fixture_as_single_frame() -> rtc.AudioFrame:
    """The whole fixture as one frame, for the batch ``recognize()`` path."""
    pcm, sample_rate = _read_mono_pcm16(FIXTURE_WAV)
    return rtc.AudioFrame(
        pcm, sample_rate=sample_rate, num_channels=1, samples_per_channel=len(pcm) // 2
    )


def silence_frames(*, sample_rate: int, total_ms: int, chunk_ms: int = 20) -> list[rtc.AudioFrame]:
    """Trailing silence so a real VAD actually fires END_OF_SPEECH.

    The streaming providers here commit on VAD end-of-speech, not on the
    source running out of frames (see app/stt/nemotron.py's module
    docstring) - so a test that stops feeding audio the instant the sentence
    ends never gets a final.
    """
    chunk_samples = max(1, sample_rate * chunk_ms // 1000)
    chunk = b"\x00\x00" * chunk_samples
    count = max(1, total_ms // chunk_ms)
    return [
        rtc.AudioFrame(chunk, sample_rate=sample_rate, num_channels=1, samples_per_channel=chunk_samples)
        for _ in range(count)
    ]


async def push_realtime(stream: RecognizeStream, frames: list[rtc.AudioFrame]) -> None:
    """Push frames to an STT stream at the rate real audio arrives.

    Real pacing matters here: NemotronSTT's VAD runs inside the stream and
    segments on end-of-speech, so pushing a whole utterance in one burst
    would starve it of the "still speaking" signal a live session gives it
    frame by frame.
    """
    for frame in frames:
        stream.push_frame(frame)
        await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)
