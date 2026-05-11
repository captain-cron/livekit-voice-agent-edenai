"""Voice agent web frontend.

Serves the React SPA and a /api/token endpoint that mints LiveKit access
tokens. All non-auth routes (including the token mint) are gated behind a
SuperTokens session — users sign in with the same credentials they use for the
portals.cx CRM, since both apps share the same SuperTokens connection.

Cross-domain note: SuperTokens cookies are scoped to PUBLIC_DOMAIN. Today
voice-agent and portals.cx live on different hostnames, so users sign in
separately on this domain. If we ever park voice-agent on a subdomain of
portals.cx, point ``PUBLIC_DOMAIN`` at the apex and set
``cookie_domain="portals.cx"`` in the session.init() below to share sessions.
"""

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
from supertokens_python.recipe import emailpassword, session
from supertokens_python.recipe.session import SessionContainer
from supertokens_python.recipe.session.asyncio import get_session
from supertokens_python.recipe.session.framework.fastapi import verify_session
from starlette.middleware.cors import CORSMiddleware


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
            session.init(),
            emailpassword.init(),
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
        raise HTTPException(status_code=400, detail="Invalid mode. Expected 'pipeline' or 'realtime'.")

    # Tie the LiveKit participant identity to the SuperTokens user so the
    # downstream transcript / action-item extractor knows who was on the call.
    user_id = s.get_user_id()
    room_name = body.get("room", f"{mode}-room-{uuid.uuid4().hex[:8]}")
    identity = body.get("identity") or f"user-{user_id[:8]}"

    token = (
        api.AccessToken(api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
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
    # Clear cookies so the browser doesn't keep sending the revoked tokens.
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
    """Catch-all: serve the SPA, but redirect anonymous users to /sign-in.

    The /sign-in and /auth/* routes are registered above and are matched
    first by FastAPI's router, so this handler never sees them.
    """
    s = await get_session(request, session_required=False)
    if s is None:
        # Send the user to sign in, preserving where they wanted to go.
        return RedirectResponse(f"/sign-in?return_to=/{full_path}", status_code=303)
    return FileResponse(str(DIST_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
