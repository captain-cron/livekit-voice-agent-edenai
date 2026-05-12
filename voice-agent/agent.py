"""Voice agent powered by Eden AI v3 (LLM, STT, TTS).

The worker auto-joins any room our self-hosted livekit-server hosts. The
room name + participant metadata determine how the agent behaves:

  - ``portals-cx-roc-<uuid>``  → ROC mode. Bot calls portals.cx for the
    rep's funnel + admin-editable instructions, reads through items,
    captures replies. Each turn is mirrored to portals.cx (transcript
    persistence) and an end-of-call sweep triggers action extraction.

  - ``pipeline-...`` / ``realtime-...`` (from voice1.portals.cx)
    → generic assistant (default prompt).

Eden AI has no speech-to-speech endpoint, so both modes use the same
STT -> LLM -> TTS pipeline.
"""

import asyncio
import json
import logging
import os
from contextlib import suppress
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AgentServer,
    JobContext,
    JobProcess,
    cli,
)
from livekit.agents.voice import events as agent_events
from livekit.plugins import openai, silero

from edenai_plugin import EdenAISTT, EdenAITTS
from roc_client import RocClient

load_dotenv()
logger = logging.getLogger("voice-agent")

server = AgentServer()

EDENAI_LLM_BASE_URL = "https://api.edenai.run/v3"
DEFAULT_INSTRUCTIONS = (
    "You are a friendly voice AI assistant. "
    "Keep your responses concise and conversational. "
    "You are helpful, witty, and knowledgeable."
)


class VoiceAssistant(Agent):
    def __init__(self, instructions: str = DEFAULT_INSTRUCTIONS) -> None:
        super().__init__(instructions=instructions)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


def resolve_room_mode(room_name: str) -> str:
    if room_name.startswith("portals-cx-roc-"):
        return "roc"
    if room_name.startswith("realtime-"):
        return "realtime"
    if room_name.startswith("pipeline-"):
        return "pipeline"
    return "pipeline"


def _build_session(ctx: JobContext, llm_model_override: str | None = None) -> AgentSession:
    llm_model = llm_model_override or os.environ.get(
        "EDENAI_MODEL", "openai/gpt-5.4-mini"
    )
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
        # Strict ping-pong turn-taking: agent finishes its turn before
        # listening, STT only finalizes after the user has been silent
        # for ~1.2s. Tuned for structured Q&A (the ROC use case).
        allow_interruptions=False,
        min_endpointing_delay=1.2,
    )


def _read_participant_context(participant: Any) -> dict[str, Any]:
    """Decode the LiveKit participant.metadata JSON. Returns {} on miss."""
    raw = getattr(participant, "metadata", "") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("participant metadata was not valid JSON: %r", raw[:200])
        return {}


def _format_roc_instructions(
    base_prompt: str,
    funnel: list[dict[str, Any]],
    user: dict[str, Any] | None,
) -> str:
    """Compose the system prompt for an ROC session by appending the rep's
    funnel as compact JSON the LLM can quote when it asks about each opp."""
    rep_name = (
        (user.get("firstName") or "").strip() if user else ""
    ) or "the rep"
    # Trim to the first 25 rows + just the columns the bot needs, so we
    # don't blow the context window on huge funnels.
    compact = [
        {
            "opportunityId": r.get("opportunityId"),
            "oppNumber": r.get("oppNumber"),
            "name": r.get("oppName"),
            "status": r.get("oppStatus"),
            "customer": r.get("customerName"),
            "mrc": r.get("oppEstimatedMrc"),
            "closeDate": r.get("oppCloseDate"),
        }
        for r in (funnel or [])[:25]
    ]
    return (
        f"{base_prompt}\n\n"
        f"You are speaking with {rep_name}. Here is their open funnel as a "
        f"JSON list (newest first). Walk through them one at a time, ask for "
        f"a status update, log any follow-ups the rep asks for, and offer "
        f"to email the customer when a contract is overdue. Keep replies "
        f"short — this is a phone-quality voice line.\n\n"
        f"FUNNEL: {json.dumps(compact, default=str)}"
    )


