# Native AMD Pixal3D Implementation Plan

> **For agentic workers:** Execute this plan in the current session. Do not use subagents. Use test-driven development, keep each task independently reviewable, and stop at hardware or numerical-parity blockers rather than hiding them behind fake adapters.

**Goal:** Run Pixal3D/TRELLIS.2 natively on Windows with AMD ROCm on RX 9070 XT, without WSL in the generation path, while preserving the existing ComfyUI API, Web UI, GLB output, and WSL fallback.

**Architecture:** Add an explicit `windows-rocm` backend beside the existing NVIDIA `windows` and AMD `wsl` backends. A small backend registry exposes capabilities and replaceable per-GPU profiles; it is not a universal GPU abstraction. Native Windows Python/PyTorch runs the same workflows through the existing ComfyUI HTTP/WebSocket runner. Custom TRELLIS.2 extensions are admitted individually only after import, smoke, and numerical-parity tests. The WebGL preview gets a separate adaptive frame budget so compute work does not freeze interaction.

**Tech Stack:** Windows 11, Node.js, PowerShell, native Windows Python, PyTorch with ROCm/HIP, ComfyUI, TRELLIS.2, Python numerical tests, Node test runner, WebGL/Three.js, Playwright or the existing browser E2E harness.

**Primary hardware target:** Radeon RX 9070 XT, `gfx1201`, 16 GB VRAM, single-view 1024 shape generation. Multi-view 1024 and 1024 UV texturing remain locked until separately measured.

**Global constraints**

- Do not install, replace, or downgrade GPU drivers as part of this work. Do not change BIOS, global GPU scheduling, or system power settings. No reboot is required by this plan.
- Do not overwrite the existing WSL runtime, NVIDIA runtime, model cache, or generated recovery artifacts.
- Do not use `/mnt/c`, `wsl.exe`, Linux wheels, or Linux shell scripts in the native execution path.
- Do not ship dummy DLLs, fake extension modules, silent CPU fallback, or unconditional patches for unsupported GPUs.
- Preserve the recovery commit `0c682804bf8ce10089550eae995a244ed96f82d4` and tag `amd-rocm-recovery-2026-11-03`.
- Keep `wsl` selectable until all native acceptance gates pass. Backend rollback must not require reinstalling either runtime.
- Treat every custom extension as unverified until its exact Windows wheel/build and GPU architecture pass the probes.
- Never promise a speedup before native measurements. The WSL full-1024 reference is 842.0 s and 15,301.30 MB peak memory for job `7edaa31c-8155-4d7c-977d-dc5839510761`.
- Keep model generation and preview-rendering metrics separate. A 30 fps adaptive preview is acceptable during generation; UI input must remain responsive.
- Put logs and generated diagnostics under `data/runtime/diagnostics/native-amd/`; do not commit large models, wheels, generated GLBs, or runtime logs.

---

## Task 0: Freeze the Recovery Baseline and Add Native Telemetry

**Files:**

