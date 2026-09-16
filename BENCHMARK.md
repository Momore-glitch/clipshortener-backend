# ClipShortener speed benchmark

Test source: `Testing out German AA at 5.7 to see how good it'll be - Part 1(1).mp4`

- Source size: 304,660,037 bytes (~304.66 MB / ~290.55 MiB)
- Source duration: ~18:50.88
- Test workload: full-source processing path, 180-second clip segmentation
- Output target: 1080x1920 (9:16), H.264, CRF 23, ultrafast
- CPU test host: 5 logical CPUs

## Measured encoder comparison

A 30-second representative export was used to compare encoder configurations before the full workload test.

| Configuration | Time | Speed |
|---|---:|---:|
| ultrafast + zerolatency + threads=0 | 3.467 s | 8.65x realtime |
| ultrafast + fastdecode + threads=0 | 2.911 s | 10.30x realtime |
| ultrafast + fastdecode + threads=5 | 2.707 s | 11.08x realtime |

A 180-second segmented export using `ultrafast + fastdecode + threads=5` completed in 20.14 s (~8.94x realtime) on the local benchmark host.

These are local measurements only. They are **not a Render benchmark** and do not establish 2 GB / 15 minutes on Render.
