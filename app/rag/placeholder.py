"""Placeholder llm-wiki backend, for live ASR/LLM testing without the real RAG service.

app/rag/llm_wiki.py (the only file that talks to the real backend) expects an
SSE stream of ``run -> plan -> level_start -> level -> done`` frames from
``POST {LLM_WIKI_BASE_URL}/{LLM_WIKI_PREFIX}/{LLM_WIKI_DATABASE}/api/ask/realtime/stream``,
plus a ``POST .../api/agent-runs/{run_id}/stop`` to cancel one. This serves
just that shape with a fixed placeholder answer, so a `research_wiki` tool
call completes instead of erroring out - useful for exercising the full
ASR -> LLM -> report -> TTS loop while the real wiki backend isn't reachable.
Not a stand-in for wiki content: the answer text is always the same dummy
string, never a real lookup.

Run it with ``python -m app.rag.placeholder``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

load_dotenv(override=True)

app = FastAPI()

PLACEHOLDER_TEXT = (
    "これはプレースホルダーの検索結果です。実際の社内Wikiバックエンドには接続していません。"
    "これは音声認識と対話モデルの動作を確認するためのダミー回答です。"
)


def sse(event_type: str, **fields: Any) -> str:
    payload = {"type": event_type, **fields}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/{_prefix:path}/api/ask/realtime/stream")
async def ask_stream(_prefix: str, request: Request) -> StreamingResponse:
    body = await request.json()
    question = str(body.get("question") or "")
    run_id = f"placeholder-{time.monotonic_ns():x}"

    async def events():
        yield sse("run", run_id=run_id)
        await asyncio.sleep(0.2)
        level_plan = {"position": 0, "objective": "プレースホルダー確認"}
        yield sse("plan", version=1, levels=[level_plan])
        await asyncio.sleep(0.2)
        yield sse("level_start")
        await asyncio.sleep(0.5)
        yield sse(
            "level",
            level_id="placeholder-0",
            position=0,
            objective=level_plan["objective"],
            text=f"{PLACEHOLDER_TEXT}\n\n[質問] {question}",
        )
        await asyncio.sleep(0.1)
        yield sse("done", status="complete")

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/{_prefix:path}/api/agent-runs/{run_id}/stop")
async def stop_run(_prefix: str, run_id: str) -> dict:
    return {"status": "stopped", "run_id": run_id}


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("LLM_WIKI_PLACEHOLDER_HOST", "0.0.0.0"),
        port=int(os.getenv("LLM_WIKI_PLACEHOLDER_PORT", "8005")),
        log_level=os.getenv("LLM_WIKI_PLACEHOLDER_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
