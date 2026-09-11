from __future__ import annotations

import asyncio
import json
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
from urllib.request import Request as URLRequest, urlopen
from urllib.error import HTTPError, URLError

import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field


try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

VERSION = "28.4.0-batch-status-final"
DATA_ROOT = Path(os.getenv("CLIPSHORTENER_DATA", "/tmp/clipshortener"))
JOBS_ROOT = DATA_ROOT / "jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)
MAX_VIDEO_SIZE = int(os.getenv("CLIPSHORTENER_MAX_VIDEO_GB", "10")) * 1024 * 1024 * 1024
UPLOAD_CHUNK_SIZE = int(os.getenv("CLIPSHORTENER_UPLOAD_CHUNK_MB", "64")) * 1024 * 1024
MAX_BATCH_FILES = 8
MAX_CLIP_SECONDS = 900
JOB_TTL = 60 * 60 * 6
MAX_WORKERS = max(1, min(4, int(os.getenv("CLIPSHORTENER_WORKERS", "1"))))
RATE_LIMIT = 20
RATE_WINDOW = 600
request_times: dict[str, list[float]] = {}
jobs: dict[str, dict[str, Any]] = {}
job_state_lock = threading.RLock()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
upload_locks: dict[str, asyncio.Lock] = {}
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
PLATFORM_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "instagram.com", "www.instagram.com", "facebook.com", "www.facebook.com", "fb.watch",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "reddit.com", "www.reddit.com", "old.reddit.com",
    "pinterest.com", "www.pinterest.com", "pin.it",
    "snapchat.com", "www.snapchat.com", "vimeo.com", "www.vimeo.com",
    "soundcloud.com", "www.soundcloud.com", "vk.com", "www.vk.com",
}
MAX_URL_REDIRECTS = 5
URL_READ_CHUNK = 4 * 1024 * 1024
ACQUISITION_TIMEOUT = int(os.getenv("CLIPSHORTENER_ACQUISITION_TIMEOUT", "180"))
ACQUISITION_API_URL = os.getenv("ACQUISITION_API_URL", "").strip().rstrip("/")
FFMPEG_THREADS = max(1, min(4, int(os.getenv("CLIPSHORTENER_FFMPEG_THREADS", "1"))))

def _host_is_platform(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == item or host.endswith("." + item) for item in PLATFORM_HOSTS)

def _host_is_public(host: str) -> bool:
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True

def _validate_source_url(value: str) -> tuple[str, bool]:
    value = (value or "").strip()
    if not value:
        raise HTTPException(400, "Video URL is required.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Enter a valid HTTP or HTTPS video URL.")
    if _host_is_platform(parsed.hostname):
        return value, True
    if not _host_is_public(parsed.hostname):
        raise HTTPException(400, "The video URL must point to a public internet host.")
    return value, False


def _validate_public_url(value: str) -> str:
    value, is_platform = _validate_source_url(value)
    if is_platform:
        raise HTTPException(400, "Platform acquisition is handled separately. Use a configured supported platform link.")
    return value


def _acquisition_request(url: str) -> dict[str, Any]:
    """Ask the separate acquisition service to fetch a direct public media URL."""
    if not ACQUISITION_API_URL:
        raise RuntimeError(
            "URL acquisition is not configured. Set ACQUISITION_API_URL on the processing server."
        )
    endpoint = ACQUISITION_API_URL + "/acquire"
    payload = json.dumps({"url": url}).encode("utf-8")
    request = URLRequest(
        endpoint,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ClipShortener/27.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=ACQUISITION_TIMEOUT) as response:
            raw = response.read(1024 * 1024)
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        try:
            raw = exc.read(1024 * 1024)
            detail = raw.decode("utf-8", "replace")
        finally:
            exc.close()
        raise RuntimeError(
            f"Acquisition service returned HTTP {exc.code}: {detail[:300]}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Could not reach the URL acquisition service.") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Acquisition service returned invalid JSON (HTTP {status})."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError("Acquisition service returned an invalid response.")
    if not data.get("download_url"):
        raise RuntimeError(data.get("error") or "Acquisition service returned no media file.")
    return data


