# Native Windows AMD Pixal3D tools

These tools build and probe an **isolated native Windows ROCm runtime**. They do not install or replace GPU drivers, change WSL, modify the NVIDIA runtime, or request a reboot.

Current scope on RX 9070 XT: native single-view draft 512 shape, standard 1024 shape, draft 512 geometry with a 1024 × 1024 UV atlas, and standard 1024 geometry with a 1024 × 1024 UV atlas are verified. The approved four-view orc attempt has also produced a visually reviewed draft 512 shape without a texture atlas; this does not unlock generic multi-view UV. A separate texture-only baton experiment has also produced verified 2048 × 2048 and 4096 × 4096 base-color atlases without changing the 1024 denoiser or geometry; these larger atlases are not persistent production capabilities. Generic multi-view UV and UV 512 remain locked. The installer now covers the Python/PyTorch HIP foundation and three bundled HIP extensions; the complete ComfyUI/TRELLIS.2 runtime, model weights and `native-ready.json` production gate remain separate readiness requirements.

## Layout

- `installer/amd/Pixal3DNative.ps1` — isolated portable-Python, ROCm and HIP-extension installer plus hardware probes.
- `installer/amd/native-wheels.json` — exact package URLs, sizes and SHA-256 hashes for official and bundled packages.
- `installer/amd/wheels/` — bundled CuMesh, FlexGEMM and O-Voxel CPython 3.12 Windows HIP wheels.
- `installer/amd/licenses/` — license notices accompanying the bundled HIP wheels.
- `scripts/native/install_pixal3d_amd.py` — native-path and offline-wheel validation.
- `scripts/native/verify_pixal3d_amd.py` — fail-fast Python, HIP, GPU, matrix, SDPA and memory probes.
- `scripts/native/probe_trellis2_extensions.py` — functional sparse-SDPA, NAF, FlexGEMM, O-Voxel and CuMesh probes.
- `scripts/native/requirements-base.txt` — pinned base Python dependencies.
- `scripts/native/patches/` — reproducible source fixes for the Git-ignored native runtime.
- `runtime/ComfyUI-Pixal3D-AMD-Native/` — disposable native runtime; never shared with WSL or NVIDIA.
- `data/runtime/diagnostics/native-amd/` — hardware JSON reports and logs; runtime output is not source material.

The default runtime can be overridden only with an absolute native Windows path through `-RuntimeRoot` or `ASTRA_PIXAL3D_NATIVE_DIR`. The installer rejects relative paths, `/mnt/<drive>` paths and the existing `ComfyUI-Pixal3D` / `ComfyUI-Pixal3D-AMD` runtime directories.

## Verified packages

The installer downloads missing or corrupt official packages from the pinned `repo.radeon.com` URLs in `installer/amd/native-wheels.json` into `installer/downloads/amd/`. Every cached and downloaded file must match the manifest's exact byte size and SHA-256 before use:

- `rocm-7.2.1.tar.gz` (provides the `rocm_sdk` package);
- `rocm_sdk_core`, `rocm_sdk_devel`, `rocm_sdk_libraries_custom` 7.2.1;
- `torch` / `torchaudio` 2.9.1 and `torchvision` 0.24.1, all `+rocm7.2.1` CPython 3.12 Windows wheels.

The source meta-package is installed with `--no-build-isolation`. The three CuMesh, FlexGEMM and O-Voxel HIP wheels are bundled under `installer/amd/wheels/`, verified against the same manifest and installed offline with `--no-index --no-deps`. Missing packages, hash mismatches or CUDA wheels stop installation; there is no silent substitution.

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