- Modify: `docs/forge/pixal3d.md`
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs`
- Modify: `server/pixal3d-run.mjs`
- Test: `tests/pixal3d-run.test.mjs`
- Create: `scripts/native/README.md`

**Interfaces:**

Preserve the existing runner result and add optional fields without changing current callers:

```js
{
  backend: 'wsl' | 'windows' | 'windows-rocm',
  gpu: { name, architecture, vramBytes },
  phases: [{ name, startedAt, endedAt, durationMs, peakMemoryBytes }],
  fallbackEvents: [{ operation, reason }],
  output: { path, vertices, triangles }
}
```

**Steps:**

1. Verify the clean recovery point and record the existing WSL draft/full figures in `docs/forge/pixal3d.md`: full single 1024 = 842.0 s / 15,301.30 MB; draft single = 186.3 s / 10,048.65 MB; draft multi = 184.0 s / 10,175.95 MB.

2. Write a failing runner test proving that omitted telemetry remains backward-compatible and that supplied phase timings and fallback events are serialized.

   Run:

   ```powershell
   node --test tests/pixal3d-run.test.mjs
   ```

   Expected: the new telemetry assertions fail before implementation.

3. Implement additive telemetry in `server/pixal3d-run.mjs`. Reuse the existing job, progress, mutex, history, download, and validation flow. Do not fork the ComfyUI protocol implementation.

4. Extend `scripts/verify_amd_pixal3d_e2e.mjs` with `--backend=wsl|windows-rocm`, `--output=<dir>`, and `--json=<file>`. Default to `wsl` so existing automation remains unchanged. Reject `windows-rocm` until Task 4 marks the native runtime ready.

5. Document probe commands and result layout in `scripts/native/README.md`. Include the exact seed, input image, workflow hash, backend, GPU name, architecture, and versions in every hardware report.

6. Run:

   ```powershell
   node --test tests/pixal3d-run.test.mjs
   npm test
   ```

   Expected: all tests pass; existing WSL invocation and result consumers are unchanged.

7. Commit only the source, tests, and documentation:

   ```powershell
   git add server/pixal3d-run.mjs scripts/verify_amd_pixal3d_e2e.mjs tests/pixal3d-run.test.mjs docs/forge/pixal3d.md scripts/native/README.md
   git commit -m "Add backend telemetry for native Pixal3D"
   ```

---

## Task 1: Add an Explicit, Replaceable AMD Backend

**Files:**

- Create: `server/pixal3d-backends.mjs`
- Create: `server/pixal3d-native.mjs`
- Create: `tests/pixal3d-backends.test.mjs`
- Create: `tests/pixal3d-native.test.mjs`
- Modify: `server/pixal3d.mjs`
- Modify: `server/schema.mjs`
- Modify: `tests/pixal3d-schema.test.mjs` if present; otherwise add schema cases to `tests/pixal3d-backends.test.mjs`
- Modify: `docs/forge/pixal3d.md`

**Interfaces:**

```js
export function getPixal3DBackend(id, env = process.env) {
  // Returns a frozen descriptor or throws a user-readable configuration error.
}

export function listPixal3DBackends() {
  // Always includes windows, wsl, and windows-rocm.
}
```

Backend descriptor:

```js
{
  id: 'windows-rocm',
  runtimeRoot: '<absolute native Windows path>',
  pythonPath: '<absolute native python.exe>',
  capabilities: {
    single512: false,
    single1024: false,
    multi512: false,
    multi1024: false,
    uvTexture512: false,
    uvTexture1024: false
  },
  memoryPolicy: { minFree1024Bytes: 11 * 1024 ** 3 },
  qualityProfile: 'balanced-1024',
  attention: 'sdpa'
}
```

Native launcher:

```js
export function buildNativePixal3DLaunch({ config, descriptor }) {
  // Returns { command, args, cwd, env } using native Windows paths only.
}
```

**Steps:**

1. Write failing tests for:

   - `windows` and `wsl` descriptors retaining their current meaning.
   - `windows-rocm` using `runtime/ComfyUI-Pixal3D-AMD-Native` by default and a configurable absolute override.
   - Native launch arguments containing no `wsl.exe`, `/mnt/c`, or Linux Python.
   - SDPA being selected for native AMD rather than `flash_attn`.
   - Multi-view 1024 and 1024 texturing remaining disabled by default.
   - Missing native readiness/capability data producing a clear error instead of a WSL fallback.
   - Unknown GPU architecture failing closed with a supported-GPU message.

2. Run:

   ```powershell
   node --test tests/pixal3d-backends.test.mjs tests/pixal3d-native.test.mjs
   ```

   Expected: failures because the native descriptor and launcher do not exist.

3. Implement `server/pixal3d-backends.mjs` as a narrow registry, not a general driver framework. Keep GPU-specific values in profiles keyed by `gfx1201`, `gfx1100`, `gfx1200`, and `gfx1101`; only `gfx1201` is enabled initially.

4. Implement `server/pixal3d-native.mjs` with native path validation and process launch construction. Reuse the existing job runner and GPU handover. The native runtime readiness file must be runtime-local and must name the probe-report hash.

5. Modify `server/pixal3d.mjs` to dispatch the new backend without changing default backend selection. Modify `server/schema.mjs` so native AMD uses SDPA and capability checks are driven by the selected descriptor.

6. Run:

   ```powershell
   node --test tests/pixal3d-backends.test.mjs tests/pixal3d-native.test.mjs tests/pixal3d-schema.test.mjs
   npm test
   ```

   If the schema test file does not exist, run only the existing test files plus the new ones; do not create an empty command failure.

7. Document backend selection and rollback. Commit:

   ```powershell
   git commit -am "Add explicit native AMD Pixal3D backend"
   ```

---

## Task 2: Build an Isolated Native Runtime and Fail-Fast Probes

**Files:**

- Create: `installer/amd/Pixal3DNative.ps1`
- Create: `scripts/native/install_pixal3d_amd.py`
- Create: `scripts/native/verify_pixal3d_amd.py`
- Create: `tests/pixal3d-native-installer.test.mjs`
- Modify: `installer/amd/AmdCore.ps1` only if GPU architecture detection is not already exposed
- Modify: `scripts/native/README.md`
- Create: `docs/forge/pixal3d-native-amd.md`

**Installer contract:**

```powershell
powershell -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 `
  -RuntimeRoot "<absolute-path>\runtime\ComfyUI-Pixal3D-AMD-Native" `
  -ProbeOnly
