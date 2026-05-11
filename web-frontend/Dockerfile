# Stage 1: Build React app
FROM node:22-slim AS frontend-build

WORKDIR /build
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/ ./
RUN npm run build -- --outDir /dist

# Stage 2: Python runtime
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
# Lock is intentionally not frozen so adding new deps in pyproject.toml
# (e.g. supertokens-python for the auth gate) doesn't require regenerating
# uv.lock locally on every change.
RUN uv sync --no-install-project

COPY main.py signin.html ./
COPY --from=frontend-build /dist ./dist

RUN uv sync

CMD ["uv", "run", "python", "main.py"]
