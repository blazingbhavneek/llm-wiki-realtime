"""FastAPI token endpoint and LiveKit worker bootstrap.

This file wires the parts together and then gets out of the way: every decision
lives in ``Conductor.handle``. Nothing here reacts to anything - the LiveKit
callbacks below only translate session events into inbox events.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import trustme
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from livekit import agents, api, rtc
from livekit.agents import AgentSession, room_io
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.voice.events import CloseReason
from livekit.plugins import openai, silero

import agent as prompts
from agent import Assistant, AssistantDeps
from attention import Attention
from conductor import Conductor
from events import (
    IdleTick,
    ListenButtonChanged,
    UserSaidText,
    UserStartedSpeaking,
    UserStoppedSpeaking,
)
from memory import Memory
from nemo_stt import NemotronSTT
from research import ResearchPool, stream_url
from screen import Screen
from speaker import Speaker

# This project's .env is the deployment configuration. It must override
# inherited shell values, which otherwise can silently route agents to stale
# STT/TTS/RAG services.
load_dotenv(override=True)


# ============================================================
# HTTP frontend and LiveKit token service
# ============================================================

app = FastAPI()

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


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


def ensure_local_https_certificate() -> tuple[Path, Path, Path]:
    """Create a reusable local CA and server certificate for browser testing."""
    cert_dir = Path(os.getenv("TEXT_TEST_CERT_DIR", "certs"))
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "text-test-cert.pem"
    key_path = cert_dir / "text-test-key.pem"
    ca_path = cert_dir / "text-test-ca.pem"
    if cert_path.exists() and key_path.exists() and ca_path.exists():
        return cert_path, key_path, ca_path

    hostnames = [
        value.strip()
        for value in os.getenv(
            "TEXT_TEST_HTTPS_HOSTS",
            "localhost,127.0.0.1",
        ).split(",")
        if value.strip()
    ]
    ca = trustme.CA()
    certificate = ca.issue_cert(*hostnames)
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(cert_path)
    certificate.private_key_pem.write_to_path(key_path)
    return cert_path, key_path, ca_path


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIST / "index.html")


async def close_lkapi(lkapi: api.LiveKitAPI) -> None:
    try:
        if hasattr(lkapi, "aclose"):
            await lkapi.aclose()
        elif hasattr(lkapi, "close"):
            await lkapi.close()
    except Exception:
        pass


def _authenticate_token_request(request: Request) -> None:
    expected = os.getenv("TEXT_TEST_ACCESS_TOKEN")
    if not expected:
        return
    supplied = request.headers.get("authorization", "")
    if supplied.startswith("Bearer "):
        supplied = supplied[7:]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid token endpoint credentials")


@app.get("/token")
async def token(request: Request):
    """
    Create a new room and participant token for every browser connection.

    A room is intentionally not reused across page reloads: a fresh browser
    session must receive a fresh dispatch and its own agent greeting.
    """
    _authenticate_token_request(request)
    room = f"japanese-assistant-{uuid.uuid4().hex[:8]}"
    identity = f"user-{uuid.uuid4().hex[:8]}"

    livekit_api_key = os.getenv("LIVEKIT_API_KEY", "devkey")
    livekit_api_secret = os.getenv("LIVEKIT_API_SECRET", "secret")
    public_livekit_url = os.getenv("PUBLIC_LIVEKIT_URL", "wss://127.0.0.1:7880")
    livekit_api_url = os.getenv("LIVEKIT_URL", "http://127.0.0.1:7880")
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "japanese-wiki-agent")
    manual_dispatch = env_bool("LIVEKIT_MANUAL_DISPATCH", True)

    lkapi = None
    dispatch = None
    try:
        print(
            f"TOKEN HIT room={room} identity={identity} manual_dispatch={manual_dispatch}",
            flush=True,
        )
        lkapi = api.LiveKitAPI(livekit_api_url, livekit_api_key, livekit_api_secret)
        await lkapi.room.create_room(
            api.CreateRoomRequest(name=room, empty_timeout=60, max_participants=10)
        )

        if manual_dispatch:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(room=room, agent_name=agent_name, metadata="{}")
            )
            print(
                f"MANUAL DISPATCH CREATED room={room} "
                f"dispatch_id={getattr(dispatch, 'id', None)} agent_name={agent_name!r}",
                flush=True,
            )
        else:
            print(f"MANUAL DISPATCH SKIPPED room={room}; expecting auto-dispatch", flush=True)

        grant_kwargs = dict(room_join=True, room=room, can_publish=True, can_subscribe=True)
        try:
            grants = api.VideoGrants(**grant_kwargs, can_publish_data=True)
        except TypeError:
            grants = api.VideoGrants(**grant_kwargs)
            if hasattr(grants, "can_publish_data"):
                setattr(grants, "can_publish_data", True)

        jwt = (
            api.AccessToken(livekit_api_key, livekit_api_secret)
            .with_identity(identity)
            .with_name(identity)
            .with_grants(grants)
            .to_jwt()
        )
        return {
            "url": public_livekit_url,
            "token": jwt,
            "room": room,
            "identity": identity,
            "agent_dispatched": manual_dispatch,
            "agent_name": agent_name if manual_dispatch else None,
            "dispatch_id": getattr(dispatch, "id", None) if dispatch else None,
        }
    except Exception as exc:
        print("TOKEN_ENDPOINT_FAILED:", repr(exc), flush=True)
        print(traceback.format_exc(), flush=True)
        raise HTTPException(
            status_code=500,
            detail={"error": repr(exc), "room": room, "identity": identity},
        ) from exc
    finally:
        if lkapi is not None:
            await close_lkapi(lkapi)


# Mount after API routes so /health and /token remain handled by FastAPI.
# Absent before the first `npm run build`; the token endpoint still works.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


# ============================================================
# LiveKit worker
# ============================================================


def dbg(label: str, **fields: Any) -> None:
    print(
        "[AGENT DEBUG]",
        json.dumps({"label": label, **fields}, ensure_ascii=False, default=str),
        flush=True,
    )


def prewarm(proc: agents.JobProcess) -> None:
    dbg("PREWARM_START")
    # This is the "how long do we wait before deciding he stopped" dial, and
    # it is the single biggest piece of the gap between the user going quiet
    # and the LLM being asked - the rest of that path measures ~140ms. Too
    # low and a mid-sentence breath ends the turn; too high and the assistant
    # feels slow. Silero's own default is 0.55s.
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE_SECONDS", "0.55")),
    )
    dbg("PREWARM_DONE")


def worker_load() -> float:
    """Reserve one agent job without using the shared host's aggregate CPU load."""
    return 0.0