```

Parameters: `-RuntimeRoot`, `-ProbeOnly`, `-SkipModels`, `-ForceProbe`. No driver-install, reboot, WSL-install, or global Python options.

**Probe JSON contract:**

```json
{
  "schemaVersion": 1,
  "platform": "Windows",
  "gpu": { "name": "", "architecture": "", "vramBytes": 0 },
  "torch": { "version": "", "hip": "" },
  "checks": [{ "name": "", "status": "pass|fail|unsupported", "details": "" }],
  "nativeReady": false
}
```

**Steps:**

1. Write installer tests using temporary directories and mocked command execution. Cover path isolation, repeated installation, probe-only mode, missing wheel, wrong architecture, non-Windows rejection, and preservation of unrelated runtimes.

2. Run:

   ```powershell
   node --test tests/pixal3d-native-installer.test.mjs
   ```

   Expected: the new installer contract tests fail before implementation.

3. Implement the isolated native installer. Reuse the already identified ROCm 7.2.1/PyTorch 2.9.1 Windows wheel URLs and `Select-AmdGpu`; do not query or replace AMD drivers. Install into the native runtime only.

4. Implement ordered probes:

   - Native interpreter and absolute path validation.
   - `torch.cuda.is_available()` through the ROCm PyTorch API, HIP version, GPU name, `gfx` architecture, and VRAM.
   - FP32/FP16/BF16 matrix multiplication and reduction.
   - SDPA forward and backward on small tensors.
   - Model-weight loading from a native Windows cache.
   - Memory allocation/free and process-exit cleanup.

5. Each probe must return a structured failure. A failed mandatory check leaves `nativeReady=false`; the launcher must not start generation.

6. Run installer tests and the probe on the RX 9070 XT:

   ```powershell
   node --test tests/pixal3d-native-installer.test.mjs
   powershell -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 -RuntimeRoot "<absolute-native-runtime>" -ProbeOnly
   python scripts/native/verify_pixal3d_amd.py --runtime "<absolute-native-runtime>" --json "data/runtime/diagnostics/native-amd/base-probe.json"
   ```

   Use the native runtime's `python.exe` for the second command if the managed Python cannot import the native environment. Record the actual command in the report.

7. Stop this task if HIP, SDPA, or memory probes fail. Report the exact failing operation and version; do not proceed to a large model installation by retrying blindly.

8. Commit the installer, probes, tests, and measured documentation:

   ```powershell
   git commit -am "Add isolated native AMD runtime probes"
   ```

---

## Task 3: Prove Every TRELLIS.2 Extension Before Full Generation

**Files:**

- Create: `scripts/native/probe_trellis2_extensions.py`
- Create: `scripts/native/patches/trellis2_amd/__init__.py`
- Create: `scripts/native/patches/trellis2_amd/attention.py`
- Create: `scripts/native/patches/trellis2_amd/geometry.py`
- Create: `scripts/native/patches/trellis2_amd/texture.py`
- Create: `scripts/native/test_trellis2_parity.py`
- Modify: `scripts/native/verify_pixal3d_amd.py`
- Modify: `docs/forge/pixal3d-native-amd.md`

**Required operations:**

- FlexGEMM / Triton-dependent paths.
- O-Voxel sparse operations.
- CuMesh geometry operations.
- Attention, including any NATTEN-dependent path.
- NAF fallback used by the AMD patch.
- `nvdiffrast`-dependent rasterization and the existing CPU/PyTorch UV fallback.

**Steps:**

1. Write small deterministic tests with CPU references before porting the existing Linux patch. Test forward results, gradients where training code calls them, empty/small inputs, non-contiguous tensors, and memory cleanup.

2. Use these starting numerical gates, documenting any kernel-specific justified exception:

   - FP32 dense/reference comparison: `rtol=1e-4`, `atol=1e-5`.
   - FP16/BF16 kernels: `rtol=2e-2`, `atol=2e-2`, plus finite-output and shape checks.
   - Geometry: no NaN/Inf, valid indices, deterministic topology for fixed input, and normalized geometric error reported against CPU/reference output.
   - UV output: non-empty UVs, finite coordinates, valid face corners, and a deterministic small-texture checksum against the approved reference path.

3. Run the probes separately for every extension. Record `pass`, `fail`, or `unsupported`; never convert an import failure into success.

   ```powershell
   python scripts/native/probe_trellis2_extensions.py --runtime "<absolute-native-runtime>" --json "data/runtime/diagnostics/native-amd/extensions.json"
   python -m pytest scripts/native/test_trellis2_parity.py -q
   ```

   Again, use the native runtime interpreter when required.

4. Port the relevant logic from `scripts/wsl/patch_trellis2_amd.py` into narrow versioned native modules. Keep SDPA and bounded NAF fallback. Do not copy Linux Triton assumptions, Linux wheel URLs, or implicit FlexGEMM behavior without a probe.

5. For each unavailable extension, choose exactly one evidence-based route:

   - Use a compatible native Windows HIP build that passes parity.
   - Use a bounded PyTorch fallback that passes parity and has an explicit performance report.
   - Disable only the dependent capability and leave a user-readable blocker.

6. Keep UV 512 disabled until its native path passes both numerical and end-to-end texture tests. Keep UV 1024 disabled regardless of import success until Task 5 hardware gates pass.

7. Update `nativeReady` only when all shape-generation mandatory checks pass. Extension success alone must not unlock 1024.

8. Commit:

   ```powershell
   git commit -am "Validate native TRELLIS2 AMD extensions"
   ```

---

## Task 4: Complete the First Native 512 Generation

**Files:**

- Modify: `server/pixal3d-native.mjs`
- Modify: `server/pixal3d-backends.mjs`
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs`
- Modify: `tests/pixal3d-native.test.mjs`
- Modify: `docs/forge/pixal3d-native-amd.md`

