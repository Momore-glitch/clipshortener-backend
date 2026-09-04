# ClipShortener — Render Docker deployment
FROM ghcr.io/imputnet/cobalt:11.7.1

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_URL=http://127.0.0.1:9000/ \
    API_PORT=9000 \
    API_LISTEN_ADDRESS=127.0.0.1 \
    API_AUTH_REQUIRED=0 \
    CORS_WILDCARD=1 \
    COBALT_API_URL=http://127.0.0.1:9000 \
    BGUTIL_API_URL=http://127.0.0.1:4416 \
    NO_PROXY=127.0.0.1,localhost,::1 \
    no_proxy=127.0.0.1,localhost,::1 \
    HOME=/root \
    CLIPSHORTENER_MAX_VIDEO_GB=10 \
    CLIPSHORTENER_UPLOAD_CHUNK_MB=64

RUN apk add --no-cache \
      python3 py3-pip ffmpeg curl ca-certificates git make g++ \
      cairo-dev pango-dev jpeg-dev giflib-dev librsvg-dev pixman-dev

WORKDIR /opt
RUN git clone --depth 1 --branch 1.3.2 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
      bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci \
    && npx tsc

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m pip install --break-system-packages --no-cache-dir --prefer-binary -r requirements.txt

COPY app.py ./
COPY start.sh ./

RUN chmod +x /app/start.sh \
    && mkdir -p /tmp/clipshortener /tmp/yt-session

EXPOSE 10000
ENTRYPOINT ["/app/start.sh"]
