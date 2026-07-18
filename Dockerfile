FROM python:3.11-slim

# Install ffmpeg and aria2 (needed for fast downloads + audio extraction)
RUN apt-get update && \
    apt-get install -y ffmpeg aria2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Upgrade yt-dlp on every container start: platform extractors (YouTube,
# TikTok, Instagram...) break often and fixes ship in new releases.
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp; uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8000}"]