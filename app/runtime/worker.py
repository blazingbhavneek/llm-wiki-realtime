"""Process bootstrap: the LiveKit worker and the web server in one process.

``python -m app`` lands here. This is the process entry, so it is where the
environment is loaded - every module below is imported after that, including
``app.web.http``, whose routes therefore see the ``.env`` values.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

import uvicorn
from dotenv import load_dotenv
from livekit import agents
from livekit.plugins import silero

# This project's .env is the deployment configuration. It must override
# inherited shell values, which otherwise can silently route agents to stale
# STT/TTS/RAG services.
#
# Deliberately before the app imports below: this is the process entry, and
# app.web.http is imported from here, so the routes see the loaded env.
load_dotenv(override=True)

from app.config import Settings  # noqa: E402
from app.log import dbg  # noqa: E402
from app.runtime.entrypoint import entrypoint  # noqa: E402
from app.web.http import app  # noqa: E402  - the object uvicorn.Config is given
from app.web.tls import ensure_local_https_certificate  # noqa: E402


def prewarm(proc: agents.JobProcess) -> None:
    dbg("PREWARM_START")
    # This is the "how long do we wait before deciding he stopped" dial, and
    # it is the single biggest piece of the gap between the user going quiet
    # and the LLM being asked - the rest of that path measures ~140ms. Too
    # low and a mid-sentence breath ends the turn; too high and the assistant
    # feels slow. Silero's own default is 0.55s.
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=Settings.from_env().tuning.vad_min_silence_seconds,
    )
    dbg("PREWARM_DONE")


def worker_load() -> float:
    """Reserve one agent job without using the shared host's aggregate CPU load."""
    return 0.0


def disable_proxy_for_local_livekit() -> None:
    """Keep LiveKit's Python and native RTC clients off the corporate proxy."""
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "WS_PROXY",
        "WSS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "ws_proxy",
        "wss_proxy",
    ):
        os.environ.pop(name, None)


def build_web_server() -> uvicorn.Server:
    web = Settings.from_env().web
    server_options: dict[str, Any] = {
        "host": web.host,
        "port": web.port,
        "log_level": web.log_level,
    }

    if web.tls:
        cert_path, key_path, ca_path = ensure_local_https_certificate(web)
        print(f"HTTPS test server CA certificate: {ca_path}", flush=True)
        print("Trust this CA in your browser/OS before opening the HTTPS URL.", flush=True)
        server_options.update(ssl_certfile=str(cert_path), ssl_keyfile=str(key_path))
    else:
        print("Serving HTTP behind the TLS reverse proxy.", flush=True)

    return uvicorn.Server(uvicorn.Config(app, **server_options))


def start_web_server() -> tuple[uvicorn.Server, threading.Thread]:
    """Start Uvicorn once, fail fast if it cannot bind, then return control."""
    web = Settings.from_env().web
    web_server = build_web_server()
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            web_server.run()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve, name="movi-http", daemon=True)
    thread.start()

    deadline = time.monotonic() + web.start_timeout_seconds
    while thread.is_alive() and not web_server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    if not web_server.started:
        web_server.should_exit = True
        thread.join(timeout=1.0)
        if failures:
            raise RuntimeError("FastAPI server failed to start") from failures[0]
        raise RuntimeError("FastAPI server did not start before the timeout")

    print(
        "Combined server ready: "
        f"http{'s' if web.tls else ''}://"
        f"{web.host}:{web.port}",
        flush=True,
    )
    return web_server, thread


_AGENTS_CLI_SUBCOMMANDS = {"console", "start", "dev", "connect", "download-files"}


def run_combined_server() -> None:
    # agents.cli.run_app is itself a Click CLI expecting a subcommand
    # (start/dev/console/...); default to "start" (no file-watch respawn,
    # unlike "dev") so plain `uv run python -m app` runs the worker instead of
    # printing a help screen and exiting.
    if len(sys.argv) < 2 or sys.argv[1] not in _AGENTS_CLI_SUBCOMMANDS:
        sys.argv.insert(1, "start")

    # The native RTC client in spawned job processes does not honor NO_PROXY.
    # Clear inherited proxy settings before those processes are forked.
    disable_proxy_for_local_livekit()
    web_server, web_thread = start_web_server()
    try:
        agents.cli.run_app(
            agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                prewarm_fnc=prewarm,
                agent_name=Settings.from_env().livekit.agent_name,
                load_fnc=worker_load,
                load_threshold=0.95,
                num_idle_processes=1,
                # WorkerOptions otherwise unconditionally adopts
                # HTTPS_PROXY/HTTP_PROXY and ignores NO_PROXY.
                http_proxy=None,
                # Default (16) gives up and tears down the whole combined
                # server - including the FastAPI/frontend side - if LiveKit
                # isn't reachable yet. Keep retrying indefinitely instead so
                # the web server stays up while LiveKit comes online.
                max_retry=1_000_000,
            )
        )
    finally:
        web_server.should_exit = True
        web_thread.join(timeout=5.0)


def main() -> None:
    """``python -m app``: the agent worker and the web server in one process."""
    run_combined_server()
