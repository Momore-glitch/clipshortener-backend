#!/usr/bin/env python3
"""One-off in-container benchmark for ClipShortener's real export routes.

Run this INSIDE the deployed Render container/service when shell access is
available. It does not modify production job state.

Usage:
  python render_benchmark.py /path/to/input.mp4
  CLIPSHORTENER_BENCH_CLIP_SECONDS=180 python render_benchmark.py /path/to/input.mp4

The benchmark compares the two important routes:
  1. SMART: source-class aware 9:16 output. 720p-class sources use 720x1280;
     1080p-class sources use 1080x1920.
  2. EXACT-1080: always 1080x1920, useful for measuring the cost of forcing
     the larger output class.
"""
from __future__ import annotations
import os, pathlib, shutil, subprocess, sys, tempfile, time

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
CLIP_SECONDS = float(os.getenv("CLIPSHORTENER_BENCH_CLIP_SECONDS", "180"))
CRF = os.getenv("CLIPSHORTENER_BENCH_CRF", "23")
PRESET = os.getenv("CLIPSHORTENER_FFMPEG_PRESET", "ultrafast")
TUNE = os.getenv("CLIPSHORTENER_FFMPEG_TUNE", "")
X264 = os.getenv("CLIPSHORTENER_X264_PARAMS", "")
THREADS = os.getenv("CLIPSHORTENER_FFMPEG_THREADS", "0")


def cpu_quota():
    for p in ("/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
        try:
            text = pathlib.Path(p).read_text().strip()
            if p.endswith("cpu.max"):
                q, period = text.split()[:2]
                return "unlimited" if q == "max" else f"{int(q)/int(period):.3f} CPU"
            q = int(text)
            period = int(pathlib.Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            return "unlimited" if q < 0 else f"{q/period:.3f} CPU"
        except Exception:
            pass
    return "unknown"


def probe(path):
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration,size:stream=width,height,codec_name,codec_type", "-of", "json", str(path)]
    import json
    data = json.loads(subprocess.check_output(cmd, text=True))
    fmt = data.get("format", {})
    video = next(s for s in data.get("streams", []) if s.get("codec_type") == "video")
    return float(fmt.get("duration", 0)), int(float(fmt.get("size", 0))), int(video.get("width", 0)), int(video.get("height", 0))


def run_route(src, seconds, width, height, name, td):
    out = pathlib.Path(td) / f"{name}.mp4"
    # Crop first, then scale. This avoids creating a large intermediate frame
    # only to throw most of it away during the vertical crop.
    vf = f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale={width}:{height}:flags=fast_bilinear"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-sws_flags", "fast_bilinear", "-ss", "0", "-i", str(src),
           "-t", str(seconds), "-map", "0:v:0", "-map", "0:a?",
           "-fps_mode", "passthrough", "-vf", vf, "-c:v", "libx264",
           "-preset", PRESET, "-crf", CRF, "-threads", THREADS,
           "-c:a", "copy", "-pix_fmt", "yuv420p"]
    if TUNE:
        cmd += ["-tune", TUNE]
    if X264:
        cmd += ["-x264-params", X264]
    cmd += [str(out)]

    started = time.monotonic()
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    elapsed = time.monotonic() - started
    if proc.returncode:
        print(proc.stderr[-4000:], file=sys.stderr)
        raise SystemExit(proc.returncode)
    return elapsed, out.stat().st_size


src = pathlib.Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else None
if not src or not src.is_file():
    print("ERROR: supply the source video path from the deployed service.", file=sys.stderr)
    raise SystemExit(2)

duration, size, sw, sh = probe(src)
smart = (720, 1280) if min(sw, sh) < 1000 else (1080, 1920)
seconds = min(CLIP_SECONDS, duration)

print(f"CPU quota: {cpu_quota()}")
print(f"Source dimensions: {sw}x{sh}")
print(f"Source size: {size/1e6:.2f} MB, duration: {duration/60:.2f} min")
print(f"Smart route: {smart[0]}x{smart[1]}")
print(f"FFmpeg: {subprocess.check_output([FFMPEG, '-version'], text=True).splitlines()[0]}")

with tempfile.TemporaryDirectory(prefix="clipshortener-route-bench-") as td:
    for name, dims in (("smart", smart), ("exact_1080", (1080, 1920))):
        elapsed, out_size = run_route(src, seconds, dims[0], dims[1], name, td)
        print(f"{name}: {elapsed:.3f}s | realtime {seconds/elapsed:.3f}x | output {out_size/1e6:.2f} MB")
        print(f"{name}: linear 2GB estimate {elapsed*(2000/size)/60:.2f} min (size-linear estimate only)")
