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

# Nightly yt-dlp, pinned into the IMAGE rather than installed at boot.
# This used to live in CMD, which meant every cold start (Render's free tier
# sleeps after ~15 min idle) paid a full PyPI download+install — 30-60s before
# uvicorn even bound a port, on top of the container start itself. Baking it in
# moves that cost to build time, where nobody is waiting.
# BUILD_STAMP busts Docker's layer cache so a redeploy still pulls a fresh
# nightly: pass --build-arg BUILD_STAMP=$(date +%s), or just bump it.
ARG BUILD_STAMP=1
RUN pip install --no-cache-dir -U --pre 'yt-dlp[default]' bgutil-ytdlp-pot-provider

# Build the bgutil PO-token HTTP server. yt-dlp's bgutil plugin auto-connects
# to it on 127.0.0.1:4416 and mints the proof-of-origin tokens YouTube now
# requires to release real video formats.
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil && \
    cd /opt/bgutil/server && \
    npm install && \
    npx tsc

COPY . .

EXPOSE 8000

# Startup: launch the PO-token server, WAIT for it to actually bind, then start
# the API. The old blind `sleep 3` was a gamble — if the server wasn't listening
# yet, the bgutil plugin silently fell back to spawning a Node subprocess per
# token, which costs ~15-20s per player client on every extraction. Polling the
# port turns that race into a certainty.
CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js & for i in $(seq 1 40); do if node -e 'require(\"net\").connect(4416,\"127.0.0.1\").on(\"connect\",()=>process.exit(0)).on(\"error\",()=>process.exit(1))' 2>/dev/null; then echo 'pot server ready'; break; fi; sleep 0.5; done; uvicorn backend:app --host 0.0.0.0 --port ${PORT:-8000}"]
