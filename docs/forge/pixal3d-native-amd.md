# Pixal3D native Windows AMD backend

Status as of 2026-11-04: **native single-view draft 512 shape, standard 1024 shape, draft 512 geometry with a 1024 × 1024 UV atlas, and standard 1024 geometry with a 1024 × 1024 UV atlas are verified on RX 9070 XT**. Other native workflows remain locked.

This backend is a narrow native Windows path for AMD ROCm, not a universal GPU abstraction. It preserves the existing ComfyUI API, studio job flow and WSL fallback. It does not install or replace GPU drivers, remove WSL, change global Python or require a reboot.

## What is ready

- Explicit backend IDs: `windows` (NVIDIA), `wsl` (existing AMD fallback), and `windows-rocm` (native AMD).
- Isolated runtime at `runtime/ComfyUI-Pixal3D-AMD-Native`, optionally overridden by the absolute `ASTRA_PIXAL3D_NATIVE_DIR` path.
- Complete native ComfyUI/TRELLIS.2 installation and verified model-weight loading.
- Native process construction using portable `python_embeded/python.exe`; no `wsl.exe`, Linux Python or `/mnt/c` execution path.
- SDPA selected for AMD instead of `flash_attn`; no silent CPU, WSL or model fallback.
- Fail-closed native request validation and runtime installation checks.
- Job telemetry for backend, phase timings, fallback events and output geometry, serialized through the existing job result.
- Offline base installer and structured hardware probes; see `scripts/native/README.md`.

## Measured native results

Hardware: AMD Radeon RX 9070 XT, `gfx1201`, 17,095,983,104 bytes of reported device memory, PyTorch `2.9.1+rocm7.2.1`, HIP `7.2.53211-158bd99533`. All runs used seed 42 and the deterministic RGBA ellipsoid.

| Workflow | Job | Time | Vertices | Triangles | GLB bytes |
|---|---|---:|---:|---:|---:|
| Single-view draft 512, shape | `6df4738b-404e-4129-8742-0dfe024dce51` | 182.4 s | 50,729 | 95,570 | 1,756,320 |
| Single-view standard 1024, shape | `13ba0c81-211e-4c6d-9e43-fc7ec70f6f39` | 907.8 s | 330,358 | 660,712 | 11,893,576 |
| Single-view draft 512, UV atlas 1024 × 1024 | `e603b707-4329-4a0d-b2f1-1bbc67625811` | 546.4 s | 60,079 | 96,090 | 3,736,252 |
| Single-view standard 1024, UV atlas 1024 × 1024 | `eb4e1a32-a123-455c-aacf-86c4c2eef7b4` | 1,641.2 s | 335,153 | 660,226 | 15,616,676 |

All four GLBs passed strict technical validation with no fallback events. Reports and artifacts:

- Draft 512: `data/runtime/diagnostics/native-amd/512-single.json`, `data/runtime/amd-pixal3d-native-single.glb`.
- Standard 1024: `data/runtime/diagnostics/native-amd/1024-single.json`, `data/runtime/amd-pixal3d-native-single-standard.glb`.
- Draft 512 + UV 1024: `data/runtime/diagnostics/native-amd/512-single-uv1024.json`, `data/runtime/amd-pixal3d-native-single-uv1024.glb`.
- Standard 1024 + UV 1024: `data/runtime/diagnostics/native-amd/1024-single-uv1024.json`, `data/runtime/amd-pixal3d-native-single-standard-uv1024.glb`.

For standard 1024 shape only, the shape cascade took 623,249 ms, latent decode 12,841 ms, and mesh reconstruction 98,737 ms. For draft UV, texturing took 416,476 ms. In the combined standard UV run, the shape cascade took 639,595 ms, latent decode 16,013 ms, mesh reconstruction 269,213 ms, and texturing 558,041 ms. Both UV runs produced a non-uniform 1024 × 1024 PNG with finite in-range UV coordinates. Four-view previews were visually inspected; the combined output had a coherent surface and texture without obvious corruption. These checks validate the pipeline and mesh bounds, not artistic quality or anatomy.

`native-ready.json` enables `single512`, `single1024`, `uvTexture1024`, and `uvTextureStandard1024` with links to their E2E evidence. `uvTexture1024` means draft-512 geometry with a 1024 × 1024 atlas; `uvTextureStandard1024` separately enables standard-1024 geometry with a 1024 × 1024 atlas.

