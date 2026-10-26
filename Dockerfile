FROM denoland/deno:bin AS deno
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=uv /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

COPY README.md bot.py ./

RUN uv sync --frozen --no-dev \
    && mkdir -p /app/uploads

ENV PATH="/app/.venv/bin:$PATH"

VOLUME ["/app/uploads"]

CMD ["rubychan-bot"]
