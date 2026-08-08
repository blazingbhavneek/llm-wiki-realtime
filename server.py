"""LiveKit voice agent backed by llm-wiki's realtime research stream."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
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
    public_livekit_url = os.getenv(
        "PUBLIC_LIVEKIT_URL",
        "wss://10.160.152.38:7880",
    )
    livekit_api_url = os.getenv(
        "LIVEKIT_URL",
        "http://10.160.152.38:7880",
    )
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "japanese-wiki-agent")
    manual_dispatch = env_bool("LIVEKIT_MANUAL_DISPATCH", True)

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
    plan_levels: list[dict[str, Any]] = field(default_factory=list)
    plan_spoken: bool = False
    speech_tasks: set[asyncio.Task] = field(default_factory=set)


@dataclass
class WikiVoiceState:
    session: Optional[AgentSession] = None
    room: Any = None
    delivery: DeliveryQueue = field(default_factory=DeliveryQueue)
    attention: AttentionFSM = field(default_factory=AttentionFSM)
    scheduler: Optional[SpeechScheduler] = None
    coordinator: Optional["ResearchCoordinator"] = None
    last_research_question: str = ""


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
        # A stream can deliver levels quickly.  Serialize their voice turns so
        # level 2 cannot overtake the short explanation for level 1.
        self._speech_filter_lock = asyncio.Lock()
        self._speech_tasks: set[asyncio.Task] = set()

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
        self.state.last_research_question = question
        self.state.attention.research_started()
        dbg("RESEARCH_STARTED", question=question, local_id=local_id, url=stream_url())

    async def _speak_research_level(self, run: ResearchRun, chunk: Any) -> None:
        """Convert one completed RAG level from internal state to speech."""
        session = self.state.session
        if session is None:
            return
        source = chunk.text.strip()
        if not source:
            return
        async with self._speech_filter_lock:
            ordered_levels = sorted(
                run.plan_levels,
                key=lambda level: int(level.get("position") or 0),
            )
            current_index = next(
                (
                    index
                    for index, level in enumerate(ordered_levels)
                    if str(level.get("id") or level.get("level_id") or "") == chunk.level_id
                ),
                max(chunk.position - 1, 0),
            )
            level_count = max(len(ordered_levels), current_index + 1)
            next_objective = ""
            if current_index + 1 < len(ordered_levels):
                next_objective = str(
                    ordered_levels[current_index + 1].get("objective") or ""
                )
            dbg(
                "RAG_LEVEL_SPEECH_FILTER_STARTED",
                local_id=run.local_id,
                level_id=chunk.level_id,
                position=chunk.position,
            )
            await session.generate_reply(
                instructions=(
                    "以下は社内Wiki調査の一段階の内部結果です。ユーザーには、この段階で確定した"
                    "質問への答えだけを、自然な日本語で二から四文、二百四十文字程度までで話してください。"
                    "最初にユーザーの質問で直接求められた結論を述べ、その名前だけで終わらせず、"
                    "その値・引数・用語が何を指定するのかを平易な日本語で一段深く説明してください。"
                    "根拠に範囲、単位、入出力の向き、条件、構成要素がある場合は、質問に役立つものだけを補足してください。"
                    "根拠に単純な足し算や引き算などで得られる値がある場合は、計算過程を短く示して結論の数値まで言ってください。"
                    "関連する別関数、周辺機能、実装例、カテゴリ一覧、背景説明、検索で見つけた周辺情報は、"
                    "質問への直接回答に不可欠でない限り絶対に話しません。"
                    "これは内部状態なので、内容を列挙・引用・復唱したり、画面を見るよう案内したり、"
                    "調査の計画・進捗・エラー・検索語を話したりしないでください。"
                    "Markdown、箇条書き、URL、パス、コード、ID、表、括弧を出力しないでください。"
                    "ファイル名、定義名、API名、関数名、マクロ名など、質問への直接回答となる"
                    "重要な固有名詞は絶対に省略せず、一般名詞や役割の説明だけで置き換えないでください。"
                    "重要な固有名詞は自然なカタカナ読みにした名称を先に言い、その後に役割を説明してください。"
                    "複数の名称が答えである場合は、文字数の目安より名称の保持を優先し、すべて言ってください。"
                    "英数字の名称は構成要素を落とさずカタカナの自然な読みに変換し、"
                    "アンダースコア、ハイフン、ピリオド自体は読みません。"
                    "意味が明確な略語は自然な語にします。たとえば mpf_mfs_open は"
                    "『エムピーエフ・エムエフエス・オープン』、mpf_buf は"
                    "『エムピーエフ・バッファ』、pmf_prg.txt は"
                    "『ピーエムエフ・プログラム・テキスト・ファイル』のように扱います。"
                    "ファイルの拡張子も省略せず、txt は『テキスト・ファイル』のように読みます。"
                    "名称を示した後は、技術的な生データの羅列を避け、何をするものかを説明してください。"
                    "これは単独の回答ではなく、段階的な説明の途中です。前の段階の内容を繰り返さず、"
                    "この段階で新たに分かったことだけを話してください。"
                    "次の観点が示されている場合は、内容を言い終えた後に一度だけ自然に"
                    "『続けて、引数の意味を確認します』のように次の話題へつないでください。"
                    "最後の段階では次の調査を予告せず、質問への答えを短く締めてください。"
                    "未確定の内容は断定せず、追加の調査やツール呼び出しはしないでください。\n\n"
                    f"[ユーザーの質問]\n{run.question}\n\n"
                    f"[説明の段階]\n{current_index + 1} / {level_count}\n\n"
                    f"[この段階の観点]\n{chunk.objective}\n\n"
                    f"[次の観点]\n{next_objective or 'なし。ここで説明を締める。'}\n\n"
                    f"[内部結果]\n{source[:5000]}"
                ),
                tools=[],
                allow_interruptions=True,
            )
            dbg(
                "RAG_LEVEL_SPEECH_FILTER_FINISHED",
                local_id=run.local_id,
                level_id=chunk.level_id,
            )

    async def _speak_research_plan(self, run: ResearchRun) -> None:
        """Turn the SSE plan into one conversational opening, not a query list."""
        session = self.state.session
        if session is None or not run.plan_levels:
            return
        outline = "\n".join(
            f"{index + 1}. {level.get('objective') or '必要な情報'}"
            for index, level in enumerate(
                sorted(run.plan_levels, key=lambda level: int(level.get("position") or 0))
            )
        )
        async with self._speech_filter_lock:
            dbg("RAG_PLAN_SPEECH_FILTER_STARTED", local_id=run.local_id)
            await session.generate_reply(
                instructions=(
                    "以下は社内Wiki調査の内部的な段階計画です。ユーザーに、これから何を確認し、"
                    "どの順で説明するかを自然な日本語一文で予告してください。"
                    "検索語、件数、段階番号、箇条書き、計画という言葉、Markdown、英字、ローマ字、"
                    "アンダースコア、ハイフンは出力しません。内部の観点をそのまま列挙せず、"
                    "例えば『まず関数の役割を確認し、そのあと引数の意味を見ます』のように"
                    "会話として言い換えてください。まだ事実の回答や推測はしないでください。"
                    "追加の調査やツール呼び出しはしないでください。\n\n"
                    f"[ユーザーの質問]\n{run.question}\n\n"
                    f"[内部の段階計画]\n{outline}"
                ),
                tools=[],
                allow_interruptions=True,
            )
            dbg("RAG_PLAN_SPEECH_FILTER_FINISHED", local_id=run.local_id)

    def _queue_level_speech(self, run: ResearchRun, chunk: Any) -> None:
        task = asyncio.create_task(self._speak_research_level(run, chunk))
        run.speech_tasks.add(task)
        self._speech_tasks.add(task)

        def _finished(completed: asyncio.Task) -> None:
            run.speech_tasks.discard(completed)
            self._speech_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception as exc:
                dbg(
                    "RAG_LEVEL_SPEECH_FILTER_FAILED",
                    local_id=run.local_id,
                    level_id=chunk.level_id,
                    error=repr(exc),
                )

        task.add_done_callback(_finished)
        dbg(
            "RAG_LEVEL_SPEECH_FILTER_QUEUED",
            local_id=run.local_id,
            level_id=chunk.level_id,
            position=chunk.position,
        )

    def _queue_plan_speech(self, run: ResearchRun) -> None:
        task = asyncio.create_task(self._speak_research_plan(run))
        run.speech_tasks.add(task)
        self._speech_tasks.add(task)

        def _finished(completed: asyncio.Task) -> None:
            run.speech_tasks.discard(completed)
            self._speech_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception as exc:
                dbg("RAG_PLAN_SPEECH_FILTER_FAILED", local_id=run.local_id, error=repr(exc))

        task.add_done_callback(_finished)
        dbg("RAG_PLAN_SPEECH_FILTER_QUEUED", local_id=run.local_id)

    async def _run(self, run: ResearchRun) -> None:
        try:
            max_retries = int(os.getenv("RAG_STREAM_MAX_RETRIES", "1"))
            retry_count = 0
            while True:
                should_retry = await self._stream_attempt(run)
                if not should_retry:
                    break
                if retry_count >= max_retries:
                    raise TimeoutError("realtime research stream did not deliver its planned result")
                retry_count += 1
                dbg(
                    "RESEARCH_STREAM_RETRYING",
                    local_id=run.local_id,
                    retry_count=retry_count,
                    question=run.question,
                )
                # The next stream emits a fresh run and plan. Keep plan_spoken
                # true so a retry never makes the assistant repeat its opener.
                run.run_id = None
                run.plan_version = 0
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

    async def _stream_attempt(self, run: ResearchRun) -> bool:
        """Run one SSE attempt; return True only when its watchdog requests retry."""
        progress: asyncio.Queue[str] = asyncio.Queue()
        plan_received = False
        completed_levels: set[str] = set()
        retry_requested = False
        retry_reason = ""
        stream_task: asyncio.Task | None = None

        async def watchdog() -> None:
            nonlocal retry_requested, retry_reason
            while True:
                timeout = float(
                    os.getenv(
                        "RAG_INITIAL_PLAN_TIMEOUT_SECONDS"
                        if not plan_received
                        else "RAG_LEVEL_TIMEOUT_SECONDS",
                        "5" if not plan_received else "15",
                    )
                )
                try:
                    await asyncio.wait_for(progress.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    retry_requested = True
                    retry_reason = "initial_plan" if not plan_received else "level_gap"
                    dbg(
                        "RESEARCH_STREAM_TIMEOUT",
                        local_id=run.local_id,
                        run_id=run.run_id,
                        reason=retry_reason,
                        timeout_seconds=timeout,
                    )
                    if run.run_id:
                        try:
                            await cancel_run(run.run_id)
                            dbg("RESEARCH_STREAM_STOP_SENT", run_id=run.run_id)
                        except Exception as exc:
                            dbg("RESEARCH_STREAM_STOP_FAILED", run_id=run.run_id, error=repr(exc))
                    if stream_task is not None:
                        stream_task.cancel()
                    return

                expected_levels = len(run.plan_levels)
                if plan_received and expected_levels and len(completed_levels) >= expected_levels:
                    return

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal plan_received
            await mirror(self.state.room, event)
            event_type = event.get("type")
            if event_type == "run" and event.get("run_id"):
                run.run_id = str(event["run_id"])
                dbg("RESEARCH_RUN_BOUND", local_id=run.local_id, run_id=run.run_id)
                return

            scheduler = self.state.scheduler
            if event_type == "plan":
                run.plan_version = max(run.plan_version, int(event.get("version") or 0))
                run.plan_levels = list(event.get("levels") or [])
                plan_received = True
                progress.put_nowait("plan")
                if run.plan_levels and not run.plan_spoken:
                    run.plan_spoken = True
                    self._queue_plan_speech(run)
            elif event_type == "plan_update":
                version = int(event.get("version") or 0)
                if version <= run.plan_version:
                    return
                run.plan_version = version
                run.plan_levels = list(event.get("levels") or run.plan_levels)
            elif event_type == "level_start" and scheduler is not None:
                scheduler.level_started(str(event.get("level_id") or ""))
            elif event_type == "level":
                level_id = str(event.get("level_id") or "")
                completed_levels.add(level_id)
                progress.put_nowait("level")
                run_id = run.run_id or run.local_id
                chunk = self.state.delivery.enqueue_event(run_id, event)
                if scheduler is not None:
                    scheduler.level_completed(level_id, int(event.get("latency_ms") or 0))
                if chunk is not None:
                    # Keep the complete raw result for the UI/read_result, but
                    # never let the queue speak it verbatim.
                    chunk.state = "stale"
                    if chunk.complete:
                        self._queue_level_speech(run, chunk)
            elif event_type == "error" and scheduler is not None:
                scheduler.enqueue_notice("調査中に問題が起きました。画面には取得済みの内容を残しています。")

        stream_task = asyncio.create_task(stream_answer(run.question, on_event))
        watchdog_task = asyncio.create_task(watchdog())
        try:
            returned_run_id = await stream_task
            if run.run_id is None:
                run.run_id = returned_run_id
            return retry_requested
        except asyncio.CancelledError:
            if retry_requested:
                dbg(
                    "RESEARCH_STREAM_ATTEMPT_STOPPED",
                    local_id=run.local_id,
                    reason=retry_reason,
                )
                return True
            raise
        finally:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)

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
        for speech_task in tuple(self._speech_tasks):
            speech_task.cancel()
        tasks = [run.task for run in runs if run.task is not None]
        tasks.extend(self._speech_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@function_tool
async def read_result(ctx: RunContext[WikiVoiceState], handle: str) -> str:
    """Read retained Wiki evidence before answering a related follow-up question."""
    result = ctx.userdata.delivery.read_result(handle)
    return result or "その調査結果は見つかりませんでした。"


@function_tool
async def research_wiki(ctx: RunContext[WikiVoiceState], question: str) -> str:
    """Search the internal Wiki for a question that needs company-specific facts."""
    coordinator = ctx.userdata.coordinator
    if coordinator is None:
        return "調査機能はまだ準備できていません。"
    await coordinator.start_research(question)
    # Tool-level stop is deliberate: a prompt alone cannot reliably prevent a
    # model from voicing a speculative "I'll look that up" response.
    raise StopResponse()


class JapaneseWikiAssistant(Agent):
    def __init__(self, state: WikiVoiceState) -> None:
        self._state = state
        super().__init__(
            instructions=(
                "あなたは日本語で話す社内Wikiアシスタントです。"
                "出力は日本語だけにしてください。英字、英単語、ローマ字、ASCIIの記号、Markdown、"
                "箇条書き記号、コードフェンス、URL、パス、表、引用記法は一切出力しません。"
                "技術識別子を言う必要がある場合も、アンダースコアとハイフンを消し、必ず自然なカタカナ読みにします。"
                "ファイル名、定義名、API名、関数名、マクロ名など、質問への直接回答となる重要な固有名詞は"
                "説明だけに置き換えず、構成要素と拡張子を省略しないカタカナ読みで必ず言ってください。"
                "音声では短く自然な敬語を使い、識別子、パス、コードをそのまま読み上げません。"
                "挨拶、雑談、一般的な質問には自然に会話として答えてください。"
                "社内Wikiの確認、社内固有の事実、最新情報の調査が必要な質問だけ research_wiki を使ってください。"
                "関数、マクロ、API、構造体、引数、戻り値、エラーコードの質問は、推測や一般的な例で答えず、"
                "必ず最初に research_wiki を使ってください。"
                "ただし、直前のWiki回答についての詳細、理由、具体例、別の引数、制約、言い換え、"
                "『もっと詳しく』のような追質問は新規調査ではありません。"
                "この場合は、まず会話の直前の回答と保持済みの調査状態から該当する handle を選び、"
                "必ず read_result を使って根拠を読み、そこにある情報だけで必要な範囲を詳しく説明してください。"
                "read_result を使った追質問では、『確認します』『調べます』『少々お待ちください』のような"
                "前置きだけを言って終わらせず、同じ応答で結論から答えてください。"
                "read_result の内容で答えられない新しい対象・新しい事実を尋ねられた場合だけ research_wiki を使ってください。"
                "research_wiki を呼んだ直後は、調査開始、計画、途中経過、画面の案内、推測した回答を音声で言わず、"
                "その応答を終了してください。"
                "調査レベルが完了したときは別の指示が自然な音声回答を生成します。"
                "保持済みの結果が必要なときだけ read_result を使います。"
            ),
            tools=[read_result, research_wiki],
        )

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        text = getattr(new_message, "text_content", "")
        if callable(text):
            text = text()
        text = str(text or "").strip()
        if self._state.last_research_question:
            turn_ctx.add_message(
                role="assistant",
                content=(
                    "[直前のWiki調査]\n"
                    f"質問: {self._state.last_research_question}\n"
                    "関連する追質問では read_result を先に使い、新規調査を始めない。"
                ),
            )
        if self._state.delivery.chunks:
            turn_ctx.add_message(role="assistant", content=self._state.delivery.state_block())


async def handle_text_turn(state: WikiVoiceState, text: str) -> None:
    if not text or state.session is None:
        return
    if state.scheduler is not None:
        state.scheduler.resolve_user_speech(addressed=True)
    await state.session.generate_reply(user_input=text, input_modality="text")


def prewarm(proc: agents.JobProcess) -> None:
    # VAD can be shared through process userdata. MultilingualModel cannot:
    # recent LiveKit versions require an active JobContext for its inference
    # executor, which does not exist while prewarming.
    dbg("PREWARM_START", "Loading Silero VAD.")
    proc.userdata["vad"] = silero.VAD.load()
    dbg("PREWARM_DONE", "VAD loaded.")


def worker_load() -> float:
    """Reserve one agent job without using the shared host's aggregate CPU load."""
    return 0.0


