"""LiveKit voice agent backed by llm-wiki's realtime research stream."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from livekit import agents, api
from livekit.agents import Agent, AgentSession, RunContext, function_tool, room_io
from livekit.agents import tokenize, tts as lk_tts
from livekit.agents.llm import StopResponse
from livekit.plugins import openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
import trustme
import uvicorn

from attention import AttentionFSM
from delivery import DeliveryQueue
from rag_client import cancel_run, mirror, stream_answer, stream_url
from scheduler import SpeechScheduler


load_dotenv()


# ============================================================
# HTTP frontend and LiveKit token service
# ============================================================

app = FastAPI()

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
SESSION_COOKIE = "movi_session"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


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
            "localhost,127.0.0.1,10.160.152.38",
        ).split(",")
        if value.strip()
    ]
    ca = trustme.CA()
    certificate = ca.issue_cert(*hostnames)
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(cert_path)
    certificate.private_key_pem.write_to_path(key_path)
    return cert_path, key_path, ca_path


@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIST / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


async def close_lkapi(lkapi: api.LiveKitAPI):
    try:
        if hasattr(lkapi, "aclose"):
            await lkapi.aclose()
        elif hasattr(lkapi, "close"):
            await lkapi.close()
    except Exception:
        pass


def _session_secret() -> bytes:
    return os.getenv(
        "TEXT_TEST_SESSION_SECRET",
        os.getenv("LIVEKIT_API_SECRET", "local-development-only"),
    ).encode("utf-8")


def _sign_session(session_id: str) -> str:
    signature = hmac.new(
        _session_secret(), session_id.encode("utf-8"), hashlib.sha256
    ).digest()
    value = session_id.encode("utf-8") + b"." + signature
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _read_session(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        padding = "=" * (-len(cookie) % 4)
        value = base64.urlsafe_b64decode(cookie + padding)
        session_id, signature = value.rsplit(b".", 1)
        expected = hmac.new(_session_secret(), session_id, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        return session_id.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


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
async def token(request: Request, response: Response):
    """
    Reuse a stable room derived from a signed browser session cookie and mint a
    fresh participant token. Manual dispatch stays disabled by default because
    running it alongside auto-dispatch creates two agents in one room.
    """
    _authenticate_token_request(request)
    session_id = _read_session(request.cookies.get(SESSION_COOKIE))
    if session_id is None:
        session_id = secrets.token_urlsafe(24)
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session(session_id),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            secure=(
                request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
                == "https"
                or env_bool("TEXT_TEST_SECURE_COOKIE", False)
            ),
            samesite="lax",
        )
    room_key = hmac.new(
        _session_secret(), session_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
    room = f"japanese-assistant-{room_key}"
    identity = f"user-{uuid.uuid4().hex[:8]}"

    livekit_api_key = os.getenv("LIVEKIT_API_KEY", "devkey")
    livekit_api_secret = os.getenv("LIVEKIT_API_SECRET", "secret")
    public_livekit_url = os.getenv(
        "PUBLIC_LIVEKIT_URL",
        "wss://10.160.152.38:7880",
    )
    livekit_api_url = os.getenv(
        "LIVEKIT_URL",
        "http://10.160.152.38:7880",
    )
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "")
    manual_dispatch = env_bool("LIVEKIT_MANUAL_DISPATCH", False)

    lkapi = None
    dispatch = None
    try:
        print(
            f"TOKEN HIT room={room} identity={identity} "
            f"manual_dispatch={manual_dispatch}",
            flush=True,
        )
        lkapi = api.LiveKitAPI(
            livekit_api_url,
            livekit_api_key,
            livekit_api_secret,
        )
        await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room,
                empty_timeout=60,
                max_participants=10,
            )
        )

        if manual_dispatch:
            dispatch_req = api.CreateAgentDispatchRequest(
                room=room,
                agent_name=agent_name,
                metadata="{}",
            )
            dispatch = await lkapi.agent_dispatch.create_dispatch(dispatch_req)
            print(
                f"MANUAL DISPATCH CREATED room={room} "
                f"dispatch_id={getattr(dispatch, 'id', None)} "
                f"agent_name={agent_name!r}",
                flush=True,
            )
        else:
            print(
                f"MANUAL DISPATCH SKIPPED room={room}; expecting auto-dispatch",
                flush=True,
            )

        grant_kwargs = dict(
            room_join=True,
            room=room,
            can_publish=True,
            can_subscribe=True,
        )
        try:
            grants = api.VideoGrants(
                **grant_kwargs,
                can_publish_data=True,
            )
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
            detail={
                "error": repr(exc),
                "room": room,
                "identity": identity,
            },
        ) from exc
    finally:
        if lkapi is not None:
            await close_lkapi(lkapi)


# Mount after API routes so /health and /token remain handled by FastAPI.
app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


def dbg(label: str, message: str = "", **fields: Any) -> None:
    print(
        "[AGENT DEBUG]",
        json.dumps({"label": label, "message": message, **fields}, ensure_ascii=False, default=str),
        flush=True,
    )


async def publish_duck(room: Any, ducked: bool) -> None:
    if room is None:
        return
    try:
        await room.local_participant.publish_data(
            json.dumps({"type": "duck", "ducked": ducked}).encode("utf-8"),
            reliable=True,
            topic="attention",
        )
    except Exception as exc:
        dbg("ATTENTION_PUBLISH_FAILED", error=repr(exc))


@dataclass
class ResearchRun:
    local_id: str
    question: str
    task: asyncio.Task | None = None
    run_id: str | None = None
    plan_version: int = 0


@dataclass
class WikiVoiceState:
    session: Optional[AgentSession] = None
    room: Any = None
    delivery: DeliveryQueue = field(default_factory=DeliveryQueue)
    attention: AttentionFSM = field(default_factory=AttentionFSM)
    scheduler: Optional[SpeechScheduler] = None
    coordinator: Optional["ResearchCoordinator"] = None


def _objective_phrase(objective: str) -> str:
    value = re.sub(
        r"(?:を|について)?(?:特定|確認|検証|説明|調査|整理|比較|把握|評価)する$",
        "",
        objective.strip(),
    )
    return value[:34] or "必要な情報"


def plan_ack(event: dict[str, Any]) -> str:
    levels = list(event.get("levels") or [])
    objectives = [_objective_phrase(str(level.get("objective") or "")) for level in levels]
    objectives = [objective for objective in objectives if objective]
    if len(objectives) < 2 or event.get("planning_fallback"):
        return ""
    if len(objectives) == 2:
        joined = f"{objectives[0]}と、{objectives[1]}"
    else:
        joined = "、".join(objectives[:-1]) + f"、それから{objectives[-1]}"
    return f"調べる観点を整理しました。{joined}、この{len(objectives)}つから見ます。"


class ResearchCoordinator:
    """Connect turns, SSE events, the durable queue, and the speech floor."""

    def __init__(self, state: WikiVoiceState) -> None:
        self.state = state
        self.runs: dict[str, ResearchRun] = {}

    async def handle_turn(self, text: str, *, text_input: bool = False) -> None:
        decision = self.state.attention.evaluate(text, text_input=text_input)
        scheduler = self.state.scheduler
        if scheduler is not None:
            scheduler.resolve_user_speech(addressed=decision.addressed)

        dbg(
            "TURN_ATTENTION_DECISION",
            text=text,
            cleaned_text=decision.text,
            accepted=decision.accepted,
            action=decision.action,
            wake_detected=decision.wake_detected,
        )
        if not decision.accepted:
            return

        self.state.delivery.advance_turn()

        if decision.action == "wake":
            if scheduler is not None:
                scheduler.enqueue_notice("はい、どうぞ。")
            return
        if decision.action == "close":
            if scheduler is not None:
                scheduler.enqueue_notice("わかりました。")
            return
        if decision.action == "cancel":
            await self.cancel_all()
            if scheduler is not None:
                scheduler.enqueue_notice("調査を止めました。")
            return
        if decision.action == "continue":
            chunk = self.state.delivery.resume_interrupted()
            if scheduler is not None:
                if chunk is None:
                    scheduler.enqueue_notice("途中の回答はありません。")
                else:
                    scheduler.notify()
            return
        if decision.action == "repeat":
            chunk = self.state.delivery.replay_last()
            if scheduler is not None:
                if chunk is None:
                    scheduler.enqueue_notice("繰り返せる回答はまだありません。")
                else:
                    scheduler.notify()
            return

        await self.start_research(decision.text)

    async def start_research(self, question: str) -> None:
        local_id = f"local:{time.monotonic_ns()}"
        placeholder = ResearchRun(local_id=local_id, question=question)
        task = asyncio.create_task(self._run(placeholder))
        placeholder.task = task
        self.runs[local_id] = placeholder
        self.state.attention.research_started()
        dbg("RESEARCH_STARTED", question=question, local_id=local_id, url=stream_url())

    async def _run(self, run: ResearchRun) -> None:
        async def on_event(event: dict[str, Any]) -> None:
            await mirror(self.state.room, event)
            event_type = event.get("type")
            if event_type == "run" and event.get("run_id"):
                run.run_id = str(event["run_id"])
                dbg("RESEARCH_RUN_BOUND", local_id=run.local_id, run_id=run.run_id)
                return

            scheduler = self.state.scheduler
            if event_type == "plan":
                run.plan_version = max(run.plan_version, int(event.get("version") or 0))
                acknowledgement = plan_ack(event)
                if acknowledgement and scheduler is not None:
                    scheduler.enqueue_notice(acknowledgement)
            elif event_type == "plan_update":
                version = int(event.get("version") or 0)
                if version <= run.plan_version:
                    return
                run.plan_version = version
                if event.get("reason") == "insufficient_evidence" and scheduler is not None:
                    scheduler.enqueue_notice("もう少し裏付けが要るので、追加で見ます。")
            elif event_type == "level_start" and scheduler is not None:
                scheduler.level_started(str(event.get("level_id") or ""))
            elif event_type == "level":
                run_id = run.run_id or run.local_id
                chunk = self.state.delivery.enqueue_event(run_id, event)
                if scheduler is not None:
                    scheduler.level_completed(
                        str(event.get("level_id") or ""),
                        int(event.get("latency_ms") or 0),
                    )
                    if chunk is not None:
                        scheduler.notify()
            elif event_type == "error" and scheduler is not None:
                scheduler.enqueue_notice("調査中に問題が起きました。画面には取得済みの内容を残しています。")

        try:
            returned_run_id = await stream_answer(run.question, on_event)
            if run.run_id is None:
                run.run_id = returned_run_id
        except asyncio.CancelledError:
            dbg("RESEARCH_TASK_CANCELLED", local_id=run.local_id, run_id=run.run_id)
            raise
        except Exception as exc:
            dbg(
                "RESEARCH_STREAM_FAILED",
                local_id=run.local_id,
                run_id=run.run_id,
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
            error_event = {"type": "error", "message": str(exc), "run_id": run.run_id}
            try:
                await mirror(self.state.room, error_event)
            except Exception:
                pass
            if self.state.scheduler is not None:
                self.state.scheduler.enqueue_notice(
                    "調査との接続が切れました。届いている回答はそのまま確認できます。"
                )
        finally:
            self.runs.pop(run.local_id, None)
            self.state.attention.research_finished()
            dbg("RESEARCH_FINISHED", local_id=run.local_id, run_id=run.run_id)

    async def cancel_all(self) -> None:
        runs = list(self.runs.values())
        for run in runs:
            if run.run_id:
                try:
                    await cancel_run(run.run_id)
                except Exception as exc:
                    dbg("RESEARCH_CANCEL_REQUEST_FAILED", run_id=run.run_id, error=repr(exc))
            if run.task is not None:
                run.task.cancel()
        tasks = [run.task for run in runs if run.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@function_tool
async def read_result(ctx: RunContext[WikiVoiceState], handle: str) -> str:
    """Read a retained research result by its objective or level identifier."""
    result = ctx.userdata.delivery.read_result(handle)
    return result or "その調査結果は見つかりませんでした。"


class JapaneseWikiAssistant(Agent):
    def __init__(self, state: WikiVoiceState) -> None:
        self._state = state
        super().__init__(
            instructions=(
                "あなたは日本語で話す社内Wikiアシスタントです。"
                "音声では短く自然な敬語を使い、識別子、パス、コード、Markdownを読み上げません。"
                "詳しい結果は画面に出ています。保持済みの結果が必要なときだけ read_result を使います。"
            ),
            tools=[read_result],
        )

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        text = getattr(new_message, "text_content", "")
        if callable(text):
            text = text()
        text = str(text or "").strip()
        if self._state.delivery.chunks:
            turn_ctx.add_message(role="assistant", content=self._state.delivery.state_block())
        if text and self._state.coordinator is not None:
            await self._state.coordinator.handle_turn(text)
        # Research speech is driven by SSE levels, never by a second LLM pass.
        raise StopResponse()


async def handle_text_turn(state: WikiVoiceState, text: str) -> None:
    if state.coordinator is not None:
        await state.coordinator.handle_turn(text, text_input=True)


def prewarm(proc: agents.JobProcess) -> None:
    dbg("PREWARM_START", "Loading Silero VAD and multilingual turn detector.")
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["turn_detector"] = MultilingualModel()
    dbg("PREWARM_DONE", "Audio models loaded.")


def japanese_sentence_tokenizer() -> Any:
    # Recent LiveKit versions recognize Japanese 。！？ in the basic splitter.
    # A small min length flushes each complete Japanese clause to TTS instead
    # of buffering the whole answer.
    try:
        return tokenize.basic.SentenceTokenizer(
            language="japanese",
            min_sentence_len=8,
            stream_context_len=1,
        )
    except TypeError:
        return tokenize.basic.SentenceTokenizer(min_sentence_len=8)


async def entrypoint(ctx: agents.JobContext) -> None:
    await ctx.connect()
    dbg(
        "ENTRYPOINT_CONNECTED",
        room=ctx.room.name,
        LIVEKIT_URL=os.getenv("LIVEKIT_URL"),
        LLM_MODEL=os.getenv("LLM_MODEL"),
        STT_MODEL=os.getenv("STT_MODEL"),
        TTS_MODEL=os.getenv("TTS_MODEL"),
        RAG_STREAM_URL=stream_url(),
    )

    vad = ctx.proc.userdata["vad"]
    turn_detector = ctx.proc.userdata["turn_detector"]

    # This endpoint is backed by a realtime STT model. Passing it directly
    # exposes interim transcripts instead of forcing utterance-level batching.
    stt = openai.STT(
        model=os.getenv("STT_MODEL", "mistralai/Voxtral-Mini-4B-Realtime-2602"),
        base_url=os.getenv("STT_BASE_URL", "http://127.0.0.1:8001/v1"),
        api_key=os.getenv("STT_API_KEY", "EMPTY"),
    )
    llm = openai.LLM(
        model=os.getenv("LLM_MODEL", "gemma-4-31B"),
        base_url=os.getenv("LLM_BASE_URL", "http://10.160.144.101:51029/v1"),
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
    )
    base_tts = openai.TTS(
        model=os.getenv("TTS_MODEL", "qwen3-tts-1.7b"),
        voice=os.getenv("TTS_VOICE", "Ono_Anna"),
        base_url=os.getenv("TTS_BASE_URL", "http://127.0.0.1:8002/v1"),
        api_key=os.getenv("TTS_API_KEY", "EMPTY"),
        response_format="pcm",
    )
    tts = lk_tts.StreamAdapter(
        tts=base_tts,
        sentence_tokenizer=japanese_sentence_tokenizer(),
    )

    state = WikiVoiceState(room=ctx.room)
    session = AgentSession[WikiVoiceState](
        userdata=state,
        vad=vad,
        stt=stt,
        llm=llm,
        tts=tts,
        turn_detection=turn_detector,
        preemptive_generation=True,
        min_endpointing_delay=0.7,
    )
    state.session = session
    state.scheduler = SpeechScheduler(
        session,
        state.delivery,
        chars_per_second=float(os.getenv("TTS_JA_CHARS_PER_SECOND", "6")),
        duck_callback=lambda ducked: publish_duck(ctx.room, ducked),
    )
    state.coordinator = ResearchCoordinator(state)

    @session.on("user_started_speaking")
    def _on_user_started_speaking(*_args: Any, **_kwargs: Any) -> None:
        dbg("EVENT_USER_STARTED_SPEAKING")
        if state.scheduler is not None:
            state.scheduler.user_started_speaking()

    @session.on("user_speech_committed")
    def _on_user_speech_committed(*_args: Any, **_kwargs: Any) -> None:
        dbg("EVENT_USER_SPEECH_COMMITTED")

    def _on_text_input(_session: AgentSession, event: room_io.TextInputEvent) -> None:
        text = event.text.strip()
        if text:
            asyncio.create_task(handle_text_turn(state, text))

    assistant = JapaneseWikiAssistant(state)
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
    state.scheduler.start()
    state.scheduler.enqueue_notice(
        "こんにちは。社内Wikiを検索できます。音声では、モーヴィと呼んでから質問してください。"
    )

    async def shutdown() -> None:
        if state.coordinator is not None:
            await state.coordinator.cancel_all()
        if state.scheduler is not None:
            await state.scheduler.close()

    ctx.add_shutdown_callback(shutdown)
    dbg("SESSION_STARTED", room=ctx.room.name)


def build_web_server() -> uvicorn.Server:
    """Build the existing FastAPI server without taking over the main thread."""
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
        server_options.update(
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
        )
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

    deadline = time.monotonic() + float(
        os.getenv("TEXT_TEST_START_TIMEOUT_SECONDS", "10")
    )
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
        f"{os.getenv('TEXT_TEST_HOST', '0.0.0.0')}:"
        f"{os.getenv('TEXT_TEST_PORT', '51027')}",
        flush=True,
    )
    return web_server, thread


def run_combined_server() -> None:
    web_server, web_thread = start_web_server()
    try:
        agents.cli.run_app(
            agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm)
        )
    finally:
        web_server.should_exit = True
        web_thread.join(timeout=5.0)


if __name__ == "__main__":
    run_combined_server()
