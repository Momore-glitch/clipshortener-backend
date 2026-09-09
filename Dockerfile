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
    HOME=/root

RUN apk add --no-cache \
      python3 \
      py3-pip \
      curl \
      ca-certificates \
      tar \
      xvfb \
      chromium \
      nss \
      freetype \
      harfbuzz \
      ttf-freefont

# Official yt-session-generator source. We patch Chromium startup only because
# this service runs Chromium as root inside the container.
RUN mkdir -p /opt/yt-session-generator \
    && curl -fL --retry 8 --retry-delay 3 --retry-connrefused \
       https://codeload.github.com/imputnet/yt-session-generator/tar.gz/refs/heads/main \
       -o /tmp/yt-session-generator.tar.gz \
    && tar -xzf /tmp/yt-session-generator.tar.gz \
       -C /opt/yt-session-generator --strip-components=1 \
    && python3 -m pip install --break-system-packages --no-cache-dir \
       -r /opt/yt-session-generator/requirements.txt \
    && python3 - <<'PY'
from pathlib import Path
p = Path('/opt/yt-session-generator/potoken_generator/extractor.py')
s = p.read_text()
old = '''browser = await nodriver.start(headless=False,\n                                               browser_executable_path=self.browser_path,\n                                               user_data_dir=self.profile_path)'''
new = '''browser = await nodriver.start(headless=False,\n                                               browser_executable_path=self.browser_path,\n                                               user_data_dir=self.profile_path,\n                                               no_sandbox=True,\n                                               browser_args=[\n                                                   "--disable-dev-shm-usage",\n                                                   "--disable-gpu",\n                                               ])'''
if old not in s:
    raise SystemExit('Expected yt-session-generator startup block was not found.')
p.write_text(s.replace(old, new, 1))
print('patched yt-session-generator for root Chromium')
PY

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh \
    && mkdir -p /tmp/yt-session

EXPOSE 9000
ENTRYPOINT ["/app/start.sh"]