**Steps:**

1. Add failing tests proving that `single512` remains unavailable when readiness or any mandatory extension probe fails, and becomes available only with a matching runtime/GPU probe hash.

2. Run the Node tests, then implement the capability unlock. Do not infer readiness from directory existence.

3. Generate one deterministic single-view 512 job with the same input and seed as the WSL baseline:

   ```powershell
   node scripts/verify_amd_pixal3d_e2e.mjs --backend=windows-rocm --resolution=512 --views=single --json "data/runtime/diagnostics/native-amd/512-single.json"
   ```

4. Verify all of the following:

   - No `wsl.exe` process is started by the job.
   - No `/mnt/c` path appears in command lines or logs.
   - The ComfyUI job uses the existing progress/history/download/validation path.
   - GLB has finite vertices, valid indices, non-zero triangles, and opens in the existing preview.
   - No silent CPU execution or unapproved fallback event occurs.
   - Native Python exits and owned VRAM is released after completion or cancellation.

5. Compare four rendered views and geometry statistics with the WSL baseline. Record phase timings, memory, and differences rather than claiming visual parity from a single thumbnail.

6. Run cancellation and failure-injection tests: disconnect during generation, invalid model file, OOM, and process termination. Each must release the GPU mutex and leave WSL usable.

7. Run:

   ```powershell
   node --test tests/pixal3d-native.test.mjs tests/pixal3d-backends.test.mjs
   npm test
   ```

