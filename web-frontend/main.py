"""Web frontend for LiveKit voice agent — serves React SPA + token API."""

import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from livekit import api

app = FastAPI(title="LiveKit Voice Agent")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
VALID_MODES = {"pipeline", "realtime"}

DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")


# --- API ---

@app.post("/api/token")
async def create_token(request: Request):
    body = await request.json()
    requested_mode = body.get("mode", "pipeline")
    mode = requested_mode if requested_mode in VALID_MODES else None
    if mode is None:
        raise HTTPException(status_code=400, detail="Invalid mode. Expected 'pipeline' or 'realtime'.")

    room_name = body.get("room", f"{mode}-room-{uuid.uuid4().hex[:8]}")
    identity = body.get("identity", f"user-{uuid.uuid4().hex[:6]}")

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
    }


# --- SPA static files ---

if os.path.isdir(os.path.join(DIST_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST_DIR, "assets")), name="assets")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Catch-all: serve index.html for client-side routing."""
    return FileResponse(os.path.join(DIST_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
