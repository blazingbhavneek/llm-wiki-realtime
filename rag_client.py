"""Streaming client for the llm-wiki realtime RAG endpoint."""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx


VOICE_KNOBS: dict[str, int] = {
    "max_levels": 3,
    "max_queries_per_level": 4,
    "max_recovery_levels": 1,
    "search_limit": 8,
    "max_context_chars": 8000,
    "min_search_results": 3,
}

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _rag_base() -> str:
    return os.getenv("LLM_WIKI_BASE_URL", "http://10.160.152.38:8000").rstrip("/")


def _rag_database() -> str:
    return os.getenv("LLM_WIKI_DATABASE", "moove_wiki").strip("/")


def stream_url() -> str:
    override = os.getenv("LLM_WIKI_REALTIME_URL")
    if override:
        return override
    prefix = os.getenv("LLM_WIKI_PREFIX", "/llm-wiki").strip("/")
    return f"{_rag_base()}/{prefix}/{_rag_database()}/api/ask/realtime/stream"


def cancel_url(run_id: str) -> str:
    prefix = os.getenv("LLM_WIKI_PREFIX", "/llm-wiki").strip("/")
    return f"{_rag_base()}/{prefix}/{_rag_database()}/api/agent-runs/{run_id}/stop"


def _decode_frame(event_name: str | None, data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    if not isinstance(payload, dict):
        raise ValueError("SSE data must decode to a JSON object")
    if event_name and "type" not in payload:
        payload["type"] = event_name
    return payload


async def iter_sse_events(lines: AsyncIterable[str]):
    """Parse an SSE line stream, ignoring comments and flushing at EOF."""
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line.startswith(":"):
            continue
        if not line:
            event = _decode_frame(event_name, data_lines)
            if event is not None:
                yield event
            event_name = None
            data_lines = []
            continue

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    event = _decode_frame(event_name, data_lines)
    if event is not None:
        yield event


async def _call(callback: EventCallback, event: dict[str, Any]) -> None:
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def _stream_with_client(
    client: "httpx.AsyncClient",
    question: str,
    on_event: EventCallback,
    knobs: Mapping[str, int],
) -> str | None:
    run_id: str | None = None
    async with client.stream(
        "POST",
        stream_url(),
        json={"question": question, **dict(knobs)},
        headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
    ) as response:
        response.raise_for_status()
        async for event in iter_sse_events(response.aiter_lines()):
            if event.get("type") == "run" and event.get("run_id"):
                run_id = str(event["run_id"])
            await _call(on_event, event)
    return run_id


async def stream_answer(
    question: str,
    on_event: EventCallback,
    *,
    knobs: Mapping[str, int] | None = None,
    client: "httpx.AsyncClient | None" = None,
) -> str | None:
    """Consume one POST SSE stream and invoke ``on_event`` for every frame."""
    settings = VOICE_KNOBS if knobs is None else knobs
    if client is not None:
        return await _stream_with_client(client, question, on_event, settings)

    # Deliberately no read timeout: long research gaps are kept alive by SSE
    # comments, and a normal httpx read timeout would kill valid runs.
    import httpx

    timeout = httpx.Timeout(None, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await _stream_with_client(owned_client, question, on_event, settings)


async def cancel_run(run_id: str, *, client: "httpx.AsyncClient | None" = None) -> None:
    async def stop(active_client: "httpx.AsyncClient") -> None:
        response = await active_client.post(cancel_url(run_id))
        response.raise_for_status()

    if client is not None:
        await stop(client)
        return
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as owned_client:
        await stop(owned_client)


async def mirror(room: Any, event: dict[str, Any]) -> None:
    """Publish the original RAG event to the browser without reshaping it."""
    if room is None:
        return
    await room.local_participant.publish_data(
        json.dumps(event, ensure_ascii=False).encode("utf-8"),
        reliable=True,
        topic="research",
    )