8. Enable native `single512` only after hardware validation. Commit:

   ```powershell
   git commit -am "Run native AMD Pixal3D 512 generation"
   ```

---

## Task 5: Deliver and Validate Balanced Single-View 1024

**Files:**

- Create: `server/pixal3d-quality-profiles.mjs`
- Create: `tests/pixal3d-quality-profiles.test.mjs`
- Modify: `server/pixal3d.mjs`
- Modify: `server/pixal3d-backends.mjs`
- Modify: `server/schema.mjs`
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs`
- Modify: `docs/forge/pixal3d.md`
- Modify: `docs/forge/pixal3d-native-amd.md`

**Profile contract:**

```js
{
  id: 'balanced-1024',
  resolution: 1024,
  views: 'single',
  texture: false,
  samplers: { sparse: 'heun', shape: 'heun', slat: 'heun' },
  steps: { sparse: 25, shape: 12, slat: 12 },
  memoryPolicy: { minFreeBytes: 11 * 1024 ** 3 }
}
```

The initial profile preserves the existing Heun 25/12/12 behavior. Any shorter-step candidate is a separate profile and cannot replace the default without quality review.

**Steps:**

1. Write failing tests for profile immutability, single-view-only enforcement, free-VRAM checks, disabled 1024 texturing/multi-view, and backend rollback.

2. Implement the profile and expose `Balanced 1024` in the existing UI/API only after the native backend reports readiness.

3. Run three consecutive deterministic native single-view 1024 jobs without restarting AstraForge:

   ```powershell
   node scripts/verify_amd_pixal3d_e2e.mjs --backend=windows-rocm --resolution=1024 --views=single --repeat=3 --json "data/runtime/diagnostics/native-amd/1024-balanced-x3.json"
   ```

4. Acceptance gates on RX 9070 XT:

   - Three valid GLBs and three clean process exits.
   - No driver reset, system freeze, WSL launch, or shared-memory fallback used to conceal VRAM exhaustion.
   - Peak dedicated VRAM remains within the 16 GB device budget with at least 512 MiB measured headroom.
   - No progressive memory growth between runs beyond measurement noise; explain any increase over 256 MiB.
   - Median native total time is no more than 926.2 s (WSL 842.0 s + 10%) for the first performance gate.
   - Four-view quality review shows no material geometry/material regression against the WSL reference.

5. If the performance gate fails, optimize the slowest measured phase first: extension dispatch, attention, host/device copies, sampler loops, or export. Do not reduce quality to make the timing pass.

6. Benchmark separate shorter-step candidates only after the baseline profile passes. Preserve the default unless the candidate passes the same geometry, four-view, stability, and memory gates.

7. Keep multi-view 1024 and UV 1024 locked. UV 512 can be unlocked only after its own native E2E output is visually inspected and validated.

8. Run all automated tests and commit:

   ```powershell
   npm test
   git commit -am "Add validated balanced native 1024 profile"
   ```

---

## Task 6: Remove Preview Freezes Without Lowering Generation Quality

**Files:**

- Create: `src/render-budget.js`
- Create: `tests/render-budget.test.mjs`
- Modify: `src/scene.js`
- Modify: `src/main.js` or the existing generation-state owner
- Create: `scripts/measure_preview_responsiveness.mjs`
- Modify: `docs/forge/pixal3d-native-amd.md`

**Interface:**

```js
export class RenderBudget {
  constructor({ now, maxDpr = 2, minDpr = 0.75, computeFps = 30 });
  setComputeActive(active);
  shouldRender(timestamp);
  recordFrame(timestamp);
  getDpr();
  reset();
}
```

**Steps:**

1. Write deterministic fake-clock tests for 30 fps frame capping during compute, normal interactive rendering when idle, DPR decrease after sustained slow frames, hysteresis before DPR increase, resize behavior, tab resume, and reset after generation.

2. Run:

   ```powershell
   node --test tests/render-budget.test.mjs
   ```

   Expected: failure before the frame-budget module exists.

3. Implement adaptive rendering in `src/scene.js` without changing scene geometry, generated textures, camera controls, or export resolution. Reduce only preview DPR/frame rate during compute.

4. Keep event handling, progress messages, and job state independent of rendering. Move avoidable synchronous export/validation work off the critical UI turn or split it into bounded chunks.

5. Add a hardware/browser benchmark that records:

   - Preview frame-time p50/p95/p99.
   - Long tasks over 50 ms and worst long-task duration.
   - Input-to-next-paint latency for orbit/zoom and UI controls.
   - DPR changes and generation phase timings.

6. Acceptance targets during native 1024 generation:

   - No browser tab or system freeze.
   - Orbit/zoom and controls remain usable; input-to-next-paint p95 is at most 100 ms.
   - No recurring main-thread task over 200 ms.
   - Adaptive preview is capped at 30 fps under contention and returns to normal interactive mode after compute.
   - Generation timing and GLB content are unchanged by preview quality adaptation.

7. Run automated tests and the browser benchmark, save measurements, then commit:

   ```powershell
   npm test
   node scripts/measure_preview_responsiveness.mjs --backend=windows-rocm --json "data/runtime/diagnostics/native-amd/ui-budget.json"
   git commit -am "Keep preview responsive during native generation"
   ```

---

## Task 7: Remove Owned WSL Overhead and Prove Rollback

**Files:**

- Create: `server/pixal3d-wsl-lifecycle.mjs`
- Create: `tests/pixal3d-wsl-lifecycle.test.mjs`
- Modify: `server/pixal3d.mjs`
- Modify: `installer/amd/Pixal3DAmd.ps1` only to expose lifecycle status, not to remove the fallback
- Modify: `docs/forge/pixal3d-native-amd.md`

**Steps:**

1. Write tests with mocked process inspection for: native jobs never launching WSL, project-owned distro detection, refusal to stop unrelated WSL instances, active-job refusal, cleanup after native exit, and one-command backend rollback.

2. Implement narrowly scoped lifecycle helpers. Never run global `wsl --shutdown`. Stop only the exact project-owned distro/process when it is idle, the native backend is selected, and native E2E has passed.

3. Measure idle RAM and CPU before and after native migration using the same workload-free window. Confirm that the WSL generation path contributes no continuing CPU usage when native is active.

4. Verify backend switching in both directions without reinstalling models or deleting either runtime. A failed native probe must produce an actionable message; it must not silently launch WSL.

5. Confirm VRAM/RAM release after normal completion, cancellation, and backend switch. Preserve all recovery artifacts.

6. Run tests and commit:

   ```powershell
   node --test tests/pixal3d-wsl-lifecycle.test.mjs
   npm test
   git commit -am "Retire project WSL overhead for native backend"
   ```

---

## Task 8: Validate the Support Matrix and Publish Measured Limits

**Files:**

- Create: `server/pixal3d-amd-profiles.mjs`
- Create: `tests/pixal3d-amd-profiles.test.mjs`
- Modify: `server/pixal3d-backends.mjs`
- Modify: `installer/amd/Pixal3DNative.ps1`
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `docs/forge/pixal3d.md`

**Initial profile policy:**

| Architecture | Official Windows ROCm GPU family | Native profile policy |
|---|---|---|
| `gfx1201` | RX 9070, RX 9070 XT, Radeon AI PRO R9700 | RX 9070 XT is the tested target; other cards remain probe-gated |
| `gfx1100` | RX 7900 XTX, Radeon PRO W7900, W7900 Dual Slot | Experimental until the exact card passes extension and E2E probes |
| `gfx1200` | RX 9060 XT | Experimental; 16 GB variant only for low-VRAM experiments |
| `gfx1101` | RX 7700 | Experimental; do not promise comfortable 1024 |

Do not advertise native support for RX 7800 XT, RX 7900 XT/GRE, W7800, or RX 6000 solely because a Linux ROCm path exists. Larger-memory R9700, RX 7900 XTX, and W7900 are candidates for additional headroom, not validated multi-view devices.

**Steps:**

1. Test that every non-`gfx1201` profile starts experimental and cannot unlock a capability from GPU name alone.

2. Keep capability reports per exact GPU, driver-independent runtime version, extension set, and probe hash. A passing RX 9070 XT result must not be copied to another card.

3. Document the complete support matrix, known blockers, native/WSL selection, recovery tag, quality limits, and all measured timings.

4. Run final validation on RX 9070 XT:

   ```powershell
   npm test
   node --test tests/pixal3d-backends.test.mjs tests/pixal3d-native.test.mjs tests/pixal3d-native-installer.test.mjs tests/pixal3d-quality-profiles.test.mjs tests/render-budget.test.mjs tests/pixal3d-wsl-lifecycle.test.mjs tests/pixal3d-amd-profiles.test.mjs
   node scripts/verify_amd_pixal3d_e2e.mjs --backend=windows-rocm --resolution=1024 --views=single --repeat=3 --json "data/runtime/diagnostics/native-amd/final-1024-x3.json"
   node scripts/verify_amd_pixal3d_e2e.mjs --backend=wsl --resolution=512 --views=single --json "data/runtime/diagnostics/native-amd/final-wsl-rollback.json"
   ```

5. Confirm that existing NVIDIA `windows` and AMD `wsl` tests remain green, that the native runtime has no Linux-path dependency, and that no driver or reboot action was introduced.

6. Commit the final source and documentation, excluding runtime artifacts:

   ```powershell
   git commit -am "Document validated native AMD support matrix"
   ```

---

## Final Acceptance Criteria

The native port is complete only when all statements below are supported by saved measurements:

- RX 9070 XT completes three consecutive native single-view 1024 generations within its 16 GB VRAM budget, with valid GLBs, clean exits, and no WSL/Linux path.
- Native median generation time is no slower than the WSL baseline by more than 10% at the first gate; subsequent optimization is based on phase timings.
- No silent CPU fallback, driver reset, system freeze, or progressive memory leak occurs.
- UI controls remain responsive during generation; adaptive preview measurements meet Task 6 targets.
- Project-owned WSL processes are not running during native operation and owned RAM/VRAM is released after jobs.
- WSL rollback works without reinstalling, and existing NVIDIA behavior remains intact.
- `Balanced 1024` preserves the approved generation quality. Multi-view 1024 and 1024 texturing remain disabled until separately validated.
- All Node, installer, Python parity, hardware E2E, cancellation, and rollback tests pass.

## Explicitly Out of Scope

- A universal AMD/NVIDIA/Intel compute driver.
- DirectML as the primary engine.
- Vulkan as a drop-in replacement for the full TRELLIS.2 pipeline.
- GPU driver installation, BIOS changes, global WSL shutdown, or forced reboot.
- Promising support for untested AMD cards or unlocking multi-view/texture modes from theoretical VRAM capacity alone.
