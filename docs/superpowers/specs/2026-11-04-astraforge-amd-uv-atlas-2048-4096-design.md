# Native AMD UV base-color atlas 2048–4096 design

Date: 2026-11-04

## Goal

Increase only the final Pixal3D UV base-color atlas resolution on the verified RX 9070 XT. Keep the proven texture denoiser at 1024, the existing baton geometry, sampling parameters, seed, native Windows ROCm backend, and stable Standard 1024 + UV 1024 workflow unchanged. Do not implement sparse projection, texture denoiser 1536, shape 1536, WSL, driver changes, or fallback.

Increasing atlas resolution improves rasterization and texel density but cannot recover detail absent from the 1024 texture latent. Visual improvements and regressions must be assessed separately from PNG dimensions.

## Practical limits

The node formally accepts `texture_size` up to 16384, but the current postprocess allocates full-size GPU and CPU buffers. Its six-channel float32 attribute atlas alone needs approximately 100 MiB at 2048, 403 MiB at 4096, 1.5 GiB at 8192, and 6 GiB at 16384, excluding rasterization, interpolation, images, inpainting, and model memory. The GLB validator also has a 100 MiB total limit.

Use 2048 as the first hardware gate and 4096 as the realistic target. An 8192 attempt is outside this design because it may require tiled postprocess/inpainting and a separate GLB-size decision. The formal 16384 input limit is not a practical target for the current implementation.

## Stage 1: isolated UV 2048 run

Reuse the accepted baton GLB and original input image. Run only the texture stage in a dedicated experimental native ComfyUI workflow with texture denoiser resolution 1024 and `texture_size=2048`. Preserve geometry orientation, camera configuration, seed, sampler, texture steps, UV unwrap behavior, and inpainting.

Use separate local ports, logs, output files, and a report. Do not overwrite the accepted baton result or readiness capabilities. Record wall time, memory telemetry, requested and actual denoiser/atlas resolutions, and fallback events.

Acceptance requires a completed native AMD run, valid UVs, an embedded base-color PNG exactly 2048 × 2048, non-uniform content, an accepted GLB within existing limits, and no fallback. Inspect four rendered views and the atlas against the 1024 result.

## Stage 2: isolated UV 4096 run

Only after UV 2048 passes, repeat the same texture-only experiment with `texture_size=4096`; do not change the denoiser or geometry to make it pass. Use another set of artifacts and preserve the 2048 result.

Apply the same technical and visual checks with an exact 4096 × 4096 atlas. Stop on OOM, driver reset, malformed UV output, GLB-size rejection, or excessive memory pressure. Do not retry blindly, change drivers, or unlock a persistent capability.

## Error handling and delivery

Run GPU work sequentially without browser stress tests or parallel GPU jobs. Preserve raw logs on failure. Report time, memory evidence where available, atlas dimensions, GLB size, fallback events, visible crust/scoring detail, backside/ends, stretching, color shift, and seams. Higher resolution is not automatically higher quality.

Deliver the successful experimental GLB, atlas, four-view preview, diagnostic report, and an honest comparison with UV 1024. A separate user request is required before exposing a persistent new profile, committing implementation changes, or pushing.
