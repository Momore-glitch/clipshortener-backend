# ClipShortener throughput baseline

## Current target
- 1.3 GB source with the user's selected export options: target <= 20 minutes.
- Secondary target: approximately <= 5 seconds per 1% of end-to-end progress.

## Version 49.0.0-throughput-route
- Preserves 16 MB upload chunks.
- Single CPU-heavy processing job.
- Source-aware no-upscale routing.
- Crop before scale.
- Single decode/filter/encode path for transformed exports.
- AAC audio copied when possible.
- Fixed clip-aligned GOPs for reliable CFR inputs, with the existing forced-keyframe route retained for VFR/unknown-rate inputs.
- Individual MP4 outputs only; no ZIP generation.
- Output-duration validation remains configurable and off by default.

## Local route tests
- 60 s, 720x720, libx264 ultrafast, CRF 23, 4 threads: ~3.60 s in the local test environment.
- 60 s, 720x720, 4 threads, forced keyframes: ~3.76 s.
- 60 s, 720x720, 4 threads, fixed clip-aligned GOP: ~3.48 s.
- 180 s, 720x720, 4 threads: fixed GOP and forced-keyframe routes were approximately equal (~11.6 s).

These are local measurements only and are not Render benchmarks. Production Render performance must be measured on the deployed service.
