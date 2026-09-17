# ClipShortener performance notes — route engine 47

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


## Route benchmark used for the 47.0 engine
The attached test source was benchmarked locally with the same FFmpeg family and output route used by the service. On a 15-second sample:

| Route | Time | Realtime |
|---|---:|---:|
| 1080 vertical, segmented, custom x264 params | 1.686 s | 8.90x |
| 1080 vertical, segmented, stock `ultrafast` | **1.442 s** | **10.41x** |
| 720 vertical, segmented, custom x264 params | 0.934 s | 16.07x |
| 720 vertical, segmented, stock `ultrafast` | **0.976 s** | **15.38x** |

A separate 30-second thread test on the 1080 route measured approximately 5.881 s at one thread, 3.408 s at two, 2.568 s at four, and 2.548 s with FFmpeg auto-threading. The production engine therefore continues to derive its thread budget from the actual CPU allocation instead of hard-coding one local benchmark's thread count.

### Key finding
The previous no-upscale implementation was not actually crop-aware: a 1280x720 source selected for a 1080/9:16 export could still be enlarged to 1080x1920 after the vertical crop. The new route engine classifies the source before choosing the encode size. A 1080/9:16 request from 720p-class source material is routed to 720x1280 instead of 1080x1920, eliminating the large 1080-class pixel workload while keeping a standard vertical canvas.

The new engine also removes the previously forced x264 parameter bundle by default because the measured stock `ultrafast` route was faster on the real workload.

All measurements above are local and are **not** Render measurements. Production CPU capacity remains governed by the Render service's assigned CPU.