def _acquire_via_service(job_id: str, url: str) -> tuple[Path, str]:
    parsed = urlparse(url)
    if _host_is_platform(parsed.hostname or ""):
        raise RuntimeError(
            "This link is a platform page. ClipShortener currently accepts direct public video-file URLs here."
        )

    result = _acquisition_request(url)
    download_url = str(result["download_url"])
    if download_url.startswith("/"):
        download_url = urljoin(ACQUISITION_API_URL + "/", download_url.lstrip("/"))
    filename = safe_filename(str(result.get("filename") or "imported-video.mp4"))

    download_parsed = urlparse(download_url)
    if download_parsed.scheme not in {"http", "https"} or not download_parsed.hostname:
        raise RuntimeError("The acquisition service returned an invalid download URL.")
    if not _host_is_public(download_parsed.hostname):
        raise RuntimeError("The acquisition service returned an unsafe download URL.")

    dest = JOBS_ROOT / job_id
    dest.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        suffix = ".mp4"
        filename = Path(filename).stem + suffix
    source = dest / f"input{suffix}"

    req = URLRequest(
        download_url,
        headers={
            "User-Agent": "ClipShortener/27.0",
            "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.1",
        },
    )
    try:
        with urlopen(req, timeout=ACQUISITION_TIMEOUT) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_VIDEO_SIZE:
                        raise RuntimeError("The acquired video exceeds the configured upload limit.")
                except ValueError:
                    pass
            total = 0
            with source.open("wb") as target:
                while True:
                    chunk = response.read(URL_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_SIZE:
                        source.unlink(missing_ok=True)
                        raise RuntimeError("The acquired video exceeds the configured upload limit.")
                    target.write(chunk)
    except HTTPError as exc:
        source.unlink(missing_ok=True)
        raise RuntimeError(f"The acquisition download returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        source.unlink(missing_ok=True)
        raise RuntimeError("Could not download the acquired video.") from exc

    if total <= 0:
        source.unlink(missing_ok=True)
        raise RuntimeError("The acquisition service returned an empty video file.")

    log.info(
        "DIRECT_URL_IMPORT_SUCCESS job=%s service=%s size=%s filename=%s",
        job_id,
        urlparse(ACQUISITION_API_URL).hostname,
        total,
        filename,
    )
    return source, filename


def _acquire_video(job_id: str, url: str) -> tuple[Path, str]:
    value, is_platform = _validate_source_url(url)
    if is_platform:
        raise RuntimeError(
            "Platform-page acquisition is not enabled in this build. "
            "Use a direct public video-file URL."
        )
    if ACQUISITION_API_URL:
        return _acquire_via_service(job_id, value)
    return _acquire_public_video(job_id, value)


def _acquire_public_video(job_id: str, url: str) -> tuple[Path, str]:
    current = _validate_public_url(url)
    dest = JOBS_ROOT / job_id
    dest.mkdir(parents=True, exist_ok=True)
    for _ in range(MAX_URL_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _host_is_public(parsed.hostname):
            raise RuntimeError("The video URL or redirect target is not a public HTTP(S) host.")
        req = URLRequest(current, headers={"User-Agent": "ClipShortener/27.0", "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.1"})
        try:
            response = urlopen(req, timeout=30)
        except HTTPError as exc:
            if exc.code in {301,302,303,307,308}:
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise RuntimeError("The video server returned an invalid redirect.")
                current = urljoin(current, location)
                continue
            raise RuntimeError(f"The video server returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Could not connect to the video URL.") from exc
        break
    else:
        raise RuntimeError("Too many redirects while opening the video URL.")

    with response:
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_VIDEO_SIZE:
                    raise RuntimeError("The video exceeds the configured upload limit.")
            except ValueError:
                pass
        suffix = Path(urlparse(current).path).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            suffix = ".mp4" if (content_type.startswith("video/") or "octet-stream" in content_type) else ".mp4"
        source = dest / f"input{suffix}"
        total = 0
        with source.open("wb") as target:
            while True:
                chunk = response.read(URL_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_SIZE:
                    source.unlink(missing_ok=True)
                    raise RuntimeError("The video exceeds the configured upload limit.")
                target.write(chunk)
        if total <= 0:
            source.unlink(missing_ok=True)
            raise RuntimeError("The video URL returned an empty file.")
    name = safe_filename(Path(urlparse(current).path).name or "imported-video.mp4")
    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        name = Path(name).stem + suffix
    log.info("PUBLIC_URL_IMPORT_SUCCESS job=%s size=%s filename=%s", job_id, total, name)
    return source, name


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


def _job_state_path(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "job.json"


def _persist_job(job_id: str, job: dict[str, Any]) -> None:
    path = _job_state_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def update_job(job_id: str, **updates: Any) -> None:
    with job_state_lock:
        job = jobs.setdefault(job_id, {})
        job.update(updates)
        job["updated_at"] = now()
        _persist_job(job_id, job)


def load_persisted_jobs() -> None:
    with job_state_lock:
        for path in JOBS_ROOT.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job_id = path.parent.name
                if re.fullmatch(r"[0-9a-fA-F]{32}", job_id) and isinstance(data, dict):
                    jobs[job_id] = data
            except Exception:
                log.warning("Could not restore job state from %s", path)


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
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.1", "-i", str(video),
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", str(FFMPEG_THREADS), "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
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
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", str(FFMPEG_THREADS),
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
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as z:
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
        update_job(job_id, progress=85, message=f"Clips created. Finalising {len(clips)} clips...")
        update_job(job_id, progress=90, message="Generating previews...")
        job_dir = JOBS_ROOT / job_id
        thumb = job_dir / "source-thumb.jpg"
        make_thumbnail(source, thumb)
        # Generate clip thumbnails before the job is marked complete. This
        # removes the on-demand thumbnail race that could leave broken
        # previews on a freshly rendered result.
        for index, clip in enumerate(clips, 1):
            clip_thumb = clip.with_suffix(".jpg")
            make_thumbnail(clip, clip_thumb)
            update_job(
                job_id,
                progress=90 + int(5 * index / max(1, len(clips))),
                message=f"Generating preview {index} of {len(clips)}...",
            )
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


def recover_jobs_after_restart() -> None:
    """Requeue jobs whose source file survived a process restart."""
    load_persisted_jobs()
    for job_id, job in list(jobs.items()):
        if job.get("status") not in {"queued", "processing"}:
            continue
        raw_source = job.get("source_path")
        source = Path(raw_source) if raw_source else next(iter((JOBS_ROOT / job_id).glob("input.*")), None)
        if not source or not source.is_file():
            update_job(job_id, status="error", progress=0, message="Processing was interrupted before the source could be recovered.")
            continue
        try:
            update_job(job_id, status="queued", progress=0, message="Recovered after server restart.")
            executor.submit(
                run_job_from_upload, job_id, source, job.get("source_name", source.name),
                float(job.get("clip_length", 30)), job.get("export_format", "original"),
                float(job.get("start", 0)), None if job.get("end") in (None, "", 0) else float(job.get("end")),
                job.get("captions"),
            )
        except Exception as exc:
            update_job(job_id, status="error", progress=0, message=f"Recovery failed: {exc}")


def captions_from_form(enabled: bool, language: str, style: str, font: str, size: str, animation: str, position: str) -> dict[str, Any] | None:
    if not enabled: return None
    return {"enabled": True, "language": language, "style": style, "font": font, "size": size, "animation": animation, "position": position}


recover_jobs_after_restart()


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse("ClipShortener API is running. Use /health for diagnostics.")


def runtime_diagnostics() -> dict[str, Any]:
    return {
        "processing": "local FFmpeg clip engine",
        "input_mode": "uploaded files + direct public video-file URLs",
        "url_acquisition": "configured" if ACQUISITION_API_URL else "local_fallback",
        "ffmpeg_threads": FFMPEG_THREADS,
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


@app.post("/api/import-url")
async def import_url(
    request: Request,
    url: str = Form(...),
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
    job_id = uuid.uuid4().hex
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    captions_cfg = captions_from_form(captions, caption_language, caption_style, caption_font, caption_size, caption_animation, caption_position)
    try:
        source, filename = await asyncio.to_thread(_acquire_video, job_id, url)
        update_job(job_id, status="queued", progress=5, message="Video acquired. Queued.", source_path=str(source), source_name=filename, clip_length=length, export_format=export, start=start_v, end=end_v, captions=captions_cfg, source_url=url)
        executor.submit(run_job_from_upload, job_id, source, filename, length, export, start_v, end_v, captions_cfg)
        return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        log.exception("URL_IMPORT_FAILED id=%s", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)[:500]) from exc


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
    update_job(job_id, status="queued", progress=0, message="Queued.", source_path=str(source), source_name=filename, clip_length=length, export_format=export, start=start_v, end=end_v, captions=captions_cfg)
    executor.submit(run_job_from_upload, job_id, source, filename, length, export, start_v, end_v, captions_cfg)
    return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


@app.post("/api/upload-chunk")
async def upload_chunk(request: Request) -> dict[str, Any]:
    """Receive an idempotent, resumable upload chunk."""
    job_id = (request.headers.get("X-Upload-Job") or "").strip()
    session_id = (request.headers.get("X-Upload-Session") or "").strip()
    if not job_id and session_id:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", session_id):
            raise HTTPException(400, "Invalid upload session.")
        job_id = session_id
    is_first = not bool(job_id)
    if is_first:
        rate_limit(request); cleanup_old_jobs(); job_id = uuid.uuid4().hex
    try:
        total_size = int(request.headers.get("X-Upload-Total", "0"))
        index = int(request.headers.get("X-Upload-Index", "-1"))
        total_chunks = int(request.headers.get("X-Upload-Chunks", "0"))
        requested_chunk_size = int(request.headers.get("X-Upload-Chunk-Size", str(UPLOAD_CHUNK_SIZE)))
    except ValueError as exc:
        raise HTTPException(400, "Invalid upload chunk metadata.") from exc
    if total_size <= 0 or total_size > MAX_VIDEO_SIZE:
        raise HTTPException(413, "Video exceeds the configured upload limit or has an invalid size.")
    if requested_chunk_size < 1 * 1024 * 1024 or requested_chunk_size > UPLOAD_CHUNK_SIZE:
        raise HTTPException(400, "Invalid upload chunk size.")
    if index < 0 or total_chunks < 1 or index >= total_chunks:
        raise HTTPException(400, "Invalid upload chunk index.")
    filename = safe_filename(request.headers.get("X-Upload-Name") or "video.mp4")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, "Unsupported video format.")
    job_dir = JOBS_ROOT / job_id; part = job_dir / "upload.part"; meta_path = job_dir / "upload.json"
    job_dir.mkdir(parents=True, exist_ok=True)
    lock = upload_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        if not meta_path.is_file():
            if total_chunks > math.ceil(MAX_VIDEO_SIZE / requested_chunk_size):
                raise HTTPException(400, "Too many upload chunks.")
            metadata = {
                "filename": filename, "total_size": total_size, "total_chunks": total_chunks, "chunk_size": requested_chunk_size, "received": [],
                "clip_length": request.headers.get("X-Upload-Clip-Length", "30"), "export_format": request.headers.get("X-Upload-Export", "original"),
                "start": request.headers.get("X-Upload-Start", "0"), "end": request.headers.get("X-Upload-End", "0"),
                "captions": request.headers.get("X-Upload-Captions", "0") == "1",
                "caption_language": request.headers.get("X-Upload-Caption-Language", "auto"), "caption_style": request.headers.get("X-Upload-Caption-Style", "classic"),
                "caption_font": request.headers.get("X-Upload-Caption-Font", "DejaVu Sans"), "caption_size": request.headers.get("X-Upload-Caption-Size", "large"),
                "caption_animation": request.headers.get("X-Upload-Caption-Animation", "none"), "caption_position": request.headers.get("X-Upload-Caption-Position", "bottom"),
            }
            validate_options(metadata["clip_length"], metadata["export_format"], metadata["start"], metadata["end"])
            meta_path.write_text(json.dumps(metadata), encoding="utf-8")
        else:
            # Existing metadata means this is a normal continuation/retry of the
            # same resumable upload. Do not reject it just because the job is not
            # in the in-memory jobs dict yet; the upload is only queued after the
            # final chunk arrives.
            existing = jobs.get(job_id)
            if existing and existing.get("status") in {"queued", "processing", "complete"}:
                return {"job_id": job_id, "status_url": f"/api/jobs/{job_id}", "complete": True, "retry": True}
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(400, "Upload session metadata is invalid.") from exc
        chunk_size = int(metadata.get("chunk_size", UPLOAD_CHUNK_SIZE))
        if chunk_size < 1 * 1024 * 1024 or chunk_size > UPLOAD_CHUNK_SIZE:
            raise HTTPException(400, "Upload session has an invalid chunk size.")
        if metadata.get("total_size") != total_size or metadata.get("total_chunks") != total_chunks or metadata.get("filename") != filename:
            raise HTTPException(400, "Upload chunk metadata does not match the original upload.")
        if "X-Upload-Chunk-Size" in request.headers and requested_chunk_size != chunk_size:
            raise HTTPException(400, "Upload chunk size does not match the original upload session.")
        offset = index * chunk_size; expected_chunk = min(chunk_size, total_size - offset)
        if offset >= total_size or expected_chunk <= 0:
            raise HTTPException(400, "Upload chunk offset is invalid.")
        received = {int(x) for x in metadata.get("received", [])}
        if index in received:
            return {
                "job_id": job_id,
                "status_url": f"/api/jobs/{job_id}",
                "index": index,
                "complete": len(received) == total_chunks,
                "received": len(received),
                "total_chunks": total_chunks,
            }
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
            return {
                "job_id": job_id,
                "status_url": f"/api/jobs/{job_id}",
                "index": index,
                "complete": False,
                "received": len(received),
                "total_chunks": total_chunks,
            }
        if part.stat().st_size != total_size:
            raise HTTPException(400, "Upload is incomplete or corrupted.")
        try:
            length, export, start_v, end_v = validate_options(metadata["clip_length"], metadata["export_format"], metadata["start"], metadata["end"])
            captions_cfg = captions_from_form(metadata["captions"], metadata["caption_language"], metadata["caption_style"], metadata["caption_font"], metadata["caption_size"], metadata["caption_animation"], metadata["caption_position"])
            source = job_dir / f"input{suffix}"; part.replace(source); meta_path.unlink(missing_ok=True)
            update_job(job_id, status="queued", progress=0, message="Upload complete. Queued.", source_path=str(source), source_name=filename, clip_length=length, export_format=export, start=start_v, end=end_v, captions=captions_cfg)
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
    received = sorted({int(x) for x in metadata.get("received", [])})
    total_chunks = int(metadata.get("total_chunks", 0) or 0)
    total_size = int(metadata.get("total_size", 0) or 0)
    received_set = set(received)
    chunk_size = int(metadata.get("chunk_size", UPLOAD_CHUNK_SIZE))
    received_bytes = sum(
        min(chunk_size, max(0, total_size - i * chunk_size))
        for i in received
    )
    next_missing = next((i for i in range(total_chunks) if i not in received_set), None)
    return {
        "job_id": job_id,
        "complete": False,
        "received": received,
        "total_chunks": total_chunks,
        "total_size": total_size,
        "chunk_size": metadata.get("chunk_size", UPLOAD_CHUNK_SIZE),
        "received_bytes": received_bytes,
        "next_missing": next_missing,
        "filename": metadata.get("filename", ""),
    }


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


@app.get("/api/batch-status")
def batch_status(jobs_param: str = Query("", alias="jobs")) -> dict[str, Any]:
    """Return the state of up to MAX_BATCH_FILES jobs in one request.

    Batch clients use this endpoint instead of polling every job separately.
    That cuts request volume substantially while keeping per-job failures
    isolated and preserving the existing job-status contract.
    """
    ids = [x.strip() for x in jobs_param.split(",") if x.strip()]
    if not ids or len(ids) > MAX_BATCH_FILES:
        raise HTTPException(400, f"Batch status must contain between 1 and {MAX_BATCH_FILES} jobs.")
    if any(not re.fullmatch(r"[0-9a-fA-F]{32}", job_id) for job_id in ids):
        raise HTTPException(400, "Invalid batch job identifier.")

    states: list[dict[str, Any]] = []
    for job_id in ids:
        job = jobs.get(job_id)
        if not job:
            path = _job_state_path(job_id)
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        jobs[job_id] = loaded
                        job = loaded
                except Exception:
                    job = None
        if job is None:
            states.append({"job_id": job_id, "status": "error", "progress": 0, "message": "Job not found or expired."})
        else:
            states.append({
                "job_id": job_id,
                "status": job.get("status", "queued"),
                "progress": max(0, min(100, int(job.get("progress", 0) or 0))),
                "message": job.get("message", ""),
                "result": job.get("result"),
            })
    return {"success": True, "jobs": states}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", job_id):
        raise HTTPException(400, "Invalid job identifier.")
    # Prefer the persisted snapshot when it exists. Worker threads update the
    # same file after each stage, so polling always sees the latest durable
    # state even if the request is served by a different process/thread.
    path = _job_state_path(job_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                jobs[job_id] = data
                return data
        except Exception:
            pass
    job = jobs.get(job_id)
    if job:
        return job
    raise HTTPException(404, "Job not found or expired.")


def _preview_headers() -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=86400, immutable",
        "X-Content-Type-Options": "nosniff",
    }


@app.get("/api/jobs/{job_id}/source-thumb")
def source_thumb(job_id: str) -> FileResponse:
    path = JOBS_ROOT / job_id / "source-thumb.jpg"
    if not path.is_file(): raise HTTPException(404, "Thumbnail not ready.")
    return FileResponse(path, media_type="image/jpeg", headers=_preview_headers())


@app.get("/api/jobs/{job_id}/thumbnail/{name}")
def clip_thumb(job_id: str, name: str) -> FileResponse:
    if "/" in name or "\\" in name: raise HTTPException(400, "Invalid filename.")
    clip = JOBS_ROOT / job_id / "clips" / name
    if not clip.is_file() or clip.suffix.lower() != ".mp4":
        raise HTTPException(404, "Clip not found.")
    thumb = clip.with_suffix(".jpg")
    if not thumb.is_file():
        # Backward-compatible fallback for jobs created before preview hardening.
        make_thumbnail(clip, thumb)
    if not thumb.is_file() or thumb.stat().st_size <= 0:
        raise HTTPException(404, "Preview not available.")
    return FileResponse(thumb, media_type="image/jpeg", headers=_preview_headers())


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


@app.get("/api/batch-zip")
def download_batch_zip(jobs_param: str = Query("", alias="jobs")) -> FileResponse:
    """Build one archive containing the completed clips from multiple jobs."""
    ids = [x.strip() for x in jobs_param.split(",") if x.strip()]
    if not ids or len(ids) > MAX_BATCH_FILES:
        raise HTTPException(400, f"Batch archive must contain between 1 and {MAX_BATCH_FILES} jobs.")
    if any(not re.fullmatch(r"[0-9a-fA-F]{32}", job_id) for job_id in ids):
        raise HTTPException(400, "Invalid batch job identifier.")

    first_dir = JOBS_ROOT / ids[0]
    if not first_dir.is_dir():
        raise HTTPException(404, "The first batch job was not found or has expired.")
    zpath = first_dir / f"clipshortener-batch-{ids[0][:8]}.zip"
    added = 0
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as z:
        for number, job_id in enumerate(ids, 1):
            clip_dir = JOBS_ROOT / job_id / "clips"
            if not clip_dir.is_dir():
                continue
            for clip in sorted(clip_dir.glob("clip_*.mp4")):
                if clip.is_file() and clip.stat().st_size > 0:
                    z.write(clip, f"video_{number}/{clip.name}")
                    added += 1
    if not added:
        zpath.unlink(missing_ok=True)
        raise HTTPException(404, "No completed clips were found for this batch.")
    return FileResponse(zpath, media_type="application/zip", filename=zpath.name)


@app.post("/frontend-error")
async def frontend_error(payload: dict[str, Any]):
    log.error("FRONTEND_ERROR %s", payload)
    return {"ok": True}
