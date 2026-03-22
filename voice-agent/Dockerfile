FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies from lockfile
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Download Silero VAD model files
RUN uv run python -c "from livekit.plugins.silero import VAD; VAD.load()" || true

# Copy agent source
COPY . .
RUN uv sync --frozen

CMD ["uv", "run", "python", "agent.py", "start"]
