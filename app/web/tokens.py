"""LiveKit admin work behind ``GET /token``: room, dispatch, and the JWT.

The route in ``app.web.http`` owns the HTTP shape - authentication, the room and
identity names, the error response. Everything that talks to the LiveKit server
lives here.
"""

from __future__ import annotations

from typing import Any

from livekit import api

from app.config import LiveKitSettings


async def close_lkapi(lkapi: api.LiveKitAPI) -> None:
    try:
        if hasattr(lkapi, "aclose"):
            await lkapi.aclose()
        elif hasattr(lkapi, "close"):
            await lkapi.close()
    except Exception:
        pass


async def create_room_token(
    *,
    room: str,
    identity: str,
    settings: LiveKitSettings,
) -> dict[str, Any]:
    """Create the room, dispatch the agent into it, and mint the browser's JWT.

    Returns the payload the ``/token`` route hands back. Exceptions propagate to
    the route, which turns them into the 500 the frontend knows how to report.
    """
    livekit_api_key = settings.api_key
    livekit_api_secret = settings.api_secret
    public_livekit_url = settings.public_url
    livekit_api_url = settings.url
    agent_name = settings.agent_name
    manual_dispatch = settings.manual_dispatch

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
    finally:
        if lkapi is not None:
            await close_lkapi(lkapi)