## CuMesh large-mesh fix

The first native decoder failed because Windows HIP device-to-device copies stopped after 12 MiB, corrupting meshes larger than 1,048,576 packed three-component rows. `src/CuMesh/src/io.cu` now copies both input and output in 4 MiB chunks; the HIP source is generated from that file during the build. The reproducible source change is tracked as `scripts/native/patches/cumesh-large-mesh.patch` because `runtime/` is disposable and Git-ignored.

The operational regression probe round-trips the exact failing dimensions (4,091,147 vertices and 7,921,414 faces) with an exact match and also checks open-boundary filling. Result: `data/runtime/diagnostics/native-amd/cumesh-probe.log`.

## O-Voxel tiled-topology fix

Overlapping tiles emitted the same triangles, while final deduplication removed only repeated vertices. The decoder now assigns each triangle to the tile containing its centroid and includes the outer grid boundary, avoiding a global unique operation over the full face buffer. The source and installed copies have identical SHA-256 hashes; the reproducible change is tracked separately as `scripts/native/patches/o-voxel-tiled-faces.patch`.

The regression cube now has 8 vertices and 12 triangles instead of 96 triangles, with exact direct/tiled topology equality. Additional direct/tiled comparisons passed for an asymmetric grid (20 faces), a non-empty negative world-space AABB (216 faces) and a rectangular grid (22 faces). Results: `data/runtime/diagnostics/native-amd/o-voxel-tiled-probe.log`.

## FlexGEMM memory and Windows compatibility fixes

The initial standard-1024 run required a 23 GiB explicit-GEMM `im2col` buffer, exceeding the card's reported 15.92 GiB of VRAM. `scripts/native/patches/flex-gemm-explicit-chunking.patch` bounds each temporary buffer to 512 MiB and processes the operation in sequential chunks. The same fix also removed the UV run's initial 8.95 GiB allocation failure.

Native UV processing exposed two additional Windows/ROCm issues:

- `scripts/native/patches/lscm-batched-solve.patch` keeps LSCM on the GPU but solves systems in batches of 16, avoiding the failing single large `hipblasDgetrfBatched` call.
- `scripts/native/patches/flex-gemm-weighted-sum.patch` replaces the unavailable Windows/Triton weighted-sum kernel with equivalent PyTorch GPU indexing, multiplication, and reduction.

Focused numerical tests cover chunk-boundary equivalence, batched LSCM equivalence, and weighted-sum values and gradients.

## Capability state

| Workflow | Native `windows-rocm` | Existing AMD `wsl` |
|---|---|---|
| Single-view draft 512, shape | **verified** | verified |
| Multi-view draft 512, shape | locked | verified |
| Single-view standard 1024, shape | **verified** | verified |
| Multi-view standard 1024 | locked / unsupported | unsupported on the verified 16 GB card |
| UV texture 512 | locked | verified only for single-view draft 512 |
| Single-view draft 512 geometry + UV atlas 1024 × 1024 | **verified** | unsupported |
| Standard 1024 geometry + UV atlas 1024 × 1024 | **verified** | unsupported |

An unverified native workflow still returns `pixal3d_native_not_ready`; it never silently falls back to WSL or CPU.

## Selection and rollback

Set the backend explicitly before starting AstraForge:

```powershell
$env:ASTRA_PIXAL3D_BACKEND = 'windows-rocm'
# Optional; must be an absolute native Windows path:
$env:ASTRA_PIXAL3D_NATIVE_DIR = 'C:\absolute\path\to\ComfyUI-Pixal3D-AMD-Native'
```

Rollback does not require deleting or reinstalling either runtime:

```powershell
$env:ASTRA_PIXAL3D_BACKEND = 'wsl'
Remove-Item Env:ASTRA_PIXAL3D_NATIVE_DIR -ErrorAction SilentlyContinue
```

The default backend selection remains unchanged unless explicitly configured. Backend mismatch in the hardware runner is an error, not an automatic fallback.

## Existing WSL reference measurements

These are fallback references, not native performance claims:

| WSL run | Time | Peak VRAM |
|---|---:|---:|
| Full single-view 1024 | 842.0 s | 15,301.30 MB |
| Draft single-view 512 | 186.3 s | 10,048.65 MB |
| Draft multi-view 512 | 184.0 s | 10,175.95 MB |

