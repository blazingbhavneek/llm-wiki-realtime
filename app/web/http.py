"""The FastAPI app: the built frontend, ``/health`` and the token endpoint.

``app`` is a module-level name on purpose - ``app.runtime.worker`` hands this
object straight to ``uvicorn.Config``. The environment is loaded by
``app.runtime.worker`` before it imports this module, so the routes below see
the ``.env`` values.
"""

from __future__ import annotations

import hmac
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.web.tokens import create_room_token

app = FastAPI()

# Two levels up from app/web/http.py is the repository root, which is where the
# frontend build lands.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIST / "index.html")


def _authenticate_token_request(request: Request) -> None:
    expected = Settings.from_env().web.access_token
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
    session must receive a fresh dispatch.
    """
    _authenticate_token_request(request)
    room = f"japanese-assistant-{uuid.uuid4().hex[:8]}"
    identity = f"user-{uuid.uuid4().hex[:8]}"

    try:
        # Read per request, as this route always has: the web server is started
        # once but the LiveKit credentials are deployment configuration.
        return await create_room_token(
            room=room,
            identity=identity,
            settings=Settings.from_env().livekit,
        )
    except Exception as exc:
        print("TOKEN_ENDPOINT_FAILED:", repr(exc), flush=True)
        print(traceback.format_exc(), flush=True)
        raise HTTPException(
            status_code=500,
            detail={"error": repr(exc), "room": room, "identity": identity},
        ) from exc


# Mount after API routes so /health and /token remain handled by FastAPI.
# Absent before the first `npm run build`; the token endpoint still works.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
