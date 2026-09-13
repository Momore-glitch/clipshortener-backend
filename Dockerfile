# ClipShortener Render backend — speed + accurate timing + captions
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLIPSHORTENER_WORKERS=1 \
    CLIPSHORTENER_FFMPEG_THREADS=1 \
    CLIPSHORTENER_UPLOAD_CHUNK_MB=64 \
    CLIPSHORTENER_WHISPER_MODEL=tiny \
    HF_HOME=/opt/huggingface \
    TRANSFORMERS_CACHE=/opt/huggingface \
    HF_HUB_DISABLE_XET=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefer-binary -r requirements.txt

# Bake the tiny caption model into the image so the first user never waits for
# a model download and caption generation does not depend on runtime model fetches.
RUN mkdir -p /opt/huggingface && python -c 'from faster_whisper import WhisperModel; WhisperModel("tiny", device="cpu", compute_type="int8", download_root="/opt/huggingface")'

COPY app.py .

EXPOSE 10000
CMD ["sh", "-c", "exec python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
