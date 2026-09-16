# ClipShortener speed pass

## Changes in this pass

- 16 MB chunk upload path is unchanged.
- Processing remains single-flight: one CPU-heavy FFmpeg job at a time.
- FFmpeg thread count is now **container-CPU-quota aware** instead of blindly using host CPU count.
- Filter threads are bounded to avoid oversubscription on low-CPU Render instances.
- Transformed exports are encoded **directly to the individual MP4 segment files**. The previous intermediate full-size `encoded.mp4` write/read cycle is removed.
- No ZIP/archive generation.
- Individual MP4 downloads remain range-capable.
- Requested quality, ratio, and clip length are unchanged.

## Local sanity benchmark

Test source: the supplied ~304.66 MB, 18:50.88 H.264/AAC video.

Workload: 30 seconds, 1080x1920 (9:16), CRF 23, ultrafast, fast_bilinear, 180-second segment target.

Measured direct-segment encode in the current environment: **2.997 seconds for 30 seconds of video (~10.0x realtime)**.

This is a local sanity check only. It is **not** a Render benchmark.

## Render benchmark

The authoritative production benchmark must be executed inside the deployed Render service. The previous real-site observation was approximately 22% UI progress after 5 minutes; that is not converted into a fake throughput claim here because the application's progress scale and Render CPU quota both matter.

The `/health` diagnostics now expose the detected container CPU quota and FFmpeg thread count, so the Render environment can be measured directly.