async def idle_ticker(inbox: asyncio.Queue) -> None:
    while True:
        await asyncio.sleep(1.0)
        inbox.put_nowait(IdleTick())


async def entrypoint(ctx: agents.JobContext) -> None:
    await ctx.connect()
    dbg(
        "ENTRYPOINT_CONNECTED",
        room=ctx.room.name,
        LLM_MODEL=os.getenv("LLM_MODEL"),
        STT_MODEL=os.getenv("STT_MODEL"),
        TTS_MODEL=os.getenv("TTS_MODEL"),
        RAG_STREAM_URL=stream_url(),
    )

    inbox: asyncio.Queue = asyncio.Queue()
    memory = Memory()

    stt = NemotronSTT(
        model=os.getenv("STT_MODEL", "nvidia/nemotron-3.5-asr-streaming-0.6b"),
        base_url=os.getenv("STT_BASE_URL", "http://127.0.0.1:8003/v1"),
        api_key=os.getenv("STT_API_KEY", "EMPTY"),
        # Must stay a full locale. openai.STT sent the base tag only, which
        # this model does not have a language prompt for, so every turn was
        # silently language-detected instead of pinned - see nemo_stt.
        language=os.getenv("STT_LANGUAGE", "ja-JP"),
        # The same VAD the session uses, so one speech segment is one final is
        # one UserSaidText - the streaming ASR keeps _on_transcript's
        # "every final is a complete utterance" assumption true.
        vad=ctx.proc.userdata["vad"],
    )
    llm = openai.LLM(
        model=os.getenv("LLM_MODEL", "gemma-4-31B"),
        base_url=os.getenv("LLM_BASE_URL", "http://10.160.144.101:51029/v1"),
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
    )
    base_tts = openai.TTS(
        model=os.getenv("TTS_MODEL", "supertonic-3"),
        voice=os.getenv("TTS_VOICE", "F1"),
        base_url=os.getenv("TTS_BASE_URL", "http://127.0.0.1:8002/v1"),
        api_key=os.getenv("TTS_API_KEY", "EMPTY"),
        # tts_server.py returns Supertonic's native 44.1kHz WAV; "wav" lets
        # LiveKit's own decoder resample it, unlike "pcm" which assumes raw
        # 24kHz samples already on the wire.
        response_format="wav",
        # The OpenAI-compatible adapter forwards this as the request's
        # `instructions` field. tts_server.py ignores it - Supertonic's
        # language pin is TTS_LANG, read server-side - but it's harmless to
        # keep sending for parity with other OpenAI-compatible TTS servers.
        instructions=os.getenv(
            "TTS_INSTRUCTIONS",
            "日本語のみで発話してください。英語、ローマ字、他の言語へ切り替えないでください。",
        ),
    )

    assistant = Assistant()
    session = AgentSession[AssistantDeps](
        userdata=AssistantDeps(inbox=inbox, memory=memory),
        # LiveKit closes the whole session after this many *consecutive*
        # unrecoverable errors from STT, LLM or TTS, and the STT counter only
        # resets on a transcript. Its default of 3 is far too tight for a
        # hand-started local stack: an ASR server that comes up a minute after
        # the agent burns all three before a single transcript can reset them,
        # and the session dies before the user has said anything. The pipeline
        # rebuilds a failed stream by itself, so a generous budget only means
        # "keep trying while a service is coming back".
        conn_options=SessionConnectOptions(
            max_unrecoverable_errors=int(
                os.getenv("SESSION_MAX_UNRECOVERABLE_ERRORS", "10")
            ),
        ),
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=llm,
        # Speaker installs the right TTS per speech; this is only the fallback
        # the session needs at construction time.
        tts=base_tts,
        turn_handling={
            # Turn completion is unused - the Conductor reads STT finals - so
            # keep detection on plain VAD instead of loading a turn model.
            "turn_detection": "vad",
            # Every speech we create is uninterruptible so that the Conductor,
            # not the pipeline, decides what a nearby voice means. LiveKit
            # otherwise substitutes silence into the STT while such a speech
            # plays, which would make a real barge-in impossible to hear.
            "interruption": {"discard_audio_if_uninterruptible": False},
        },
    )

    speaker = Speaker(session, assistant, base_tts, inbox)
    pool = ResearchPool(inbox)
    screen = Screen(ctx.room)
    conductor = Conductor(
        inbox=inbox,
        attention=Attention(),
        speaker=speaker,
        pool=pool,
        memory=memory,
        screen=screen,
    )

    # ---- producers: translate, never decide -----------------------------

    @session.on("user_state_changed")
    def _on_user_state(event: Any) -> None:
        if event.new_state == "speaking":
            inbox.put_nowait(UserStartedSpeaking())
        elif event.old_state == "speaking":
            inbox.put_nowait(UserStoppedSpeaking())

    @session.on("user_input_transcribed")
    def _on_transcript(event: Any) -> None:
        # Read straight off the STT rather than off turn completion: LiveKit
        # drops turn completion entirely while an uninterruptible speech plays,
        # which is exactly when a barge-in has to be heard.
        if not event.is_final:
            return
        text = (event.transcript or "").strip()
        if text:
            inbox.put_nowait(UserSaidText(text, from_text_input=False))

    def _on_text_input(_session: AgentSession, event: room_io.TextInputEvent) -> None:
        text = event.text.strip()
        if text:
            inbox.put_nowait(UserSaidText(text, from_text_input=True))

    # ---- failures: say so, out loud, to the log and to the browser ------
    # Without these a dying session is invisible. The orb is drawn from the
    # browser's own microphone state, so a session that has closed still looks
    # like it is listening: red, labelled "聞いています", and deaf. Every
    # component failure now leaves a line in the log naming which one it was.

    @session.on("error")
    def _on_session_error(event: Any) -> None:
        error = getattr(event, "error", None)
        recoverable = bool(getattr(error, "recoverable", False))
        source = getattr(event, "source", None)
        dbg(
            "SESSION_ERROR",
            component=getattr(error, "type", type(error).__name__),
            provider=getattr(source, "provider", type(source).__name__),
            model=getattr(source, "model", ""),
            recoverable=recoverable,
            error=repr(getattr(error, "error", error)),
        )
        if not recoverable:
            screen.set_agent_status(
                "degraded", str(getattr(error, "type", "") or "unknown")
            )

    @session.on("close")
    def _on_session_close(event: Any) -> None:
        reason = getattr(event, "reason", None)
        reason_text = getattr(reason, "value", str(reason))
        dbg("SESSION_CLOSED", reason=reason_text, error=repr(getattr(event, "error", None)))
        if reason is not CloseReason.ERROR:
            return  # a normal shutdown; the job is already on its way out
        # The session is done but the agent is still a participant, so the
        # browser would keep talking to a corpse. Tell it, then leave the room
        # so it also sees the participant go and can start a fresh dispatch.
        screen.set_agent_status("closed", reason_text)
        ctx.shutdown(reason="agent session closed on an unrecoverable error")

    @ctx.room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != "attention":
            return
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except Exception:
            return
        if payload.get("type") == "listen":
            inbox.put_nowait(ListenButtonChanged(bool(payload.get("held"))))

    await session.start(
        room=ctx.room,
        agent=assistant,
        room_options=room_io.RoomOptions(
            text_input=room_io.TextInputOptions(text_input_cb=_on_text_input),
            audio_input=True,
            audio_output=True,
            text_output=True,
        ),
    )

    # The TTS is owned by Speaker, which installs the right one per speech.
    assistant.update_options(tts=speaker.default_tts)

    conductor_task = asyncio.create_task(conductor.run(), name="conductor")
    ticker_task = asyncio.create_task(idle_ticker(inbox), name="idle-ticker")

    conductor.queue_notice(prompts.GREETING)
    inbox.put_nowait(IdleTick())  # kick the ladder so the greeting plays now

    async def shutdown() -> None:
        ticker_task.cancel()
        conductor_task.cancel()
        await pool.cancel_all()
        await speaker.aclose()
        await asyncio.gather(ticker_task, conductor_task, return_exceptions=True)

    ctx.add_shutdown_callback(shutdown)
    dbg("SESSION_STARTED", room=ctx.room.name)


