#!/bin/sh
set -eu

SESSION_PID=''
COBALT_PID=''
XVFB_PID=''

mkdir -p /tmp/yt-session /tmp/yt-home

cleanup() {
  [ -z "$SESSION_PID" ] || kill "$SESSION_PID" 2>/dev/null || true
  [ -z "$COBALT_PID" ] || kill "$COBALT_PID" 2>/dev/null || true
  [ -z "$XVFB_PID" ] || kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo '[acquisition] starting Xvfb...'
Xvfb :99 -ac -screen 0 1280x720x16 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 1
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  echo '[acquisition] Xvfb failed to start'
  cat /tmp/xvfb.log || true
  exit 1
fi

# Run the browser/session generator as an unprivileged user. This avoids the
# Chromium-as-root sandbox failure seen in Render containers.
chown -R ytuser:ytuser /tmp/yt-session /tmp/yt-home

SESSION_READY=0
SESSION_ATTEMPT=1
while [ "$SESSION_ATTEMPT" -le 5 ]; do
  echo "[acquisition] starting YouTube session generator (attempt $SESSION_ATTEMPT/5)..."
  rm -f /tmp/yt-session.log
  cd /opt/yt-session-generator
  su-exec ytuser env DISPLAY=:99 HOME=/tmp/yt-home XDG_CONFIG_HOME=/tmp/yt-home/.config \
    python3 potoken-generator.py \
      --bind 127.0.0.1 \
      --port 8080 \
      --chrome-path /usr/bin/chromium \
      >/tmp/yt-session.log 2>&1 &
  SESSION_PID=$!

  READY_WAIT=1
  while [ "$READY_WAIT" -le 90 ]; do
    if curl -fsS --max-time 3 http://127.0.0.1:8080/token >/dev/null 2>&1; then
      SESSION_READY=1
      break
    fi
    if ! kill -0 "$SESSION_PID" 2>/dev/null; then
      echo '[acquisition] session generator exited during startup'
      cat /tmp/yt-session.log || true
      break
    fi
    sleep 2
    READY_WAIT=$((READY_WAIT + 1))
  done

  if [ "$SESSION_READY" -eq 1 ]; then
    break
  fi

  kill "$SESSION_PID" 2>/dev/null || true
  wait "$SESSION_PID" 2>/dev/null || true
  SESSION_PID=''
  SESSION_ATTEMPT=$((SESSION_ATTEMPT + 1))
  sleep 2
done

if [ "$SESSION_READY" -ne 1 ]; then
  echo '[acquisition] session generator did not become ready after 5 attempts'
  cat /tmp/yt-session.log || true
  exit 1
fi

echo '[acquisition] YouTube session generator ready.'

echo '[acquisition] starting Cobalt...'
# Render exposes the public service URL and the web-service port at runtime.
# Cobalt needs the public URL so its /tunnel links are usable by ClipShortener.
if [ -n "${RENDER_EXTERNAL_URL:-}" ]; then
  export API_URL="${RENDER_EXTERNAL_URL%/}/"
fi
export API_PORT="${PORT:-${API_PORT:-9000}}"
export API_LISTEN_ADDRESS=0.0.0.0
cd /opt/cobalt
node src/cobalt >/tmp/cobalt.log 2>&1 &
COBALT_PID=$!

COBALT_READY=0
for i in $(seq 1 60); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${API_PORT}/" >/dev/null 2>&1; then
    COBALT_READY=1
    break
  fi
  if ! kill -0 "$COBALT_PID" 2>/dev/null; then
    echo '[acquisition] Cobalt exited during startup'
    cat /tmp/cobalt.log || true
    exit 1
  fi
  sleep 1
done

if [ "$COBALT_READY" -ne 1 ]; then
  echo '[acquisition] Cobalt did not become ready'
  cat /tmp/cobalt.log || true
  exit 1
fi

echo '[acquisition] Cobalt + YouTube session generator ready.'
wait "$COBALT_PID"