# Preview the installation target without changing files.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 -DryRun
```

`-ReportPath` selects a different base JSON report. `-ForceProbe` reinstalls/revalidates the base stack even when `native-base-ready.json` exists. An existing valid base marker skips only base reinstallation; bundled extension installation and probing still run. `-SkipModels` is part of the installer contract; this stage does not install model weights either way.

Run either probe directly with the native interpreter when needed:

```powershell
& "$native/python_embeded/python.exe" -I scripts/native/verify_pixal3d_amd.py --json data/runtime/diagnostics/native-amd/base-probe.json
& "$native/python_embeded/python.exe" -I scripts/native/probe_trellis2_extensions.py --output data/runtime/diagnostics/native-amd/extensions-installer.json
```

The process exit code alone is not the readiness decision: inspect `nativeReady`, `shapeReady` and every mandatory check in the corresponding JSON. For the base report, `nativeReady: true` means **base HIP readiness only**. For the extension report, `shapeReady: true` means **functional HIP-extension readiness only**. The server separately requires full runtime sources, model weights and `native-ready.json`; each generation capability is enabled only after its own E2E gate. This installer never writes `native-ready.json`.

A passing base installation writes `native-base-ready.json` with the GPU, Torch/HIP versions, base report path and SHA-256. Its capability map remains all `false`. A passing extension stage writes `native-extensions-ready.json` with `shapeReady: true` and the extension report path and SHA-256. Neither marker unlocks production generation.

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
- native single-view standard 1024 geometry with a 1024 × 1024 UV atlas: pass in 1,641.2 seconds, with 335,153 vertices, 660,226 triangles and no fallback events;
- baton Standard 1024 geometry, denoiser 1024, exact 2048 × 2048 UV atlas: texture-only pass in 499.0 seconds, with 357,819 vertices, 662,201 triangles, a 22,138,764-byte GLB and no fallback events;
- the same baton with an exact 4096 × 4096 UV atlas: texture-only pass in 382.4 seconds, with unchanged geometry, a 37,962,708-byte GLB and no fallback events.

Reports: `data/runtime/diagnostics/native-amd/base-probe.json`, `cumesh-probe.log`, `o-voxel-tiled-probe.log`, `512-single.json`, `1024-single.json`, `512-single-uv1024.json`, `1024-single-uv1024.json`, `baton-uv2048.json`, and `baton-uv4096.json`.

The standard-1024 shape-only result takes about 15 minutes; standard 1024 with UV 1024 takes about 27.4 minutes. These are correctness/memory-capacity gates, not claims of fast or freeze-free generation. UV 1024 refers to atlas resolution and is verified separately with draft-512 and standard-1024 single-view geometry.

For the larger-atlas experiment, 2048 is the recommended size/quality balance and 4096 is the largest verified atlas for this baton. Four-view and atlas inspection found a small visible difference between them; backside/end stretching remains. VRAM telemetry was unavailable in these two runs, and their wall times must not be interpreted as proof that 4096 is faster. Production readiness and UV 1024 defaults remain unchanged.

## Reproducible runtime fixes

Because `runtime/` is Git-ignored, each installed source correction has a tracked patch:

- `cumesh-large-mesh.patch` — 4 MiB input/output copies avoid the Windows HIP 12 MiB device-copy failure.
- `o-voxel-tiled-faces.patch` — centroid ownership and an inclusive outer boundary remove duplicate tiled faces without a global face sort.
- `flex-gemm-explicit-chunking.patch` — 512 MiB `im2col` chunks avoid the 23 GiB standard-1024 and 8.95 GiB UV allocation failures.
- `lscm-batched-solve.patch` — GPU LSCM solves one chart per `hipblasDgetrfBatched` call; batch 16 still failed with `HIPBLAS_STATUS_ALLOC_FAILED` on the 119,999-triangle orc.
- `flex-gemm-weighted-sum.patch` — equivalent PyTorch GPU operations replace the unavailable Windows/Triton weighted-sum kernel.

## Hardware-runner reports

The hardware runners use seed `42` and a deterministic generated 256 × 256 RGBA test object. Their default backend remains `wsl`; these native runners explicitly select `windows-rocm`. Run GPU jobs one at a time from the repository root:

```powershell
node scripts/native/run_native_single512.mjs
node scripts/native/run_native_single1024.mjs
node scripts/native/run_native_uv1024.mjs
node scripts/native/run_native_standard_uv1024.mjs
```

They write `512-single.json`, `1024-single.json`, `512-single-uv1024.json`, and `1024-single-uv1024.json` under `data/runtime/diagnostics/native-amd/`, with GLBs under `data/runtime/`. `AMD_PIXAL3D_OUTPUT_DIR` controls downloaded artifacts in the generic runner. The dedicated reviewed orc workflow supports multi-view draft 512 shape; generic multi-view UV and UV 512 requests remain locked. Generation reports retain the input, seed, backend, runtime versions and workflow identity so comparisons remain reproducible.

Reproduce the isolated baton atlas experiment with `AMD_UV_SIZE=2048` or `4096`. This is a texture-only runner: it requires an existing accepted GLB, keeps denoiser resolution 1024, uses port 8194, and never starts the production studio. Run the sizes sequentially, not in parallel:

```powershell
$env:AMD_UV_SIZE = '2048' # repeat later with 4096 after inspecting the 2048 result
$env:AMD_UV_INPUT = Join-Path $PWD 'data/assets/250bcec2-20af-4899-b954-0d706238aa6b.png'
$env:AMD_UV_GLB = Join-Path $PWD 'data/runtime/baton/amd-pixal3d-native-single-standard-uv1024.glb'
node scripts/native/run_native_uv_atlas.mjs
```

Outputs are `data/runtime/baton/amd-pixal3d-native-single-uv<SIZE>.glb` and `data/runtime/diagnostics/native-amd/baton-uv<SIZE>.json`. The runner applies `pixal3dEnv()` so `HF_HOME` and `TORCH_HOME` select the native patched NAF cache rather than the user cache. It validates exact embedded PNG dimensions, stops only its own native process, and does not modify `native-ready.json` or expose a persistent production profile.

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

## Experimental local A-pose with reusable identity-lock

This experiment is separate from verified single-view capabilities and does not change `native-ready.json`. It requires the exact Qwen-Image-Edit-2509 Q4_K_M, FP8 Qwen2.5-VL text encoder, and Qwen VAE listed with sizes and SHA-256 hashes in `qwen-apose-assets.json`. Model files stay in the Git-ignored native runtime. Do not install drivers or substitute CUDA, WSL, CPU inference, Q3, or another editor.

Configuration is split into three independent inputs under `scripts/native/identity/`:

- `identity-lock.json`: shared invariants plus kind-specific character/building rules;
- `preset-orc.json` or another object preset: identity features, inventory, asymmetry and view notes;
- `negative-character.json`, `negative-building.json`, `negative-generic.json`, `negative-none.json` or a custom `--negative-file` JSON.

Verify assets and the no-GPU contracts:

```powershell
python -m unittest scripts/native/test_qwen_apose_assets.py -v
python scripts/native/verify_qwen_apose_assets.py --runtime-root runtime/ComfyUI-Pixal3D-AMD-Native
node --test tests/native-identity-config.test.mjs tests/native-attempt-store.test.mjs tests/native-qwen-apose.test.mjs tests/native-multiview-orc.test.mjs tests/native-attempt-integration.test.mjs
```

Run stages sequentially. Every generation creates a new `attempts/attempt-NNN` with resolved prompts, seed, source/output SHA-256, workflow, timing and a pending `review.json`; previous attempts are not overwritten.

```powershell
node scripts/native/run_native_qwen_apose.mjs --input data/runtime/orc/views/raw/front.png --preset scripts/native/identity/preset-orc.json --negative-profile character --output data/runtime/orc-identity-v2
```

Inspect `<apose-attempt>/comparison.jpg` against the source. Reject changed facial features, profile inventions, duplicated equipment or replacement of asymmetric wrist coverings/boots. Record a real human decision:

```powershell
node scripts/native/review_native_attempt.mjs --attempt "<apose-attempt>" --status rejected --comment "Changed nose, chin, boots or asymmetric equipment"
node scripts/native/review_native_attempt.mjs --attempt "<apose-attempt>" --status approved --comment "Checked face, tusks, hair, equipment sides, boots and duplicates against source"
```

Only an approved, unchanged A-pose attempt can generate views:

```powershell
node scripts/native/run_native_orc_multiview.mjs --stage views --input "<apose-attempt>" --preset scripts/native/identity/preset-orc.json --negative-profile character --output data/runtime/orc-views-v2 --coherent-front true
node scripts/native/review_native_attempt.mjs --attempt "<views-attempt>" --status approved --comment "Checked all four views, profile, equipment sides and duplicates"
```

Only approved views can enter shape. Texture remains a separate stage after shape validation:

```powershell
node scripts/native/run_native_orc_multiview.mjs --stage shape --views "<views-attempt>" --output data/runtime/orc-shape-v2 --json data/runtime/diagnostics/native-amd/orc-shape-v2.json
node scripts/native/run_native_orc_multiview.mjs --stage texture --profile light --views "<views-attempt>" --glb data/runtime/orc-shape-v2/amd-pixal3d-native-multiview-draft512.glb --output data/runtime/orc-shape-v2 --json data/runtime/diagnostics/native-amd/orc-texture-v2-light.json
```

The A-pose runner uses port 8196; the multi-view runner uses 8195. Both stop only their own process trees and request memory cleanup. `--coherent-front true` selects the MV-Adapter front from the same batch. A pending/rejected/missing review or any artifact hash change blocks the next stage before ComfyUI starts.

Current status on 2026-11-04: the earlier Q4_K_M A-pose completed in 1865.4 seconds and MV-Adapter completed in 46 seconds, but visual inspection rejected them for facial redesign, a false profile wedge and symmetric replacement of distinct equipment. The revised `attempt-002` A-pose completed in 2081 seconds and was explicitly approved by the user as a working result despite its simplified equipment. The next required improvement is a preservation mask for the pauldron, distinct wrist coverings, boots and defining features: only the body pose may change inside the editable region, while protected source elements must not be repainted. The reusable resolver, immutable attempts, review CLI and approved-stage barriers are implemented and unit-tested.

## Masked belt repair for orc views

The repair runner preserves `orc-views-v2/attempts/attempt-002` outside explicit waist/artifact masks. It never runs shape or texture and always creates a separate attempt under `orc-views-repair`.

Prepare and inspect masks without GPU or a system-RAM barrier:

```powershell
node scripts/native/run_native_belt_repair.mjs --input data/runtime/orc-views-v2/attempts/attempt-002 --output data/runtime/orc-views-repair --prepare-only true
```

A full donor run requires at least 12 GiB free system RAM. The guard runs before creating an attempt or starting ComfyUI:

```powershell
node scripts/native/run_native_belt_repair.mjs --input data/runtime/orc-views-v2/attempts/attempt-002 --output data/runtime/orc-views-repair --debug true
```

The runner rejects byte-identical donor views before compositing. The shortened prompt keeps the belt instructions inside the CLIP limit, and MV-Adapter cleanup no longer moves the whole pipeline back to CPU RAM. `attempt-003` produced RGB noise and was rejected. Controlled `attempt-004` completed in 84.8 seconds with four distinct views and no monitored engine stalls over 250 ms, but RAM again peaked at 31.83 GB. Front/back are coherent and pixels outside masks are preserved; its side views still had rigid waist protrusions.

For CPU-only cleanup of those protrusions, `belt-cleanup-orc.json` enables `backgroundOnly`: no donor is read or reapplied, front/back remain byte-identical, and only `backgroundMasks` on left/right are replaced with the source background color. `backgroundFeatherPx` is independent from the donor `featherPx`. Reproduce the composite stage with an existing prepared attempt:

```powershell
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/belt_repair.py composite --source data/runtime/orc-views-repair/attempts/attempt-004/views --donor data/runtime/orc-views-repair/attempts/attempt-004/donor-views --output "<new-cleanup-attempt>" --config scripts/native/identity/belt-cleanup-orc.json
```

`orc-manual-edit/attempts/attempt-001` is approved: the user edited only `left.png` in Paint and then accepted all four views. All images remain 512×512, front/back/right are unchanged from cleanup attempt-005, and original/edited SHA-256 hashes are recorded in the success manifest. Draft 512 shape completed in 266.1 seconds: `data/runtime/orc-shape-manual/amd-pixal3d-native-multiview-draft512.glb` is 12,599,596 bytes with 351,538 vertices and 698,367 triangles, accepted technical validation and no fallback events. The original GLB was loaded into the `orc-shape-review` Edit3D project and visually inspected from four views; it has no UV texture or vertex colors, and artistic acceptance is still separate. Texture has not run. Report: `data/runtime/diagnostics/native-amd/orc-shape-manual.json`.

## Tests

```powershell
node --test tests/pixal3d-native-installer.test.mjs
node --test tests/native-uv-atlas.test.mjs tests/pixal3d-native.test.mjs tests/pixal3d.test.mjs
python -m unittest scripts/native/test_native_tools.py -v
& "$native/python_embeded/python.exe" -I scripts/native/test_flex_gemm_chunking.py
& "$native/python_embeded/python.exe" -I scripts/native/test_uv_lscm_batching.py
& "$native/python_embeded/python.exe" -I scripts/native/test_flex_gemm_weighted_sum.py
```

Do not run a large model installation after a failed HIP, SDPA or memory probe. Fix or report the exact failing operation first.
