# Native Windows AMD Pixal3D tools

These tools build and probe an **isolated native Windows ROCm runtime**. They do not install or replace GPU drivers, change WSL, modify the NVIDIA runtime, or request a reboot.

Current scope on RX 9070 XT: native single-view draft 512 shape, standard 1024 shape, draft 512 geometry with a 1024 × 1024 UV atlas, and standard 1024 geometry with a 1024 × 1024 UV atlas are verified. Multi-view and UV 512 remain locked. The base installer still installs only the Python/PyTorch HIP foundation; the complete ComfyUI/TRELLIS.2 runtime and model weights are separate readiness requirements.

## Layout

- `installer/amd/Pixal3DNative.ps1` — isolated portable-Python and ROCm installer plus hardware probe.
- `scripts/native/install_pixal3d_amd.py` — native-path and offline-wheel validation.
- `scripts/native/verify_pixal3d_amd.py` — fail-fast Python, HIP, GPU, matrix, SDPA and memory probes.
- `scripts/native/requirements-base.txt` — pinned base Python dependencies.
- `scripts/native/patches/` — reproducible source fixes for the Git-ignored native runtime.
- `runtime/ComfyUI-Pixal3D-AMD-Native/` — disposable native runtime; never shared with WSL or NVIDIA.
- `data/runtime/diagnostics/native-amd/` — hardware JSON reports and logs; runtime output is not source material.

The default runtime can be overridden only with an absolute native Windows path through `-RuntimeRoot` or `ASTRA_PIXAL3D_NATIVE_DIR`. The installer rejects relative paths, `/mnt/<drive>` paths and the existing `ComfyUI-Pixal3D` / `ComfyUI-Pixal3D-AMD` runtime directories.

## Required offline packages

`installer/downloads/amd/` must contain the ROCm 7.2.1 meta-package and all six Windows wheels validated by `install_pixal3d_amd.py`:

- `rocm-7.2.1.tar.gz` (provides the `rocm_sdk` package);
- `rocm_sdk_core`, `rocm_sdk_devel`, `rocm_sdk_libraries_custom` 7.2.1;
- `torch` / `torchaudio` 2.9.1 and `torchvision` 0.24.1, all `+rocm7.2.1` CPython 3.12 Windows wheels.

The source meta-package is installed with `--no-build-isolation`. Missing packages or CUDA wheels stop installation; there is no silent substitution.

## Install and probe

Run from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 -SkipModels -ForceProbe
```

Useful options:

```powershell
# Validate an explicit isolated runtime without installing anything.
$native = Join-Path (Get-Location) 'runtime/ComfyUI-Pixal3D-AMD-Native'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 -RuntimeRoot $native -ProbeOnly

