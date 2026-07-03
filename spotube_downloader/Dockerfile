FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt


FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATABASE_PATH=/data/queue.db \
    MUSIC_DIR=/music \
    SPOTDL_FORMAT=mp3 \
    SPOTDL_BITRATE=192k \
    SPOTDL_THREADS=1 \
    SPOTDL_AUDIO_PROVIDERS=youtube-music,youtube \
    SPOTDL_LOG_LEVEL=INFO \
    SPOTDL_LOG_TRACEBACKS=false \
    SPOTDL_REPROCESS_MISSING_ONLY=false \
    SPOTDL_MAX_ATTEMPTS=5 \
    SPOTDL_RETRY_DELAY_SECONDS=120 \
    SPOTDL_RETRY_BACKOFF_FACTOR=2 \
    SPOTDL_RETRY_MAX_DELAY_SECONDS=900 \
    SPOTDL_RETRY_ALL_FAILURES=true \
    SPOTDL_SPOTIFY_MAX_RETRIES=5 \
    SPOTDL_SAVE_TIMEOUT_SECONDS=60 \
    SPOTDL_COOKIE_FILE= \
    SPOTDL_YT_DLP_ARGS= \
    METADATA_WORKER_SLEEP_SECONDS=10 \
    SPOTIFY_METADATA_TIMEOUT=8 \
    WORKER_SLEEP_SECONDS=5

WORKDIR /app
COPY app.py requirements.txt ./
COPY templates ./templates

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /music /data \
    && chown -R appuser:appuser /app /music /data

USER appuser

RUN spotdl --download-deno

EXPOSE 8080
VOLUME ["/music", "/data"]

CMD ["python", "app.py"]