def tts_sentence_tokenizer() -> Any:
    """Buffer a complete Japanese response before each non-streaming TTS call."""
    return tokenize.basic.SentenceTokenizer(
        language="japanese",
        # Qwen is served through OpenAI's non-streaming TTS endpoint.  A short
        # threshold turns one reply into many independent synthesis requests,
        # which makes language/prosody reset mid-answer.  This deliberately
        # favors a coherent utterance over first-word latency.
        min_sentence_len=int(os.getenv("TTS_MIN_SENTENCE_CHARS", "180")),
        stream_context_len=int(os.getenv("TTS_STREAM_CONTEXT_CHARS", "240")),
    )


async def entrypoint(ctx: agents.JobContext) -> None:
    await ctx.connect()
    dbg(
        "ENTRYPOINT_CONNECTED",
        room=ctx.room.name,
        LIVEKIT_URL=os.getenv("LIVEKIT_URL"),
        LLM_MODEL=os.getenv("LLM_MODEL"),
        STT_MODEL=os.getenv("STT_MODEL"),
        STT_BASE_URL=os.getenv("STT_BASE_URL"),
        TTS_MODEL=os.getenv("TTS_MODEL"),
        TTS_BASE_URL=os.getenv("TTS_BASE_URL"),
        RAG_STREAM_URL=stream_url(),
    )

    vad = ctx.proc.userdata["vad"]
    turn_detector = MultilingualModel()

    # This endpoint is backed by a realtime STT model. Passing it directly
    # exposes interim transcripts instead of forcing utterance-level batching.
    stt = openai.STT(
        model=os.getenv("STT_MODEL", "nvidia/nemotron-3.5-asr-streaming-0.6b"),
        base_url=os.getenv("STT_BASE_URL", "http://10.160.144.101:51026/v1"),
        api_key=os.getenv("STT_API_KEY", "EMPTY"),
    )
    llm = openai.LLM(
        model=os.getenv("LLM_MODEL", "gemma-4-31B"),
        base_url=os.getenv("LLM_BASE_URL", "http://10.160.144.101:51029/v1"),
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
    )
    base_tts = openai.TTS(
        model=os.getenv("TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
        voice=os.getenv("TTS_VOICE", "Ono_Anna"),
        base_url=os.getenv("TTS_BASE_URL", "http://10.160.144.101:51027/v1"),
        api_key=os.getenv("TTS_API_KEY", "EMPTY"),
        response_format="pcm",
        # The OpenAI-compatible adapter forwards this as the request's
        # `instructions` field.  Qwen servers that implement it can disable
        # automatic language selection for otherwise Japanese text.
        instructions=os.getenv(
            "TTS_INSTRUCTIONS",
            "日本語のみで発話してください。英語、ローマ字、他の言語へ切り替えないでください。",
        ),
    )
    tts = lk_tts.StreamAdapter(
        tts=base_tts,
        sentence_tokenizer=tts_sentence_tokenizer(),
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
    greeting = "こんにちは。社内Wikiを検索できます。音声またはテキストで質問してください。"
    await session.generate_reply(
        instructions=(
            "次の一文だけを、そのまま日本語で発話してください。言い換え、追加、英字、"
            "ローマ字、Markdown、記号を加えてはいけません。\n\n"
            f"発話文: {greeting}\n"
            f"音声合成の指示: {os.getenv('TTS_INSTRUCTIONS', '')}"
        ),
        tools=[],
        allow_interruptions=True,
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
                # The worker's WebSocket must reach the local LiveKit server
                # directly. WorkerOptions otherwise unconditionally adopts
                # HTTPS_PROXY/HTTP_PROXY, ignoring NO_PROXY.
                http_proxy=None,
            )
        )
    finally:
        web_server.should_exit = True
        web_thread.join(timeout=5.0)


if __name__ == "__main__":
    run_combined_server()
