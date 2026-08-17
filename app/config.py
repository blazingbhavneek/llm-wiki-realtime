"""Deployment configuration, read from the environment once.

``.env`` stays the single deployment file and every variable keeps the name it
has today. Provider-specific settings deliberately live with their provider
(``app.tts.base.TTSSettings`` and friends) so that adding an engine never
touches this file; what is here is the wiring that is true regardless of which
models are loaded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)


@dataclass(frozen=True)
class LiveKitSettings:
    url: str  # LIVEKIT_URL
    public_url: str  # PUBLIC_LIVEKIT_URL
    api_key: str  # LIVEKIT_API_KEY
    api_secret: str  # LIVEKIT_API_SECRET
    agent_name: str  # LIVEKIT_AGENT_NAME
    manual_dispatch: bool  # LIVEKIT_MANUAL_DISPATCH
    max_unrecoverable_errors: int  # SESSION_MAX_UNRECOVERABLE_ERRORS
    agent_http_port: int  # LIVEKIT_AGENT_HTTP_PORT
    agent_log_level: str  # LIVEKIT_AGENT_LOG_LEVEL

    @classmethod
    def from_env(cls) -> "LiveKitSettings":
        return cls(
            url=env_str("LIVEKIT_URL", "http://127.0.0.1:7880"),
            public_url=env_str("PUBLIC_LIVEKIT_URL", "wss://127.0.0.1:7880"),
            api_key=env_str("LIVEKIT_API_KEY", "devkey"),
            api_secret=env_str("LIVEKIT_API_SECRET", "secret"),
            agent_name=env_str("LIVEKIT_AGENT_NAME", "japanese-wiki-agent"),
            manual_dispatch=env_bool("LIVEKIT_MANUAL_DISPATCH", True),
            max_unrecoverable_errors=env_int("SESSION_MAX_UNRECOVERABLE_ERRORS", 10),
            # livekit-agents logs tool-call execution (which function, what
            # args) at DEBUG, invisible at its prod default of INFO. Same
            # unannounced-default situation as agent_http_port above.
            agent_log_level=env_str("LIVEKIT_AGENT_LOG_LEVEL", "INFO"),
            # livekit-agents' own default (8081) is not configurable through
            # its own env - it's a WorkerOptions(port=...) constructor arg -
            # so this project exposes it the same way it exposes every other
            # port, rather than leaving an unannounced bind that can collide
            # with an unrelated service already on 8081.
            agent_http_port=env_int("LIVEKIT_AGENT_HTTP_PORT", 8081),
        )


@dataclass(frozen=True)
class WebSettings:
    host: str  # TEXT_TEST_HOST
    port: int  # TEXT_TEST_PORT
    log_level: str  # TEXT_TEST_LOG_LEVEL
    tls: bool  # TEXT_TEST_TLS
    cert_dir: str  # TEXT_TEST_CERT_DIR
    https_hosts: str  # TEXT_TEST_HTTPS_HOSTS
    access_token: str  # TEXT_TEST_ACCESS_TOKEN
    start_timeout_seconds: float  # TEXT_TEST_START_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "WebSettings":
        return cls(
            host=env_str("TEXT_TEST_HOST", "0.0.0.0"),
            port=env_int("TEXT_TEST_PORT", 51027),
            log_level=env_str("TEXT_TEST_LOG_LEVEL", "info"),
            tls=env_bool("TEXT_TEST_TLS", False),
            cert_dir=env_str("TEXT_TEST_CERT_DIR", "certs"),
            https_hosts=env_str("TEXT_TEST_HTTPS_HOSTS", "localhost,127.0.0.1"),
            access_token=env_str("TEXT_TEST_ACCESS_TOKEN", ""),
            start_timeout_seconds=env_float("TEXT_TEST_START_TIMEOUT_SECONDS", 10),
        )


@dataclass(frozen=True)
class TuningSettings:
    vad_min_silence_seconds: float  # VAD_MIN_SILENCE_SECONDS
    vad_activation_threshold: float  # VAD_ACTIVATION_THRESHOLD

    @classmethod
    def from_env(cls) -> "TuningSettings":
        return cls(
            vad_min_silence_seconds=env_float("VAD_MIN_SILENCE_SECONDS", 0.55),
            # Higher than Silero's 0.5 default: distant voices and room noise
            # must be more speech-like before they open a turn.
            vad_activation_threshold=env_float("VAD_ACTIVATION_THRESHOLD", 0.70),
        )


@dataclass(frozen=True)
class Settings:
    livekit: LiveKitSettings
    web: WebSettings
    tuning: TuningSettings
    stt_provider: str  # STT_PROVIDER
    tts_provider: str  # TTS_PROVIDER
    llm_provider: str  # LLM_PROVIDER
    rag_provider: str  # RAG_PROVIDER

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            livekit=LiveKitSettings.from_env(),
            web=WebSettings.from_env(),
            tuning=TuningSettings.from_env(),
            stt_provider=env_str("STT_PROVIDER", "nemotron"),
            tts_provider=env_str("TTS_PROVIDER", "supertonic"),
            llm_provider=env_str("LLM_PROVIDER", "openai_compatible"),
            rag_provider=env_str("RAG_PROVIDER", "llm_wiki"),
        )
