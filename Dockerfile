FROM python:3.11-slim

# Install ffmpeg + aria2 (fast downloads + audio extraction), Node.js + git
# (to build/run the bgutil PO-token server), and Deno (the JS runtime yt-dlp
# uses to solve YouTube's n-signature challenge — Node 20 is below yt-dlp's
# >=22 requirement, so Deno does the signature solving while Node runs the
# token server).
RUN apt-get update && \
    apt-get install -y ffmpeg aria2 curl git unzip && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build the bgutil PO-token HTTP server. yt-dlp's bgutil plugin auto-connects
# to it on 127.0.0.1:4416 and mints the proof-of-origin tokens YouTube now
# requires to release real video formats.
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil && \
    cd /opt/bgutil/server && \
    npm install && \
    npx tsc

COPY . .

EXPOSE 8000

# Startup: (1) force-upgrade yt-dlp (extractors break often), (2) launch the
# PO-token server in the background, (3) start the API. The `sleep` gives the
# token server a moment to bind before the first request.
CMD ["sh", "-c", "pip install --no-cache-dir -U --pre 'yt-dlp[default]' bgutil-ytdlp-pot-provider; node /opt/bgutil/server/build/main.js & sleep 3; uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8000}"]
