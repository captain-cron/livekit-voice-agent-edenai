"""Voice agent powered by Eden AI v3 (LLM, STT, TTS).

Both /pipeline and /realtime web routes are served by the same Eden AI
pipeline. Eden AI does not expose a speech-to-speech realtime endpoint, so the
"realtime" mode in the web UI runs through the same STT -> LLM -> TTS chain.
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AgentServer,
    JobContext,
    JobProcess,
    cli,
)
from livekit.plugins import openai, silero

from edenai_plugin import EdenAISTT, EdenAITTS

load_dotenv()
logger = logging.getLogger("voice-agent")

server = AgentServer()

EDENAI_LLM_BASE_URL = "https://api.edenai.run/v3"


class VoiceAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a friendly voice AI assistant. "
                "Keep your responses concise and conversational. "
                "You are helpful, witty, and knowledgeable."
            ),
        )


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


def resolve_room_mode(room_name: str) -> str:
    if room_name.startswith("realtime-"):
        return "realtime"
    if room_name.startswith("pipeline-"):
        return "pipeline"
    return "pipeline"


def _build_session(ctx: JobContext) -> AgentSession:
    llm_model = os.environ.get("EDENAI_MODEL", "openai/gpt-5.4-mini")
    stt_model = os.environ.get(
        "EDENAI_STT_PROVIDER", "audio/speech_to_text_async/deepgram/nova-3"
    )
    tts_model = os.environ.get(
        "EDENAI_TTS_PROVIDER", "audio/tts/elevenlabs/eleven_flash_v2_5"
    )

    return AgentSession(
        stt=EdenAISTT(
            model=stt_model,
            api_key=os.environ["EDENAI_API_KEY"],
        ),
        llm=openai.LLM(
            model=llm_model,
            base_url=EDENAI_LLM_BASE_URL,
            api_key=os.environ["EDENAI_API_KEY"],
        ),
        tts=EdenAITTS(
            model=tts_model,
            api_key=os.environ["EDENAI_API_KEY"],
        ),
        vad=ctx.proc.userdata["vad"],
        # Strict ping-pong turn-taking: agent finishes its turn before listening,
        # and STT only finalizes after the user has been silent for ~1.2s.
        allow_interruptions=False,
        min_endpointing_delay=1.2,
    )


def _read_participant_context(participant) -> dict:
    """Decode the participant metadata the web-frontend stuffed into the token.

    Expected shape (set in web-frontend/main.py /api/token):
        {"user_id": "...", "roles": ["..."], "context": {...}}
    """
    raw = getattr(participant, "metadata", "") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("participant metadata was not valid JSON: %r", raw[:200])
        return {}


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    room_name = getattr(ctx.room, "name", "")
    mode = resolve_room_mode(room_name)

    # Wait for the user to actually join before we read their metadata —
    # the LiveKit token's metadata is attached to the participant, not the
    # room. ctx.wait_for_participant() blocks until exactly one user is in.
    participant = await ctx.wait_for_participant()
    ctxinfo = _read_participant_context(participant)
    user_id = ctxinfo.get("user_id")
    roles = ctxinfo.get("roles") or []
    extra = ctxinfo.get("context") or {}
    logger.info(
        "Starting %s session for room %s (user_id=%s roles=%s context=%s)",
        mode, room_name, user_id, roles, extra,
    )

    # Branch behaviour by role here if needed. For now everyone gets the
    # default assistant; the role + any frontend-supplied context (e.g.
    # report_id) is available for whoever wires the meeting/report flow.
    session = _build_session(ctx)
    await session.start(agent=VoiceAssistant(), room=ctx.room)
    # Small settle delay so the user's first connection-audio doesn't race
    # against the greeting turn.
    await asyncio.sleep(0.5)
    await session.generate_reply(
        instructions="Greet the user and offer your assistance."
    )


if __name__ == "__main__":
    cli.run_app(server)
