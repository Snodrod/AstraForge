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

## Verified result: 2026-11-04

Both hardware gates passed sequentially on the RX 9070 XT using the accepted Standard 1024 baton GLB, denoiser resolution 1024, seed 42, 12 Euler texture steps, and the native Windows ROCm backend. Geometry was reused rather than regenerated. The experimental runner used isolated port 8194 and did not change `native-ready.json` or the production UV 1024 defaults.

| Embedded base-color atlas | Texture-only time | GLB bytes | Vertices | Triangles | Validation | Fallback events |
|---|---:|---:|---:|---:|---|---|
| 2048 × 2048 | 499.0 s | 22,138,764 | 357,819 | 662,201 | accepted | `[]` |
| 4096 × 4096 | 382.4 s | 37,962,708 | 357,819 | 662,201 | accepted | `[]` |

The extracted PNG dimensions matched the requested size exactly. UV coordinates were finite and in range in both outputs. The 4096 GLB remained below the existing 100 MiB limit. VRAM telemetry was unavailable (`vram: null`), so these runs make no peak-memory claim. The shorter 4096 wall time is an observed warm-run result, not a claim that 4096 is inherently faster.

Four-view previews and extracted atlases were inspected. The newly textured results showed clearer crust and scoring than the saved UV 1024 baton; the visible difference between 2048 and 4096 was small at normal preview scale. Stretching and blur on the ends and inferred back side remain, and higher atlas resolution cannot add detail absent from the denoiser. Because the texture stage was rerun, the visual change from the saved 1024 result is not attributed solely to atlas size.

Artifacts are `data/runtime/baton/amd-pixal3d-native-single-uv2048.glb`, `data/runtime/baton/amd-pixal3d-native-single-uv4096.glb`, and `data/runtime/diagnostics/native-amd/baton-uv2048.json` / `baton-uv4096.json`. External previews are `preview-renders/baton-uv2048.png`, `baton-uv4096.png`, their extracted atlases, and `baton-uv-comparison.png`.

The first 2048 attempt stopped with `No module named 'natten'` before atlas creation. The isolated runner had inherited the user Torch cache instead of the native runtime cache containing the patched NAF fallback. It now applies the production `pixal3dEnv()` cache construction, and a regression test checks `HF_HOME` and `TORCH_HOME`. No NATTEN package, driver, network component, or fallback was installed.

UV 2048 is the recommended experimental balance of file size and visible quality; UV 4096 is the largest verified atlas for this baton. Neither is a persistent production capability yet. UV 8192 and the formal 16384 input maximum remain unverified and were not attempted.
