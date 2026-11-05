# Native AMD 1024, UV 1024 and responsiveness design

Date: 2026-11-03

## Goal

On the verified RX 9070 XT native Windows ROCm backend, deliver two independently validated Pixal3D workflows without WSL in the generation path:

1. Single-view standard-1024 shape generation without a texture.
2. Single-view draft-512 geometry with a real 1024 × 1024 UV base-color atlas.

Profile and optimize tiled O-Voxel decode before those runs without changing geometry. Reduce measurable sources of PC and UI micro-freezes without lowering generation quality. Drivers, Windows priorities, WSL installation and the NVIDIA runtime remain unchanged.

## Chosen approach

Use staged evidence rather than unlocking all native capabilities at once. Each workflow has an isolated hardware run, its own diagnostic JSON and GLB, and a persistent capability only after technical and visual acceptance. A failed run leaves the capability disabled.

Two alternatives were rejected: unlocking both workflows simultaneously makes failures difficult to localize, and implementing only UV 1024 does not satisfy the shape-1024 goal.

## Stage 1: tiled decode profiling and optimization

Measure extraction, centroid ownership filtering, concatenation and final vertex deduplication in the real tiled O-Voxel path. Use CUDA timing where device work is measured and wall timing around host/device synchronization. Run GPU tests sequentially.

Change only the demonstrated dominant operation. Preserve the centroid ownership rule, including the inclusive outer boundary. Do not reintroduce duplicate triangles or replace the fix with a global face sort without measurements proving it preferable.

Acceptance requires exact direct/tiled topology equality for the cube, asymmetric grid, negative world-space AABB and rectangular grid, the large CuMesh regression, all 24 existing isolated tests, and a fresh native single-view 512 E2E with no fallback. Report timings as measurements, not as a guaranteed speedup from one run.

## Stage 2: native single-view shape 1024

Use the existing standard single-view graph with the 1024 cascade, tiled decoder, 1024 reconstruction, Heun sampling and the existing 25/12/12 step counts. Do not reduce steps, target faces or resolution to make the run pass.

Add a narrow native hardware runner using isolated local ports and separate artifacts:

- `data/runtime/diagnostics/native-amd/1024-single.json`;
- `data/runtime/amd-pixal3d-native-single-standard.glb`.

The existing free-VRAM preflight remains active. Record backend, phase durations, fallback events, geometry counts and available memory telemetry. Validate finite geometry, indices and non-zero triangle counts, then inspect four rendered views.

The temporary capability needed for an isolated hardware attempt is restored on failure. Persistent `single1024=true` is written only after a valid E2E result. Multi-view 1024 remains disabled.

## Stage 3: native UV atlas 1024 on draft-512 geometry

Keep the already validated draft-512 geometry path and the Pixal3D texture latent. For native `windows-rocm` UV requests, set `texture_size=1024` and `bake_on_vertices=false`. Keep WSL UV behavior at 512 and leave NVIDIA behavior unchanged.

Reuse the explicit AMD ComfyUI UV unwrap, tiled barycentric rasterizer and interpolation path. It is not a silent shape-only, vertex-color, WSL or whole-pipeline CPU fallback. Missing UVs, an empty raster, a missing texture or a malformed PNG is an error.

Add separate artifacts:

- `data/runtime/diagnostics/native-amd/512-single-uv1024.json`;
- `data/runtime/amd-pixal3d-native-single-uv1024.glb`.

Validation requires `TEXCOORD_0`, a `pbrMetallicRoughness.baseColorTexture`, an embedded decodable PNG exactly 1024 × 1024, non-uniform color content and non-empty UV coverage. Inspect the atlas and four textured renders. Persistent `uvTexture1024=true` is written only after these checks pass. `uvTexture512` is not inferred from the 1024 result.

## Responsiveness and micro-freezes

Avoid unnecessary device-wide synchronizations and large synchronous GPU-to-CPU readbacks. Keep long operations in bounded tiles or chunks where this does not change output. The studio and ComfyUI remain separate processes so UV work cannot block studio progress or job state.

During active generation, the existing preview may use an adaptive frame rate and DPR; generated geometry, texture resolution and exports must not change. Restore preview quality after generation. Do not run heavy GPU tests in parallel, broad server suites or browser stress tests during hardware validation.

Measure long tasks and interface responsiveness separately from model generation time. No absolute zero-micro-freeze guarantee is made for a GPU also used by the desktop, but measured causes inside AstraForge must be addressed. A previous browser disconnect is treated as incidental unless it repeats.

## Failure handling and rollback

On ROCm OOM, a driver reset, invalid geometry, malformed UV output or a browser disconnect, stop the affected attempt and preserve its log. Do not retry blindly, hide the failure through WSL/CPU execution, change drivers or unlock the mode. Restore temporary readiness state and stop only processes owned by the isolated runner.

The existing `wsl` backend and recovery artifacts remain intact. No commit or push is performed without a separate user request.

## Automated verification

Use test-driven changes for workflow options, backend validation, UV atlas dimensions, capability gating and runner configuration. Preserve existing defaults and WSL behavior. Run the focused Node and Python tests, source-parity checks, `git diff --check`, O-Voxel/CuMesh probes and real native E2E runs. Do not substitute unit tests for hardware or visual evidence.
