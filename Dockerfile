# Production image for the REST gateway. Multi-stage: build the locked,
# project-only virtualenv with uv, then copy just that venv into a slim,
# non-root runtime. (docker-compose.yml is for local dev only.)

FROM python:3.12-slim-bookworm AS builder

# Pinned uv binary — no need to install it via pip.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependency layer first — cached until the lockfile or manifest changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev --extra sqlite

# Then the project itself, installed as a wheel (not editable).
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra sqlite


FROM python:3.12-slim-bookworm AS runtime

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 glassrail

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GLASSRAIL_LOG_LEVEL=INFO

WORKDIR /app
COPY --from=builder --chown=glassrail:glassrail /app/.venv /app/.venv

USER glassrail
EXPOSE 8000

# Stdlib-only health probe (the slim image has no curl).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status == 200 else 1)"]

CMD ["uvicorn", "glassrail.gateways.rest:app", "--host", "0.0.0.0", "--port", "8000"]
