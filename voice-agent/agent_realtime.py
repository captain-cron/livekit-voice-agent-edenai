"""Entrypoint kept for backwards compatibility.

Eden AI does not expose a speech-to-speech realtime endpoint, so this module
delegates to the standard Eden AI STT -> LLM -> TTS pipeline defined in
``agent.py``.
"""

from agent import cli, server  # noqa: F401


if __name__ == "__main__":
    cli.run_app(server)
