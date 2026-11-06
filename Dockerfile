FROM denoland/deno:bin AS deno
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim-bookworm AS whisper

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    WHISPER_MODEL=small \
    WHISPER_DEVICE=cuda \
    WHISPER_COMPUTE_TYPE=float16 \
    WHISPER_PORT=8000 \
    LD_LIBRARY_PATH="/usr/local/lib/python3.13/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.13/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.13/site-packages/nvidia/cuda_nvrtc/lib"

RUN uv pip install --system --no-cache \
        faster-whisper \
        aiohttp \
        nvidia-cublas-cu12 \
        nvidia-cudnn-cu12 \
    && python -c "from faster_whisper.utils import download_model; download_model('small')"

COPY whisper_server.py ./

EXPOSE 8000

CMD ["python", "whisper_server.py"]

FROM python:3.13-slim-bookworm AS bot

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
    && mkdir -p /app/uploads /app/audio

ENV PATH="/app/.venv/bin:$PATH"

VOLUME ["/app/uploads", "/app/audio"]

CMD ["rubychan-bot"]
