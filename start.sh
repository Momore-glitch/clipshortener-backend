#!/bin/sh
set -eu

mkdir -p /tmp/clipshortener /tmp/yt-session

cleanup() {
    kill "${API_PID:-}" 2>/dev/null || true
    kill "${BGUTIL_PID:-}" 2>/dev/null || true
    kill "${COBALT_PID:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "[startup] ClipShortener Render runtime"

export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

if [ -n "${YOUTUBE_PROXY:-}" ]; then
    export HTTP_PROXY="$YOUTUBE_PROXY"
    export HTTPS_PROXY="$YOUTUBE_PROXY"
    echo "[startup] YouTube proxy: configured"
elif [ -n "${HTTPS_PROXY:-}" ]; then
    echo "[startup] HTTPS proxy: inherited from environment"
else
    echo "[startup] YouTube proxy: not configured (direct connection)"
fi

echo "[startup] starting bgutil PO-token provider 1.3.2..."
cd /opt/bgutil-ytdlp-pot-provider/server
node build/main.js >/tmp/bgutil.log 2>&1 &
BGUTIL_PID=$!

BGUTIL_READY=0
for i in $(seq 1 90); do
    if ! kill -0 "$BGUTIL_PID" 2>/dev/null; then
        echo "[startup] ERROR: bgutil provider exited"
        tail -n 120 /tmp/bgutil.log || true
        break
    fi
    code="$(curl -4 -sS -o /dev/null -w '%{http_code}' --noproxy '*' --connect-timeout 2 --max-time 5 http://127.0.0.1:4416/ping || true)"
    if [ "$code" = "200" ]; then
        BGUTIL_READY=1
        break
    fi
    sleep 1
done

if [ "$BGUTIL_READY" -eq 1 ]; then
    echo "[startup] bgutil PO-token provider is ready"
else
    echo "[startup] WARNING: bgutil provider is not ready"
    tail -n 120 /tmp/bgutil.log || true
fi

echo "[startup] starting Cobalt 11.7.1..."
cd /app
node src/cobalt >/tmp/cobalt.log 2>&1 &
COBALT_PID=$!

COBALT_READY=0
for i in $(seq 1 90); do
    if ! kill -0 "$COBALT_PID" 2>/dev/null; then
        echo "[startup] ERROR: Cobalt exited"
        tail -n 120 /tmp/cobalt.log || true
        break
    fi
    code="$(curl -4 -sS -o /dev/null -w '%{http_code}' --noproxy '*' --connect-timeout 2 --max-time 5 http://127.0.0.1:9000/ || true)"
    if [ "$code" != "000" ]; then
        COBALT_READY=1
        break
    fi
    sleep 1
done

if [ "$COBALT_READY" -eq 1 ]; then
    echo "[startup] Cobalt is ready"
else
    echo "[startup] WARNING: Cobalt did not become ready"
    tail -n 120 /tmp/cobalt.log || true
fi

echo "[startup] ===== Network diagnostics ====="

echo "[startup] Testing Google HTTPS..."
if curl -4 -sS -I --connect-timeout 10 --max-time 20 https://www.google.com/ >/tmp/google-test.log 2>&1; then
    echo "[startup] Google HTTPS: OK"
else
    echo "[startup] Google HTTPS: FAILED"
fi

echo "[startup] Testing YouTube IPv4 DNS..."
YOUTUBE_IPV4="$(getent ahostsv4 www.youtube.com 2>/dev/null | awk 'NR==1 {print $1}' || true)"
if [ -n "$YOUTUBE_IPV4" ]; then
    echo "[startup] YouTube IPv4: $YOUTUBE_IPV4"
else
    echo "[startup] WARNING: no YouTube IPv4 address returned"
fi

if [ -n "${YOUTUBE_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ]; then
    echo "[startup] Testing YouTube through configured proxy..."
    if curl -4 -sS -I -L --connect-timeout 10 --max-time 25 https://www.youtube.com/ >/tmp/youtube-proxy-test.log 2>&1; then
        echo "[startup] YouTube through proxy: OK"
    else
        echo "[startup] YouTube through proxy: FAILED"
    fi
    tail -n 25 /tmp/youtube-proxy-test.log 2>/dev/null || true
else
    echo "[startup] Testing direct YouTube HTTPS..."
    if curl -4 -sS -I -L --connect-timeout 10 --max-time 20 https://www.youtube.com/ >/tmp/youtube-test.log 2>&1; then
        echo "[startup] YouTube HTTPS: OK"
    else
        echo "[startup] YouTube HTTPS: FAILED"
    fi
    tail -n 25 /tmp/youtube-test.log 2>/dev/null || true
fi

echo "[startup] ===== End network diagnostics ====="
echo "[startup] starting ClipShortener API..."

PORT="${PORT:-10000}"
echo "[startup] API port: ${PORT}"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
