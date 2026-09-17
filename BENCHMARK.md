# ClipShortener performance notes

## Current production strategy
- One CPU-bound FFmpeg job at a time.
- 16 MB upload chunks remain unchanged.
- Individual MP4 downloads only; no ZIP/archive pipeline.
- `ultrafast` x264.
- Fast-bilinear scaling.
- AAC audio is copied when possible.
- `CLIPSHORTENER_NO_UPSCALE=1` by default: a source below 1080-class resolution is not enlarged to 1080. This is a major workload reduction, not a cosmetic FFmpeg flag.
- Render CPU allocation is read from `RENDER_CPU_COUNT`.

## Measured locally
A 60-second encode from the current attached test video, using the native-resolution speed path (720x1280 vertical output) and one FFmpeg thread, completed in about 6.9 seconds at CRF 23 in this environment. This is a local benchmark only and is **not** a Render benchmark.

A previous local 180-second 1080x1920 benchmark was about 10.6 seconds in a much faster local environment; it must not be used to predict Render performance.

## Production limitation
Render documents `RENDER_CPU_COUNT` as the CPU allocation for the service; for example, 0.5 CPU on a 0.5c plan and 2 CPUs on a 2c plan. CPU allocation is therefore a hard constraint on CPU-only H.264 encoding. See the Render metrics for actual CPU usage.
