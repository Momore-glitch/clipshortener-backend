from __future__ import annotations

import asyncio
import json

import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field


try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

VERSION = "21.0.0-upload-only"
DATA_ROOT = Path(os.getenv("CLIPSHORTENER_DATA", "/tmp/clipshortener"))
JOBS_ROOT = DATA_ROOT / "jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)
MAX_VIDEO_SIZE = int(os.getenv("CLIPSHORTENER_MAX_VIDEO_GB", "10")) * 1024 * 1024 * 1024
UPLOAD_CHUNK_SIZE = int(os.getenv("CLIPSHORTENER_UPLOAD_CHUNK_MB", "64")) * 1024 * 1024
MAX_BATCH_FILES = 8
MAX_CLIP_SECONDS = 900
JOB_TTL = 60 * 60 * 6
MAX_WORKERS = max(2, min(4, int(os.getenv("CLIPSHORTENER_WORKERS", "2"))))
RATE_LIMIT = 20
RATE_WINDOW = 600
request_times: dict[str, list[float]] = {}
jobs: dict[str, dict[str, Any]] = {}
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
whisper_model = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("clipshortener")

app = FastAPI(title="ClipShortener API", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
if not FFMPEG or not FFPROBE:
    raise RuntimeError("FFmpeg and FFprobe are required.")

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg", ".3gp", ".ts"}


def now() -> float:
    return time.time()


def safe_filename(name: str) -> str:
    clean = Path(name or "video.mp4").name
    return clean if clean not in {"", ".", ".."} else "video.mp4"


def rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    t = now()
    values = [x for x in request_times.get(ip, []) if t - x < RATE_WINDOW]
    if len(values) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests. Please try again later.")
    values.append(t)
    request_times[ip] = values


def update_job(job_id: str, **updates: Any) -> None:
    job = jobs.setdefault(job_id, {})
    job.update(updates)
    job["updated_at"] = now()


def cleanup_old_jobs() -> None:
    cutoff = now() - JOB_TTL
    for path in list(JOBS_ROOT.iterdir()):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    for key, value in list(jobs.items()):
        if value.get("updated_at", now()) < cutoff:
            jobs.pop(key, None)


def run_checked(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
    log.info("CMD %s", " ".join(map(str, cmd)))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Processing timed out. Try a shorter or smaller video.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "FFmpeg failed")[-5000:]
        log.error("COMMAND_FAILED %s", detail)
        raise RuntimeError("Video processing failed. The file may use an unsupported codec or container.")
    return result


def probe(path: Path) -> dict[str, Any]:
    result = run_checked([
        FFPROBE, "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ], timeout=60)
    data = json.loads(result.stdout or "{}")
    fmt = data.get("format", {})
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    duration = float(fmt.get("duration") or 0)
    size = int(fmt.get("size") or path.stat().st_size)
    if duration <= 0.05 or size <= 0:
        raise RuntimeError("The video could not be read.")
    return {
        "duration": duration,
        "size": size,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": video.get("codec_name") or "unknown",
    }


def parse_number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def validate_options(clip_length: Any, export_format: str, start: Any, end: Any) -> tuple[float, str, float, float | None]:
    length = parse_number(clip_length, 30)
    if not 1 <= length <= MAX_CLIP_SECONDS:
        raise HTTPException(400, "Clip length must be between 1 and 900 seconds.")
    export = export_format if export_format in {"original", "vertical", "square", "landscape"} else "original"
    start_value = max(0.0, parse_number(start, 0))
    end_num = parse_number(end, 0)
    end_value = None if end_num <= 0 else max(0.1, end_num)
    return length, export, start_value, end_value


def video_filter(export_format: str) -> str | None:
    if export_format == "vertical":
        return "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    if export_format == "square":
        return "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2"
    if export_format == "landscape":
        return "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
    return None


def make_thumbnail(video: Path, destination: Path) -> None:
    run_checked([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.5", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(destination),
    ], timeout=60)


def ass_escape(text: str) -> str:
    return (text or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", " ")


def caption_style(style: str, font: str, size: str, position: str, animation: str) -> dict[str, Any]:
    sizes = {"medium": 46, "large": 58, "xlarge": 70}
    return {
        "font": font if font in {"DejaVu Sans", "Liberation Sans", "DejaVu Sans Mono"} else "DejaVu Sans",
        "size": sizes.get(size, 58),
        "bold": 1 if style == "bold" else 0,
        "outline": 4 if style in {"classic", "bold", "boxed"} else 2,
        "shadow": 1 if style != "boxed" else 0,
        "back": "&H66000000" if style == "boxed" else "&H88000000",
        "align": {"bottom": 2, "middle": 5, "top": 8}.get(position, 2),
        "tags": r"\fad(180,180)" if animation == "fade" else (
            r"\fscx90\fscy90\t(0,160,\fscx100\fscy100)" if animation == "pop" else ""
        ),
    }


def caption_tracks(video: Path, language: str, style: str, font: str, size: str, animation: str, position: str, job_dir: Path) -> tuple[Path, Path]:
    global whisper_model
    if WhisperModel is None:
        raise RuntimeError("Automatic captions are unavailable in this deployment.")
    if whisper_model is None:
        log.info("Loading faster-whisper tiny model")
        whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    lang = None if language in {"", "auto", None} else language
    segments, _ = whisper_model.transcribe(str(video), beam_size=1, vad_filter=True, language=lang)
    ass = job_dir / "captions.ass"
    vtt = job_dir / "captions.vtt"
    settings = caption_style(style, font, size, position, animation)

    def ass_time(seconds: float) -> str:
        cs = int(round(max(0, seconds) * 100))
        h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def vtt_time(seconds: float) -> str:
        total = max(0, seconds)
        h = int(total // 3600); total -= h * 3600
        m = int(total // 60); total -= m * 60
        s = int(total); ms = int(round((total - s) * 1000))
        if ms >= 1000: s += 1; ms -= 1000
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    count = 0
    with ass.open("w", encoding="utf-8") as af, vtt.open("w", encoding="utf-8") as vfout:
        af.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n")
        af.write("[V4+ Styles]\n")
        af.write("Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n")
        af.write(
            f"Style,Default,{settings['font']},{settings['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,{settings['back']},"
            f"{settings['bold']},0,0,0,100,100,0,0,1,{settings['outline']},{settings['shadow']},{settings['align']},70,70,70,1\n\n"
        )
        af.write("[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
        vfout.write("WEBVTT\n\n")
        for seg in segments:
            text = " ".join((seg.text or "").strip().split())
            if not text:
                continue
            af.write(f"Dialogue: 0,{ass_time(seg.start)},{ass_time(seg.end)},Default,,0,0,0,,{{{settings['tags']}}}{ass_escape(text)}\n")
            vfout.write(f"{vtt_time(seg.start)} --> {vtt_time(seg.end)}\n{text}\n\n")
            count += 1
    if count == 0:
        ass.unlink(missing_ok=True); vtt.unlink(missing_ok=True)
        raise RuntimeError("No speech was detected for automatic captions.")
    return ass, vtt


def burn_captions(video: Path, ass_path: Path, output: Path) -> None:
    escaped = str(ass_path).replace("\\", "/").replace(":", r"\:")
    run_checked([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-vf", f"subtitles='{escaped}'", "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
    ], timeout=1800)


def create_clips(job_id: str, source: Path, clip_length: float, export_format: str, start: float, end: float | None, captions: dict[str, Any] | None) -> list[Path]:
    info = probe(source)
    duration = info["duration"]
    start = min(max(0.0, start), max(0.0, duration - 0.05))
    end_value = duration if end is None else min(max(start + 0.05, end), duration)
    total_duration = max(0.05, end_value - start)
    expected = max(1, math.ceil((total_duration - 1e-9) / clip_length))
    job_dir = JOBS_ROOT / job_id
    clip_dir = job_dir / "clips"
    clip_dir.mkdir(exist_ok=True)
    pattern = clip_dir / "clip_%03d.mp4"
    for old in clip_dir.glob("clip_*.mp4"): old.unlink(missing_ok=True)

    fast_copy = export_format == "original" and start <= 0.001 and abs(end_value - duration) <= 0.2 and not captions
    if fast_copy:
        run_checked([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "segment", "-segment_time", str(clip_length),
            "-reset_timestamps", "1", str(pattern),
        ], timeout=max(600, int(duration * 2)))
        copy_clips = sorted(p for p in clip_dir.glob("clip_*.mp4") if p.is_file() and p.stat().st_size > 0)
        if len(copy_clips) < expected:
            for old in copy_clips:
                old.unlink(missing_ok=True)
            copy_clips = []
    else:
        copy_clips = []

    if not copy_clips:
        filters = video_filter(export_format)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{total_duration:.3f}",
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-force_key_frames", f"expr:gte(t,n_forced*{clip_length})", "-c:a", "aac", "-b:a", "128k", "-f", "segment",
            "-segment_time", str(clip_length), "-reset_timestamps", "1",
        ]
        if filters: cmd += ["-vf", filters]
        cmd += [str(pattern)]
        run_checked(cmd, timeout=max(900, int(total_duration * 4)))

    clips = sorted(p for p in clip_dir.glob("clip_*.mp4") if p.is_file() and p.stat().st_size > 0)
    if not clips:
        raise RuntimeError("No clips were produced.")
    if len(clips) > expected + 1:
        clips = clips[: expected + 1]

    if captions and captions.get("enabled"):
        for index, clip in enumerate(clips, 1):
            update_job(job_id, progress=int(40 + 45 * index / max(1, len(clips))), message=f"Captions: clip {index} of {len(clips)}")
            ass, vtt = caption_tracks(
                clip,
                captions.get("language", "auto"),
                captions.get("style", "classic"),
                captions.get("font", "DejaVu Sans"),
                captions.get("size", "large"),
                captions.get("animation", "none"),
                captions.get("position", "bottom"),
                job_dir,
            )
            temp = clip.with_name(clip.stem + "_cap.mp4")
            burn_captions(clip, ass, temp)
            temp.replace(clip)
            # Keep a per-job VTT sidecar copy; one VTT per clip for downloads.
            vtt.rename(job_dir / f"{clip.stem}.vtt")
            ass.unlink(missing_ok=True)
    return clips


def build_zip(job_id: str, clips: list[Path]) -> Path:
    zpath = JOBS_ROOT / job_id / f"clipshortener-{job_id[:8]}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for clip in clips: z.write(clip, clip.name)
    return zpath


def render_result(job_id: str, source_info: dict[str, Any], source_name: str, clips: list[Path], thumb: Path, zip_path: Path) -> dict[str, Any]:
    items = []
    for clip in clips:
        info = probe(clip)
        vtt_name = f"{clip.stem}.vtt"
        items.append({
            "name": clip.name,
            "duration": round(info["duration"], 2),
            "size": info["size"],
            "url": f"/api/jobs/{job_id}/download/{clip.name}",
            "thumbnail": f"/api/jobs/{job_id}/thumbnail/{clip.name}",
            "vtt": f"/api/jobs/{job_id}/captions/{vtt_name}",
            "has_vtt": (JOBS_ROOT / job_id / vtt_name).is_file(),
        })
    return {
        "success": True,
        "job_id": job_id,
        "source_name": source_name,
        "source_duration": round(source_info["duration"], 2),
        "source_size": source_info["size"],
        "clip_count": len(items),
        "clips": items,
        "zip": f"/api/jobs/{job_id}/zip",
        "thumbnail": f"/api/jobs/{job_id}/source-thumb",
    }


def complete_job(job_id: str, source: Path, source_name: str, clip_length: float, export_format: str, start: float, end: float | None, captions: dict[str, Any] | None) -> None:
    try:
        update_job(job_id, status="processing", progress=10, message="Reading your video...")
        info = probe(source)
        update_job(job_id, progress=20, message="Creating clips...")
        clips = create_clips(job_id, source, clip_length, export_format, start, end, captions)
        update_job(job_id, progress=90, message="Generating previews...")
        job_dir = JOBS_ROOT / job_id
        thumb = job_dir / "source-thumb.jpg"
        make_thumbnail(source, thumb)
        zip_path = build_zip(job_id, clips)
        result = render_result(job_id, info, source_name, clips, thumb, zip_path)
        update_job(job_id, status="complete", progress=100, message="Your clips are ready.", result=result)
    except Exception as exc:
        log.exception("JOB_FAILED id=%s", job_id)
        update_job(job_id, status="error", progress=0, message=str(exc)[:600])
    finally:
        source.unlink(missing_ok=True)


def run_job_from_upload(job_id: str, source: Path, source_name: str, clip_length: float, export_format: str, start: float, end: float | None, captions: dict[str, Any] | None) -> None:
    complete_job(job_id, source, source_name, clip_length, export_format, start, end, captions)


def captions_from_form(enabled: bool, language: str, style: str, font: str, size: str, animation: str, position: str) -> dict[str, Any] | None:
    if not enabled: return None
    return {"enabled": True, "language": language, "style": style, "font": font, "size": size, "animation": animation, "position": position}


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse("ClipShortener API is running. Use /health for diagnostics.")


def runtime_diagnostics() -> dict[str, Any]:
    return {
        "processing": "local FFmpeg clip engine",
        "input_mode": "uploaded video files only",
        "max_video_size": MAX_VIDEO_SIZE,
        "max_batch_files": MAX_BATCH_FILES,
        "max_clip_seconds": MAX_CLIP_SECONDS,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "ffmpeg": bool(FFMPEG),
        "ffprobe": bool(FFPROBE),
        "whisper": bool(WhisperModel),
        "workers": MAX_WORKERS,
        **runtime_diagnostics(),
    }


@app.post("/api/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    clip_length: str = Form("30"),
    export_format: str = Form("original"),
    start: str = Form("0"),
    end: str = Form("0"),
    captions: bool = Form(False),
    caption_language: str = Form("auto"),
    caption_style: str = Form("classic"),
    caption_font: str = Form("DejaVu Sans"),
    caption_size: str = Form("large"),
    caption_animation: str = Form("none"),
    caption_position: str = Form("bottom"),
) -> dict[str, Any]:
    rate_limit(request); cleanup_old_jobs()
    length, export, start_v, end_v = validate_options(clip_length, export_format, start, end)
    filename = safe_filename(file.filename or "video.mp4")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS and not (file.content_type or "").startswith("video/"):
        raise HTTPException(400, "Unsupported video format.")
    if suffix not in SUPPORTED_EXTENSIONS: suffix = ".mp4"
    job_id = uuid.uuid4().hex
    job_dir = JOBS_ROOT / job_id; job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / f"input{suffix}"
    total = 0
    with source.open("wb") as target:
        while True:
            chunk = await file.read(4 * 1024 * 1024)
            if not chunk: break
            total += len(chunk)
            if total > MAX_VIDEO_SIZE:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(413, "Video exceeds the configured upload limit.")
            target.write(chunk)
    await file.close()
    if total == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "The selected video is empty.")
    captions_cfg = captions_from_form(captions, caption_language, caption_style, caption_font, caption_size, caption_animation, caption_position)
    update_job(job_id, status="queued", progress=0, message="Queued.")
    executor.submit(run_job_from_upload, job_id, source, filename, length, export, start_v, end_v, captions_cfg)
    return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


@app.post("/api/upload-chunk")
async def upload_chunk(request: Request) -> dict[str, Any]:
    """Receive an idempotent, resumable upload chunk."""
    job_id = (request.headers.get("X-Upload-Job") or "").strip()
    is_first = not bool(job_id)
    if is_first:
        rate_limit(request); cleanup_old_jobs(); job_id = uuid.uuid4().hex
    try:
        total_size = int(request.headers.get("X-Upload-Total", "0"))
        index = int(request.headers.get("X-Upload-Index", "-1"))
        total_chunks = int(request.headers.get("X-Upload-Chunks", "0"))
    except ValueError as exc:
        raise HTTPException(400, "Invalid upload chunk metadata.") from exc
    if total_size <= 0 or total_size > MAX_VIDEO_SIZE:
        raise HTTPException(413, "Video exceeds the configured upload limit or has an invalid size.")
    if index < 0 or total_chunks < 1 or index >= total_chunks:
        raise HTTPException(400, "Invalid upload chunk index.")
    if total_chunks > math.ceil(MAX_VIDEO_SIZE / UPLOAD_CHUNK_SIZE):
        raise HTTPException(400, "Too many upload chunks.")
    filename = safe_filename(request.headers.get("X-Upload-Name") or "video.mp4")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, "Unsupported video format.")
    job_dir = JOBS_ROOT / job_id; part = job_dir / "upload.part"; meta_path = job_dir / "upload.json"
    job_dir.mkdir(parents=True, exist_ok=True)
    if is_first:
        metadata = {
            "filename": filename, "total_size": total_size, "total_chunks": total_chunks, "chunk_size": UPLOAD_CHUNK_SIZE, "received": [],
            "clip_length": request.headers.get("X-Upload-Clip-Length", "30"), "export_format": request.headers.get("X-Upload-Export", "original"),
            "start": request.headers.get("X-Upload-Start", "0"), "end": request.headers.get("X-Upload-End", "0"),
            "captions": request.headers.get("X-Upload-Captions", "0") == "1",
            "caption_language": request.headers.get("X-Upload-Caption-Language", "auto"), "caption_style": request.headers.get("X-Upload-Caption-Style", "classic"),
            "caption_font": request.headers.get("X-Upload-Caption-Font", "DejaVu Sans"), "caption_size": request.headers.get("X-Upload-Caption-Size", "large"),
            "caption_animation": request.headers.get("X-Upload-Caption-Animation", "none"), "caption_position": request.headers.get("X-Upload-Caption-Position", "bottom"),
        }
        validate_options(metadata["clip_length"], metadata["export_format"], metadata["start"], metadata["end"])
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif not meta_path.is_file():
        existing = jobs.get(job_id)
        if existing and existing.get("status") in {"queued", "processing", "complete"}:
            return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}", "complete": True, "retry": True}
        raise HTTPException(400, "Upload session expired or is invalid.")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(400, "Upload session metadata is invalid.") from exc
    if metadata.get("total_size") != total_size or metadata.get("total_chunks") != total_chunks or metadata.get("filename") != filename:
        raise HTTPException(400, "Upload chunk metadata does not match the original upload.")
    offset = index * UPLOAD_CHUNK_SIZE; expected_chunk = min(UPLOAD_CHUNK_SIZE, total_size - offset)
    if offset >= total_size or expected_chunk <= 0:
        raise HTTPException(400, "Upload chunk offset is invalid.")
    received = {int(x) for x in metadata.get("received", [])}
    if index in received:
        return {"job_id": job_id, "index": index, "complete": len(received) == total_chunks, "received": len(received), "total_chunks": total_chunks}
    written = 0
    try:
        with part.open("r+b" if part.exists() else "w+b") as target:
            target.seek(offset)
            async for data in request.stream():
                if not data: continue
                written += len(data)
                if written > expected_chunk: raise HTTPException(413, "Upload chunk is too large.")
                target.write(data)
            target.flush(); os.fsync(target.fileno())
    except HTTPException: raise
    except Exception as exc: raise HTTPException(500, "Failed to store upload chunk.") from exc
    if written != expected_chunk:
        raise HTTPException(400, f"Incomplete upload chunk: received {written} bytes, expected {expected_chunk}.")
    received.add(index); metadata["received"] = sorted(received); meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    if len(received) != total_chunks:
        return {"job_id": job_id, "index": index, "complete": False, "received": len(received), "total_chunks": total_chunks}
    if part.stat().st_size != total_size:
        raise HTTPException(400, "Upload is incomplete or corrupted.")
    try:
        length, export, start_v, end_v = validate_options(metadata["clip_length"], metadata["export_format"], metadata["start"], metadata["end"])
        captions_cfg = captions_from_form(metadata["captions"], metadata["caption_language"], metadata["caption_style"], metadata["caption_font"], metadata["caption_size"], metadata["caption_animation"], metadata["caption_position"])
        source = job_dir / f"input{suffix}"; part.replace(source); meta_path.unlink(missing_ok=True)
        update_job(job_id, status="queued", progress=0, message="Upload complete. Queued.")
        executor.submit(run_job_from_upload, job_id, source, filename, length, export, start_v, end_v, captions_cfg)
        return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}", "complete": True, "received": total_chunks}
    except HTTPException: raise
    except Exception:
        log.exception("CHUNK_UPLOAD_FINALIZE_FAILED id=%s", job_id); shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, "The upload could not be finalized.")


@app.get("/api/upload-session/{job_id}")
def upload_session(job_id: str) -> dict[str, Any]:
    """Return resumable upload state for a previously started upload."""
    meta_path = JOBS_ROOT / job_id / "upload.json"
    if not meta_path.is_file():
        existing = jobs.get(job_id)
        if existing and existing.get("status") in {"queued", "processing", "complete"}:
            return {"job_id": job_id, "complete": True, "received": [], "total_chunks": 0}
        raise HTTPException(404, "Upload session not found or expired.")
    try: metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc: raise HTTPException(500, "Upload session metadata is unreadable.") from exc
    return {"job_id": job_id, "complete": False, "received": metadata.get("received", []), "total_chunks": metadata.get("total_chunks", 0), "total_size": metadata.get("total_size", 0), "chunk_size": metadata.get("chunk_size", UPLOAD_CHUNK_SIZE), "filename": metadata.get("filename", "")}


@app.post("/api/batch")
async def batch(
    request: Request,
    files: list[UploadFile] = File(...),
    clip_length: str = Form("30"),
    export_format: str = Form("original"),
    captions: bool = Form(False),
    caption_language: str = Form("auto"),
    caption_style: str = Form("classic"),
    caption_font: str = Form("DejaVu Sans"),
    caption_size: str = Form("large"),
    caption_animation: str = Form("none"),
    caption_position: str = Form("bottom"),
) -> dict[str, Any]:
    rate_limit(request); cleanup_old_jobs()
    if not 1 <= len(files) <= MAX_BATCH_FILES:
        raise HTTPException(400, f"Batch size must be between 1 and {MAX_BATCH_FILES} videos.")
    results = []
    for file in files:
        try:
            result = await upload(request, file, clip_length, export_format, "0", "0", captions, caption_language, caption_style, caption_font, caption_size, caption_animation, caption_position)
            results.append(result)
        except HTTPException as exc:
            results.append({"success": False, "filename": file.filename, "error": exc.detail})
    return {"success": True, "jobs": results}


@app.post("/api/detect")
async def detect(request: Request, file: UploadFile = File(...), clip_length: str = Form("30")) -> dict[str, Any]:
    rate_limit(request); cleanup_old_jobs()
    length, _, _, _ = validate_options(clip_length, "original", "0", "0")
    filename = safe_filename(file.filename or "video.mp4")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        suffix = ".mp4"
    temp_id = uuid.uuid4().hex
    job_dir = JOBS_ROOT / temp_id; job_dir.mkdir()
    src = job_dir / f"input{suffix}"
    try:
        with src.open("wb") as target:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk: break
                target.write(chunk)
                if target.stat().st_size > MAX_VIDEO_SIZE: raise HTTPException(413, "Video exceeds the configured upload limit.")
        info = probe(src)
        # Fast scene scan on a low-FPS stream; candidate windows are later turned into real clips.
        result = run_checked([
            FFMPEG, "-hide_banner", "-i", str(src), "-vf", "fps=2,select='gt(scene,0.35)',showinfo", "-f", "null", "-",
        ], timeout=300)
        times = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", result.stderr or "")]
        candidates = []
        half = length / 2
        for t in times[:30]:
            start_v = max(0.0, min(t - half, max(0.0, info["duration"] - length)))
            candidates.append({"start": round(start_v, 2), "end": round(min(info["duration"], start_v + length), 2), "score": 100})
        # De-duplicate close candidates.
        unique = []
        for c in candidates:
            if not unique or abs(c["start"] - unique[-1]["start"]) >= length * 0.4:
                unique.append(c)
        return {"success": True, "candidates": unique[:10]}
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job: raise HTTPException(404, "Job not found or expired.")
    return job


@app.get("/api/jobs/{job_id}/source-thumb")
def source_thumb(job_id: str) -> FileResponse:
    path = JOBS_ROOT / job_id / "source-thumb.jpg"
    if not path.is_file(): raise HTTPException(404, "Thumbnail not ready.")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/thumbnail/{name}")
def clip_thumb(job_id: str, name: str) -> FileResponse:
    if "/" in name or "\\" in name: raise HTTPException(400, "Invalid filename.")
    clip = JOBS_ROOT / job_id / "clips" / name
    thumb = clip.with_suffix(".jpg")
    if not thumb.is_file():
        make_thumbnail(clip, thumb)
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/download/{name}")
def download(job_id: str, name: str) -> FileResponse:
    if "/" in name or "\\" in name: raise HTTPException(400, "Invalid filename.")
    path = JOBS_ROOT / job_id / "clips" / name
    if not path.is_file(): raise HTTPException(404, "Clip not found.")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/captions/{name}")
def download_caption(job_id: str, name: str) -> FileResponse:
    if "/" in name or "\\" in name: raise HTTPException(400, "Invalid filename.")
    path = JOBS_ROOT / job_id / name
    if not path.is_file(): raise HTTPException(404, "Caption sidecar not found.")
    return FileResponse(path, media_type="text/vtt", filename=path.name)


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str) -> FileResponse:
    zpath = next((p for p in (JOBS_ROOT / job_id).glob("*.zip") if p.is_file()), None)
    if not zpath: raise HTTPException(404, "ZIP not ready.")
    return FileResponse(zpath, media_type="application/zip", filename=zpath.name)


@app.post("/frontend-error")
async def frontend_error(payload: dict[str, Any]):
    log.error("FRONTEND_ERROR %s", payload)
    return {"ok": True}
