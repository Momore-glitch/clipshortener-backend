#!/bin/sh
set -eu

# Initialise PIDs before the EXIT trap so set -u cannot abort cleanup
SESSION_PID=''
COBALT_PID=''
XVFB_PID=''

mkdir -p /tmp/yt-session

echo '[acquisition] starting Xvfb...'
Xvfb :99 -ac -screen 0 1280x720x16 -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

cleanup() {
  kill "$SESSION_PID" "$COBALT_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo '[acquisition] starting YouTube session generator...'
cd /opt/yt-session-generator
DISPLAY=:99 python3 potoken-generator.py \
  --bind 127.0.0.1 \
  --port 8080 \
  --chrome-path /usr/bin/chromium \
  >/tmp/yt-session.log 2>&1 &
SESSION_PID=$!

SESSION_READY=0
for i in $(seq 1 180); do
  if curl -fsS http://127.0.0.1:8080/token >/dev/null 2>&1; then
    SESSION_READY=1
    break
  fi
  if ! kill -0 "$SESSION_PID" 2>/dev/null; then
    echo '[acquisition] session generator exited during startup'
    cat /tmp/yt-session.log || true
    exit 1
  fi
  sleep 2
done

if [ "$SESSION_READY" -ne 1 ]; then
  echo '[acquisition] session generator did not become ready'
  cat /tmp/yt-session.log || true
  exit 1
fi

echo '[acquisition] YouTube session generator ready.'

echo '[acquisition] starting Cobalt...'
cd /opt/cobalt
node src/cobalt >/tmp/cobalt.log 2>&1 &
COBALT_PID=$!

COBALT_READY=0
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:9000/ >/dev/null 2>&1; then
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
