# ClipShortener final-pass benchmark notes

## Production evidence
- Render deployment logs showed repeated `OPTIONS /api/upload-chunk` requests and a `GET /api/upload-session/<id>` returning 404 after a deploy.
- The frontend screenshots showed the 16 MB resumable upload repeatedly retrying at 0%.
- A prior production run reached only about 22% after roughly 5 minutes; that Render result is the authoritative production performance signal.

## Changes in this pass
- Resumable upload sessions no longer return a hard 404 when an old session ID has lost its ephemeral `upload.json`; a valid session ID now receives an empty reset state so the client can recreate the session by resending chunk 0.
- FFmpeg thread selection now honours Render's `RENDER_CPU_COUNT` before falling back to cgroup quota detection.
- The previous `fastdecode` default was removed; it is not an encoding-speed optimisation.
- Runtime diagnostics now expose the Render CPU allocation.
- 16 MB upload chunk size remains unchanged.
- No ZIP packaging was added.

## Local sanity benchmark
Same source video, 180-second workload, 1080x1920 vertical, CRF 23, ultrafast x264, direct segmented output:
- 180 seconds encoded in 10.603 seconds in the local environment.
- This is **not** a Render benchmark and must not be extrapolated to Render.