def _format_greeting(template: str, user: dict[str, Any] | None, funnel_count: int) -> str:
    first_name = (user.get("firstName") if user else None) or "there"
    return (
        template
        .replace("{firstName}", first_name)
        .replace("{oppCount}", str(funnel_count))
    )


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    room_name = getattr(ctx.room, "name", "")
    mode = resolve_room_mode(room_name)

    participant = await ctx.wait_for_participant()
    meta = _read_participant_context(participant)
    logger.info(
        "Session start mode=%s room=%s participant_kind=%s",
        mode, room_name, meta.get("kind"),
    )

    if mode == "roc" and meta.get("kind") == "roc":
        await _run_roc_session(ctx, meta)
    else:
        await _run_generic_session(ctx, meta)


async def _run_generic_session(ctx: JobContext, meta: dict[str, Any]) -> None:
    session = _build_session(ctx)
    await session.start(agent=VoiceAssistant(), room=ctx.room)
    await asyncio.sleep(0.5)
    await session.generate_reply(
        instructions="Greet the user and offer your assistance."
    )


async def _run_roc_session(ctx: JobContext, meta: dict[str, Any]) -> None:
    session_id = meta.get("sessionId")
    if not session_id:
        logger.warning("ROC room missing sessionId in metadata; falling back to generic")
        return await _run_generic_session(ctx, meta)

    client = RocClient()
    if not client.configured:
        logger.error(
            "ROC session %s started but PORTALS_CX_BASE_URL / "
            "VOICE_AGENT_INTERNAL_TOKEN aren't set on the worker. "
            "Falling back to generic assistant.",
            session_id,
        )
        return await _run_generic_session(ctx, meta)

    bundle = await client.fetch_session_context(session_id)
    if not bundle:
        logger.error("ROC session %s context fetch failed; generic fallback", session_id)
        return await _run_generic_session(ctx, meta)

    config = bundle.get("config")
    user = bundle.get("user")
    funnel = bundle.get("funnel") or []

    if not config:
        logger.warning(
            "ROC session %s has no voice_agent_config (org has no default + "
            "no override). Generic assistant.",
            session_id,
        )
        return await _run_generic_session(ctx, meta)

    instructions = _format_roc_instructions(config["systemPrompt"], funnel, user)
    greeting = _format_greeting(config["greetingTemplate"], user, len(funnel))

    session = _build_session(ctx, llm_model_override=config.get("chatModel"))

    # Mirror user + agent turns into roc_transcript_lines. We don't await
    # the POSTs in the hot path — they go to a background task so a slow
    # portals.cx never starves the conversation.
    pending_writes: list[asyncio.Task[Any]] = []

    def _record(speaker: str, text: str, meta_extra: dict[str, Any] | None = None) -> None:
        task = asyncio.create_task(
            client.post_transcript(session_id, speaker, text, meta_extra),
        )
        pending_writes.append(task)

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev: agent_events.UserInputTranscribedEvent) -> None:
        if ev.is_final and ev.transcript:
            _record("user", ev.transcript)

    @session.on("conversation_item_added")
    def _on_item_added(ev: agent_events.ConversationItemAddedEvent) -> None:
        item = ev.item
        text = getattr(item, "text_content", None) or getattr(item, "content", None)
        role = getattr(item, "role", None)
        if text and role == "assistant":
            _record("agent", str(text))

    await session.start(agent=VoiceAssistant(instructions=instructions), room=ctx.room)
    await asyncio.sleep(0.5)
    _record("system", f"Session opened. Funnel rows: {len(funnel)}.", {"phase": "open"})
    await session.generate_reply(instructions=greeting)

    # Park the worker until the rep disconnects, then trigger end-of-call.
    # ctx.room emits "disconnected" / "participant_disconnected" — we wait
    # for the participant we were paired with to leave.
    try:
        await ctx.wait_for_participant_disconnect()
    except AttributeError:
        # Older livekit-agents versions don't expose that helper; poll the
        # room's remote participants instead.
        while ctx.room.remote_participants:
            await asyncio.sleep(1.0)

    # Flush any pending transcript POSTs we haven't awaited yet, then run
    # the end-of-call sweep on portals.cx.
    if pending_writes:
        with suppress(Exception):
            await asyncio.gather(*pending_writes)
    await client.post_end_of_call(session_id)


if __name__ == "__main__":
    cli.run_app(server)
