# NOTE: no `# syntax=docker/dockerfile:1` directive here — it makes BuildKit
# download the frontend from docker.io, which times out behind CN networks.
# The bundled frontend (Docker 23+) supports everything below.

# ---------- Builder stage ---------------------------------------------------
FROM ghcr.m.daocloud.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first (cache-friendly: only rebuilt when pyproject/uv.lock change)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy the rest of the source and install the project into .venv
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- Runtime stage ----------------------------------------------------
FROM docker.m.daocloud.io/python:3.12-slim-bookworm

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=9000

# Run as a non-root user
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app app

# Copy the venv + source from the builder (keeps the image small, no uv in runtime)
COPY --from=builder --chown=app:app /app /app

USER app
EXPOSE 9000

# Exit non-zero if the health endpoint stops answering
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '9000'))" || exit 1

# main.py starts both the FastAPI server and the Feishu WebSocket client
CMD ["python", "main.py"]