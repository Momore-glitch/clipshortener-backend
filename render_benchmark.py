#!/usr/bin/env python3
"""One-off Render CPU/FFmpeg benchmark for ClipShortener.

Run this INSIDE the Render service/container (Render Shell), not locally.
It uses the exact current 1080p/9:16 CRF-23 encode settings and reports
Render's cgroup CPU quota, FFmpeg version, encode time, realtime factor,
and output size. It never changes the production job state.

Usage:
  python render_benchmark.py /tmp/clipshortener/jobs/<JOB_ID>/input.mp4

Optional:
  CLIPSHORTENER_BENCH_CLIP_SECONDS=180 python render_benchmark.py /path/input.mp4
"""
from __future__ import annotations
import os, pathlib, re, shutil, subprocess, sys, tempfile, time

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
CLIP_SECONDS = float(os.getenv("CLIPSHORTENER_BENCH_CLIP_SECONDS", "180"))
CRF = os.getenv("CLIPSHORTENER_BENCH_CRF", "23")
PRESET = os.getenv("CLIPSHORTENER_FFMPEG_PRESET", "ultrafast")
TUNE = os.getenv("CLIPSHORTENER_FFMPEG_TUNE", "fastdecode")
X264 = os.getenv(
    "CLIPSHORTENER_X264_PARAMS",
    "ref=1:bframes=0:me=dia:subme=0:trellis=0:rc-lookahead=0:sync-lookahead=0:mbtree=0:weightp=0:weightb=0",
)
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
    cmd=[FFPROBE,"-v","error","-show_entries","format=duration,size","-of","default=noprint_wrappers=1:nokey=1",str(path)]
    out=subprocess.check_output(cmd,text=True).splitlines()
    return float(out[0]), int(float(out[1]))

src=pathlib.Path(sys.argv[1]).expanduser().resolve() if len(sys.argv)>1 else None
if not src or not src.is_file():
    print("ERROR: supply the source video path from the Render job.", file=sys.stderr)
    sys.exit(2)

duration,size=probe(src)
with tempfile.TemporaryDirectory(prefix="clipshortener-render-bench-") as td:
    out=pathlib.Path(td)/"benchmark.mp4"
    vf="crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=fast_bilinear"
    cmd=[FFMPEG,"-hide_banner","-y","-nostdin","-sws_flags","fast_bilinear","-i",str(src),"-t",str(duration),"-map","0:v:0","-map","0:a?","-fps_mode","passthrough","-vf",vf,"-c:v","libx264","-preset",PRESET,"-tune",TUNE,"-x264-params",X264,"-crf",CRF,"-threads",THREADS,"-c:a","copy","-pix_fmt","yuv420p",str(out)]
    print(f"CPU quota: {cpu_quota()}")
    print(f"Source: {size/1e6:.2f} MB, {duration/60:.2f} min")
    print(f"FFmpeg: {subprocess.check_output([FFMPEG,"-version"],text=True).splitlines()[0]}")
    print("Starting exact transformed encode benchmark...")
    start=time.monotonic()
    p=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    elapsed=time.monotonic()-start
    if p.returncode:
        print(p.stderr[-4000:],file=sys.stderr)
        sys.exit(p.returncode)
    out_size=out.stat().st_size
    print(f"Encode seconds: {elapsed:.3f}")
    print(f"Realtime factor: {duration/elapsed:.3f}x")
    print(f"Output: {out_size/1e6:.2f} MB")
    print(f"Estimated same-content 2 GB processing: {(elapsed*(2000/size))/60:.2f} min (linear size estimate only)")
