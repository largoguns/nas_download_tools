FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8090 \
    DATABASE_PATH=/data/anime_queue.db \
    DOWNLOAD_DIR=/downloads

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME ["/data", "/downloads"]
EXPOSE 8090

CMD ["python", "app.py"]
