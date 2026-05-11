"""Voice agent web frontend.

Serves the React SPA and a /api/token endpoint that mints LiveKit access
tokens. All non-auth routes are gated behind a SuperTokens session that is
shared across *.portals.cx (cookie scoped to the apex), so a user signed in
to portals.cx is automatically authed when they iframe-embed or visit
voice1.portals.cx.

Access is also role-gated: /api/token only mints a LiveKit token if the user
has at least one role listed in VOICE_AGENT_ALLOWED_ROLES. Those roles are
passed through to the agent worker as LiveKit participant metadata so the
agent can branch behavior (e.g. admin-only tools) per session.
"""

import json
import logging
import os
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from livekit import api
from supertokens_python import (
    InputAppInfo,
    SupertokensConfig,
    get_all_cors_headers,
    init,
)
from supertokens_python.framework.fastapi import get_middleware
from supertokens_python.recipe import emailpassword, session, userroles
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.asyncio import get_session
from supertokens_python.recipe.session.framework.fastapi import verify_session
from supertokens_python.recipe.userroles.asyncio import get_roles_for_user
from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger("voice-web")


# --- Config ---

PUBLIC_DOMAIN = os.environ.get(
    "PUBLIC_DOMAIN",
    "https://web-frontend-production-509b.up.railway.app",
).rstrip("/")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")

SUPERTOKENS_CONNECTION_URI = os.environ.get("SUPERTOKENS_CONNECTION_URI", "")
SUPERTOKENS_API_KEY = os.environ.get("SUPERTOKENS_API_KEY")

# Apex domain that both portals.cx and voice1.portals.cx share. Setting this
# makes SuperTokens write the session cookie with Domain=.portals.cx, which is
# what lets the portals.cx session ride into the voice-agent iframe.
SESSION_COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None

# Comma-separated roles that are allowed to use the voice agent. If empty,
# any authenticated user is allowed (useful for first-time testing).
VOICE_AGENT_ALLOWED_ROLES = {
    r.strip() for r in os.environ.get(
        "VOICE_AGENT_ALLOWED_ROLES", "voice-agent-user,admin"
    ).split(",") if r.strip()
}

VALID_MODES = {"pipeline", "realtime"}
DIST_DIR = Path(__file__).parent / "dist"
SIGNIN_HTML = (Path(__file__).parent / "signin.html").read_text(encoding="utf-8")


# --- SuperTokens init ---

if SUPERTOKENS_CONNECTION_URI:
    init(
        app_info=InputAppInfo(
            app_name="Voice Agent",
            api_domain=PUBLIC_DOMAIN,
            website_domain=PUBLIC_DOMAIN,
            api_base_path="/auth",
            website_base_path="/sign-in",
        ),
        supertokens_config=SupertokensConfig(
            connection_uri=SUPERTOKENS_CONNECTION_URI,
            api_key=SUPERTOKENS_API_KEY,
        ),
        framework="fastapi",
        recipe_list=[
            session.init(
                cookie_domain=SESSION_COOKIE_DOMAIN,
                cookie_same_site="lax",
            ),
            emailpassword.init(),
            userroles.init(),
        ],
        mode="asgi",
    )


# --- App ---

app = FastAPI(title="LiveKit Voice Agent")

# SuperTokens CORS headers + same-origin defaults.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[PUBLIC_DOMAIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"] + get_all_cors_headers(),
)
app.add_middleware(get_middleware())


async def _lookup_user_roles(user_id: str) -> list[str]:
    """Get the user's SuperTokens roles. Empty list on lookup error."""
    try:
        resp = await get_roles_for_user("public", user_id)
        return list(resp.roles or [])
    except Exception as exc:  # noqa: BLE001 — never block auth on a SuperTokens hiccup
        logger.warning("get_roles_for_user failed for %s: %s", user_id, exc)
        return []


# --- API ---


@app.post("/api/token")
async def create_token(
    request: Request,
    s: SessionContainer = Depends(verify_session()),
):
    body = await request.json()
    requested_mode = body.get("mode", "pipeline")
    mode = requested_mode if requested_mode in VALID_MODES else None
    if mode is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid mode. Expected 'pipeline' or 'realtime'.",
        )

    user_id = s.get_user_id()
    roles = await _lookup_user_roles(user_id)

    # Role gate: empty allowlist = "any authenticated user". Otherwise the
    # user must have at least one role from the allowlist.
    if VOICE_AGENT_ALLOWED_ROLES and not (
        VOICE_AGENT_ALLOWED_ROLES.intersection(roles)
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your account is signed in but doesn't have a role "
                f"permitted to use the voice agent. Allowed roles: "
                f"{sorted(VOICE_AGENT_ALLOWED_ROLES)}"
            ),
        )

    room_name = body.get("room", f"{mode}-room-{uuid.uuid4().hex[:8]}")
    identity = body.get("identity") or f"user-{user_id[:8]}"

    # Anything we want the agent worker to know about this user goes here.
    # The agent reads participant.metadata in agent.py:entrypoint().
    participant_metadata = json.dumps({
        "user_id": user_id,
        "roles": roles,
        # Frontend-supplied context (report_id, customer_id, ...) flows
        # through so the agent can fetch the right data. The host app sets
        # this when it constructs the iframe URL or token request body.
        "context": body.get("context") or {},
    })

    token = (
        api.AccessToken(api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_metadata(participant_metadata)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    return {
        "token": token,
        "url": LIVEKIT_URL,
        "room": room_name,
        "identity": identity,
        "mode": mode,
        "user_id": user_id,
        "roles": roles,
    }


# --- Auth pages ---


@app.get("/sign-in")
async def sign_in_page() -> Response:
    """Minimal sign-in form that POSTs to /auth/signin."""
    return Response(content=SIGNIN_HTML, media_type="text/html")


@app.get("/sign-out")
async def sign_out_page(request: Request) -> Response:
    s = await get_session(request, session_required=False)
    if s is not None:
        await s.revoke_session()
    response = RedirectResponse("/sign-in", status_code=303)
    for name in ("sAccessToken", "sRefreshToken", "sIdRefreshToken"):
        response.delete_cookie(name, path="/")
    return response


# --- SPA static files (gated) ---


if (DIST_DIR / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(DIST_DIR / "assets")),
        name="assets",
    )


@app.get("/{full_path:path}")
async def serve_spa(full_path: str, request: Request) -> Response:
    """Catch-all: serve the SPA, but redirect anonymous users to /sign-in."""
    s = await get_session(request, session_required=False)
    if s is None:
        return RedirectResponse(f"/sign-in?return_to=/{full_path}", status_code=303)
    return FileResponse(str(DIST_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