# Preview the base installation target without changing files.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 -DryRun
```

`-ReportPath` selects a different JSON report. `-ForceProbe` reinstalls/revalidates the base stack even when `native-base-ready.json` exists. `-SkipModels` is part of the installer contract; this base stage does not install model weights either way.

Run the probe directly with the native interpreter when needed:

```powershell
& "$native/python_embeded/python.exe" -I scripts/native/verify_pixal3d_amd.py --json data/runtime/diagnostics/native-amd/base-probe.json
```

The process exit code alone is not the readiness decision: inspect `nativeReady` and every mandatory check in the JSON. For the base report, `nativeReady: true` means **base HIP readiness only**. The server separately requires runtime files and `native-ready.json`; each generation capability is enabled only after its own E2E gate.

A passing base installation writes `native-base-ready.json` with the GPU, Torch/HIP versions, report path and SHA-256. Its capability map remains all `false`.

## Hardware result: RX 9070 XT

Measured on 2026-11-03 and 2026-11-04 with `AMD Radeon RX 9070 XT`, `gfx1201`, 17,095,983,104 bytes of device memory, PyTorch `2.9.1+rocm7.2.1`, HIP `7.2.53211-158bd99533`:

- native Python and ROCm/HIP import: pass;
- GPU detection and `gfx1201` profile: pass;
- FP32, FP16 and BF16 matrix multiplication against CPU references: pass;
- SDPA forward and backward against CPU: pass;
- temporary VRAM release back to the measured baseline: pass (`79,691,776` bytes before and after; peak `96,468,992` bytes);
- exact CuMesh round-trip for 4,091,147 vertices and 7,921,414 faces: pass;
- tiled O-Voxel regression: direct and tiled topology match for the 12-face cube, an asymmetric grid, a negative world-space AABB and a rectangular grid;
- native single-view draft 512 shape E2E: pass in 182.4 seconds, with 50,729 vertices, 95,570 triangles and no fallback events;
- native single-view standard 1024 shape E2E: pass in 907.8 seconds, with 330,358 vertices, 660,712 triangles and no fallback events;
- native single-view draft 512 geometry with a 1024 × 1024 UV atlas: pass in 546.4 seconds, with 60,079 vertices, 96,090 triangles and no fallback events;
- native single-view standard 1024 geometry with a 1024 × 1024 UV atlas: pass in 1,641.2 seconds, with 335,153 vertices, 660,226 triangles and no fallback events.

Reports: `data/runtime/diagnostics/native-amd/base-probe.json`, `cumesh-probe.log`, `o-voxel-tiled-probe.log`, `512-single.json`, `1024-single.json`, `512-single-uv1024.json`, and `1024-single-uv1024.json`.

The standard-1024 shape-only result takes about 15 minutes; standard 1024 with UV 1024 takes about 27.4 minutes. These are correctness/memory-capacity gates, not claims of fast or freeze-free generation. UV 1024 refers to atlas resolution and is verified separately with draft-512 and standard-1024 single-view geometry.

## Reproducible runtime fixes

Because `runtime/` is Git-ignored, each installed source correction has a tracked patch:

- `cumesh-large-mesh.patch` — 4 MiB input/output copies avoid the Windows HIP 12 MiB device-copy failure.
- `o-voxel-tiled-faces.patch` — centroid ownership and an inclusive outer boundary remove duplicate tiled faces without a global face sort.
- `flex-gemm-explicit-chunking.patch` — 512 MiB `im2col` chunks avoid the 23 GiB standard-1024 and 8.95 GiB UV allocation failures.
- `lscm-batched-solve.patch` — GPU LSCM solves in batches of 16 avoid the failing large `hipblasDgetrfBatched` call.
- `flex-gemm-weighted-sum.patch` — equivalent PyTorch GPU operations replace the unavailable Windows/Triton weighted-sum kernel.

## Hardware-runner reports

The hardware runners use seed `42` and a deterministic generated 256 × 256 RGBA test object. Their default backend remains `wsl`; these native runners explicitly select `windows-rocm`. Run GPU jobs one at a time from the repository root:

```powershell
node scripts/native/run_native_single512.mjs
node scripts/native/run_native_single1024.mjs
node scripts/native/run_native_uv1024.mjs
node scripts/native/run_native_standard_uv1024.mjs
```

They write `512-single.json`, `1024-single.json`, `512-single-uv1024.json`, and `1024-single-uv1024.json` under `data/runtime/diagnostics/native-amd/`, with GLBs under `data/runtime/`. `AMD_PIXAL3D_OUTPUT_DIR` controls downloaded artifacts in the generic runner. Native multi-view and UV 512 requests remain locked. Generation reports retain the input, seed, backend, runtime versions and workflow identity so comparisons remain reproducible.

Profile Standard 1024 + UV 1024 responsiveness in a separate process:

```powershell
node scripts/native/run_native_standard_uv1024_profile.mjs
```

The profiler uses ports `3214` and `8193`, preserves the original E2E report, and writes `1024-single-uv1024-profile.jsonl`, `1024-single-uv1024-profile.json`, and `1024-single-uv1024-profile-e2e.json`. It samples system CPU/RAM, native VRAM, job phase, and local studio heartbeat without external requests or production-code changes. The measured baseline had no studio heartbeat stall over 250 ms in 1,294 samples; three native `/system_stats` timeouts during surface rebuilding did not coincide with a studio API stall. This is not a graphics, driver, browser, or external-network freeze-free claim.

Verify real cancellation and post-run cleanup separately:

```powershell
node scripts/native/run_native_lifecycle.mjs
$env:AMD_PIXAL3D_LIFECYCLE_MODE = 'complete'
node scripts/native/run_native_lifecycle.mjs
Remove-Item Env:AMD_PIXAL3D_LIFECYCLE_MODE
```

The lifecycle runner uses isolated local ports `3212` and `8192`, writes `512-cancel.json` or `512-complete-cleanup.json`, and shuts down its studio and native runtime. It checks that the queue drains and that Torch reserved VRAM returns to within 768 MiB of the pre-generation baseline. Temporary telemetry timeouts are recorded without cancelling the model job.

## Tests

```powershell
node --test tests/pixal3d-native-installer.test.mjs
python -m unittest scripts/native/test_native_tools.py -v
& "$native/python_embeded/python.exe" -I scripts/native/test_flex_gemm_chunking.py
& "$native/python_embeded/python.exe" -I scripts/native/test_uv_lscm_batching.py
& "$native/python_embeded/python.exe" -I scripts/native/test_flex_gemm_weighted_sum.py
```

Do not run a large model installation after a failed HIP, SDPA or memory probe. Fix or report the exact failing operation first.