Native standard 1024 shape only completes in 907.8 s (about 15 minutes). Standard 1024 with UV 1024 took 1,641.2 s in the first run and 1,300.8 s in the profiling run. This variation and the phase timings establish bounded-memory execution, not a consistently fast, comfortable, or freeze-free workflow.

## Responsiveness baseline

`scripts/native/run_native_standard_uv1024_profile.mjs` monitors the real Standard 1024 + UV 1024 workflow from a separate process. It writes JSONL once per non-overlapping sample, including system CPU/RAM, native VRAM, job phase, and local studio heartbeat latency. It does not modify the studio or native engine and makes no external requests.

Profiling job `8f5b627f-9b41-4a80-8dab-0229fb6db2dc` completed in 1,300.8 s with an accepted GLB and no fallback events. Across 1,294 samples, studio heartbeat p50 was 0.72 ms, p95 1.13 ms, and the post-startup maximum 9.97 ms. There were no heartbeat stalls over 250 ms or 1 s and no studio/job request errors.

The native engine had 18 connection failures before its first successful stats response and three 2.5-second `/system_stats` timeouts during `Rebuilding the surface`. Studio heartbeat remained at or below 2.48 ms in those three samples. This isolates a brief native telemetry stall without evidence of a matching studio API stall. The largest monitor sampling gap was 3.518 s because the sampler awaited the bounded engine request.

System-wide maxima were 95.31% CPU and 29,231 MiB RAM; reported device usage peaked at 12,431 MiB. CPU/RAM are not process-attributed. These results do not prove freeze-free graphics, drivers, browser rendering, or external networking, so no production performance change was made from this baseline alone. Evidence: `1024-single-uv1024-profile.jsonl`, `1024-single-uv1024-profile.json`, and `1024-single-uv1024-profile-e2e.json`.

## Cancellation and VRAM cleanup

`scripts/native/run_native_lifecycle.mjs` exercises the real studio job and native ComfyUI on isolated local ports, without the broad server suite or a browser. It samples the Torch allocator through `/system_stats` and stops only the processes it started.

- Cancellation job `abbcad5f-5e6e-4769-a43f-a6ab97488ebe`: the running prompt was interrupted, the queue drained in about 11 seconds, and Torch reserved memory settled at 198 MiB from a 2 MiB baseline. Report: `512-cancel.json`.
- Completed job `d65ea747-c90f-4e77-9a12-40f09780cbd8`: generation completed, the queue became idle, and Torch reserved memory settled at 144 MiB from a 2 MiB baseline. Report: `512-complete-cleanup.json`.

These measurements verify cleanup relative to the native process baseline; they do not claim zero allocator retention or provide an uninterrupted device-wide peak measurement.

## Next gates

1. Verify multi-view 512 and UV 512 independently; leave them locked until their own E2E checks pass.
2. Keep multi-view 1024 locked until separately implemented and measured.
3. Add process-attributed and graphics/network measurements if user-visible stalls persist; the local heartbeat baseline alone cannot locate those stalls.

## Verification completed

- Focused native/backend/UV/monitor Node tests: 63 tests, 61 passed, 2 conditional skips, 0 failures.
- Native installation and source-synchronization tests: passed.
- Extension readiness contracts and CPU-reference attention parity: passed.
- Large CuMesh round-trip and open-square fill: passed.
- Direct/tiled O-Voxel cube, asymmetric, negative-AABB and rectangular topology checks: passed.
- FlexGEMM chunking, batched LSCM, and weighted-sum focused tests: passed.
- Native single-view 512, standard 1024, draft 512 + UV 1024, and standard 1024 + UV 1024 E2E, GLB validation, and visual inspection: passed.
- Standard 1024 + UV 1024 responsiveness baseline: passed; no local studio heartbeat stall observed.
- Real cancellation, queue draining and post-cancellation VRAM cleanup: passed.
- Full generation and post-run VRAM cleanup: passed.

Logs are under `data/runtime/diagnostics/native-amd/`. The broad server suite was not rerun during this hardware-debugging pass.

Implementation plans: `docs/superpowers/plans/2026-11-03-astraforge-native-amd-pixal3d.md`, `docs/superpowers/plans/2026-11-03-astraforge-native-1024-uv.md`, `docs/superpowers/plans/2026-11-04-native-standard-1024-uv.md`, and `docs/superpowers/plans/2026-11-04-native-standard-uv-profiling.md`.