# ============================================================
# Process bootstrap
# ============================================================


def build_web_server() -> uvicorn.Server:
    tls_enabled = env_bool("TEXT_TEST_TLS", False)
    server_options: dict[str, Any] = {
        "host": os.getenv("TEXT_TEST_HOST", "0.0.0.0"),
        "port": int(os.getenv("TEXT_TEST_PORT", "51027")),
        "log_level": os.getenv("TEXT_TEST_LOG_LEVEL", "info"),
    }

    if tls_enabled:
        cert_path, key_path, ca_path = ensure_local_https_certificate()
        print(f"HTTPS test server CA certificate: {ca_path}", flush=True)
        print("Trust this CA in your browser/OS before opening the HTTPS URL.", flush=True)
        server_options.update(ssl_certfile=str(cert_path), ssl_keyfile=str(key_path))
    else:
        print("Serving HTTP behind the TLS reverse proxy.", flush=True)

    return uvicorn.Server(uvicorn.Config(app, **server_options))


def start_web_server() -> tuple[uvicorn.Server, threading.Thread]:
    """Start Uvicorn once, fail fast if it cannot bind, then return control."""
    web_server = build_web_server()
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            web_server.run()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve, name="movi-http", daemon=True)
    thread.start()

    deadline = time.monotonic() + float(os.getenv("TEXT_TEST_START_TIMEOUT_SECONDS", "10"))
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
        f"http{'s' if env_bool('TEXT_TEST_TLS', False) else ''}://"
        f"{os.getenv('TEXT_TEST_HOST', '0.0.0.0')}:{os.getenv('TEXT_TEST_PORT', '51027')}",
        flush=True,
    )
    return web_server, thread


_AGENTS_CLI_SUBCOMMANDS = {"console", "start", "dev", "connect", "download-files"}


def run_combined_server() -> None:
    # agents.cli.run_app is itself a Click CLI expecting a subcommand
    # (start/dev/console/...); default to "start" (no file-watch respawn,
    # unlike "dev") so plain `uv run server.py` runs the worker instead of
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
                agent_name=os.getenv("LIVEKIT_AGENT_NAME", "japanese-wiki-agent"),
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


if __name__ == "__main__":
    run_combined_server()
