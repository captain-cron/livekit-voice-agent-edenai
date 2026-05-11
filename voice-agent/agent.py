"""Voice agent powered by Eden AI (LLM, STT, TTS).

Both /pipeline and /realtime routes are served by the same Eden AI pipeline.
Eden AI does not currently expose a speech-to-speech realtime endpoint, so the
"realtime" mode in the web UI runs through the same STT -> LLM -> TTS chain.
"""

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

EDENAI_BASE_URL = "https://api.edenai.run/v2/llm"


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
    llm_model = os.environ.get("EDENAI_MODEL", "openai/gpt-4.1-mini")
    stt_provider = os.environ.get(
        "EDENAI_STT_PROVIDER", "audio/speech_to_text_async/deepgram/nova-3"
    )
    tts_provider = os.environ.get(
        "EDENAI_TTS_PROVIDER", "audio/tts/elevenlabs/eleven_flash_v2_5"
    )

    # The .env values include the full Eden AI feature path; strip the feature
    # prefix since the plugin only needs the provider/model spec.
    stt_spec = stt_provider.split("speech_to_text_async/", 1)[-1]
    tts_spec = tts_provider.split("tts/", 1)[-1]

    return AgentSession(
        stt=EdenAISTT(
            provider_spec=stt_spec,
            api_key=os.environ["EDENAI_API_KEY"],
        ),
        llm=openai.LLM(
            model=llm_model,
            base_url=EDENAI_BASE_URL,
            api_key=os.environ["EDENAI_API_KEY"],
        ),
        tts=EdenAITTS(
            provider_spec=tts_spec,
            api_key=os.environ["EDENAI_API_KEY"],
        ),
        vad=ctx.proc.userdata["vad"],
    )


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    room_name = getattr(ctx.room, "name", "")
    mode = resolve_room_mode(room_name)
    logger.info("Starting %s session for room %s (Eden AI pipeline)", mode, room_name)

    session = _build_session(ctx)
    await session.start(agent=VoiceAssistant(), room=ctx.room)
    await session.generate_reply(
        instructions="Greet the user and offer your assistance."
    )


if __name__ == "__main__":
    cli.run_app(server)
