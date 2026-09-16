# ClipShortener processing benchmark

## Test file

`Testing out German AA at 5.7 to see how good it'll be - Part 1(1).mp4`

Measured source metadata:
- Size: 304,660,037 bytes (304.66 MB / 290.55 MiB)
- Duration: 306.294 seconds (5:06.29)
- Video: H.264, 1280x720, 30 fps
- Audio: AAC

## Exact requested workload

- Process the **whole source**
- Clip length: **180 seconds**
- Ratio: **9:16**
- Quality: **1080p / CRF 23**
- Output: H.264 MP4
- One FFmpeg encode process
- No ZIP packaging

Expected outputs: 2 clips (~180s + ~126s).

## Current benchmark

The current processing command uses:
- libx264 `ultrafast`
- `fastdecode`
- CRF 23
- automatic FFmpeg thread allocation (`threads=0`)
- automatic filter-thread allocation (`filter_threads=0`)
- `fast_bilinear` scaling
- source crop before upscale
- AAC audio stream copy
- single-pass segmentation
- `-nostdin` to prevent accidental stdin/process stalls

Full-source benchmark result on the local 5-CPU test host:

**34.37 seconds for 306.29 seconds of source = 8.91x realtime.**

Output files:
- clip 1: 171,192,149 bytes
- clip 2: 127,140,437 bytes
- total: 298,332,586 bytes

This is a local benchmark only. It is **not** a Render benchmark and does not prove the 2 GB / 15 minute target.

## Important correction

The uploaded test file is approximately **5 minutes 6 seconds**, not 18 minutes 51 seconds. Earlier benchmark notes incorrectly reported its duration. This report uses the actual ffprobe duration.
