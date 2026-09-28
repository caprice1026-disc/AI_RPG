FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 PYDANTIC_AI_NO_BANNER=1 \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN uv sync --frozen --no-dev --no-editable \
    && useradd --create-home --uid 10001 airpg
USER 10001:10001
