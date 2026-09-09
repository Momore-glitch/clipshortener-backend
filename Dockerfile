# ClipShortener platform acquisition service
# Cobalt + official yt-session-generator on the SAME container/IP.
FROM ghcr.io/imputnet/cobalt:11.7.1

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_URL=http://127.0.0.1:9000/ \
    API_PORT=9000 \
    API_LISTEN_ADDRESS=0.0.0.0 \
    API_AUTH_REQUIRED=0 \
    CORS_WILDCARD=1 \
    YOUTUBE_SESSION_SERVER=http://127.0.0.1:8080/token \
    YOUTUBE_SESSION_INNERTUBE_CLIENT=WEB_EMBEDDED \
    DISPLAY=:99 \
    HOME=/root \
    NO_PROXY=127.0.0.1,localhost

RUN apk add --no-cache \
      python3 \
      py3-pip \
      curl \
      ca-certificates \
      tar \
      xvfb \
      chromium \
      su-exec \
      nss \
      freetype \
      harfbuzz \
      ttf-freefont

# Official yt-session-generator source. The generator is run as an unprivileged
# user so Chromium can keep its normal sandbox behavior inside Render.
RUN addgroup -S ytuser \
    && adduser -S -D -h /home/ytuser -G ytuser ytuser \
    && mkdir -p /opt/yt-session-generator /tmp/yt-home /tmp/yt-session \
    && chown -R ytuser:ytuser /tmp/yt-home /tmp/yt-session \
    && mkdir -p /opt/yt-session-generator \
    && curl -fL --retry 8 --retry-delay 3 --retry-connrefused \
       https://codeload.github.com/imputnet/yt-session-generator/tar.gz/refs/heads/main \
       -o /tmp/yt-session-generator.tar.gz \
    && tar -xzf /tmp/yt-session-generator.tar.gz \
       -C /opt/yt-session-generator --strip-components=1 \
    && python3 -m pip install --break-system-packages --no-cache-dir \
       -r /opt/yt-session-generator/requirements.txt \
    && echo "yt-session-generator installed"

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh \
    && mkdir -p /tmp/yt-session

EXPOSE 9000
ENTRYPOINT ["/app/start.sh"]
