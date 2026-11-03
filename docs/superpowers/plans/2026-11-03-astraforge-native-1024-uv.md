# Native AMD 1024 and UV 1024 Implementation Plan

> **For agentic workers:** Execute inline in the current session. Do not use subagents. Use test-driven development and checkbox tracking. Do not commit or push without a separate user request.

**Goal:** Optimize tiled O-Voxel decode and validate separate native single-view shape-1024 and draft-512/UV-atlas-1024 workflows on RX 9070 XT while avoiding measurable AstraForge-induced micro-freezes.

**Architecture:** Keep the existing native ComfyUI/TRELLIS.2 runtime and studio job protocol. Make narrow workflow and validation changes, use isolated hardware runners and runtime-local temporary capabilities, and persist only capabilities backed by saved E2E evidence.

**Tech Stack:** Windows 11, Python 3.12, PyTorch 2.9.1 ROCm 7.2.1, O-Voxel, CuMesh, ComfyUI, Node.js test runner, GLB/PNG validation.

## Global Constraints

- Do not install or replace drivers, change Windows priorities, reboot, remove WSL or modify the NVIDIA runtime.
- No hidden CPU, WSL, shape-only or vertex-color fallback. GPU hardware tests run sequentially.
- Shape 1024 preserves standard single-view Heun 25/12/12 and existing target faces.
- UV 1024 means a 1024 × 1024 atlas on draft-512 single-view geometry; it does not imply standard-1024 geometry.
- Multi-view 1024 and combined standard-1024 geometry with UV remain disabled.
- Persistent capabilities change only after technical and visual acceptance. Runtime artifacts and logs remain Git-ignored.
- Do not run the broad server suite or browser stress tests during hardware validation.

---

### Task 1: Profile and optimize tiled O-Voxel without topology changes

**Files:**
- Create: `scripts/native/profile_o_voxel_tiles.py`
- Modify: `runtime/ComfyUI-Pixal3D-AMD-Native/src/TRELLIS.2/o-voxel/o_voxel/convert/flexible_dual_grid.py`
- Synchronize: `runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/Lib/site-packages/o_voxel/convert/flexible_dual_grid.py`
- Modify: `scripts/native/patches/o-voxel-tiled-faces.patch`
- Test: `scripts/native/probe_trellis2_extensions.py`

**Interfaces:**
- Consumes: `tiled_flexible_dual_grid_to_mesh(coords, dual_vertices, intersected_flag, split_weight, aabb, grid_size, tile_size=128, overlap=1, train=False, verbose=False)`.
- Produces: unchanged return contract `(unique_vertices, faces)` and a JSON profiling report under `data/runtime/diagnostics/native-amd/`.

- [ ] **Step 1: Write a deterministic profiler with CUDA events**

Create a standalone script that builds a non-trivial sparse grid, calls the existing function and records total wall time plus PyTorch operation profiling. Use `torch.profiler.profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA])`, fixed seed `1204`, and `os._exit(0)` after flushing JSON so short ROCm probes do not hang during interpreter shutdown.

- [ ] **Step 2: Run the profiler and identify the dominant added operation**

Run:

```bash
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/profile_o_voxel_tiles.py --json data/runtime/diagnostics/native-amd/o-voxel-profile-before.json
```

Expected: exit 0 and a report containing total milliseconds and the top operations. Identify whether centroid gather, mean, boolean indexing, final `torch.unique`, host synchronization or another operation dominates.

- [ ] **Step 3: Preserve the regression gate before optimization**

Run:

```bash
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I -c "import runpy,os; ns=runpy.run_path('scripts/native/probe_trellis2_extensions.py'); print(ns['probe_o_voxel'](),flush=True); os._exit(0)"
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I ../verify_o_voxel_tiles.py
```

Expected: cube `faces=12`, `directTiledTopologyMatch=True`; all three additional cases have `match=True`.

- [ ] **Step 4: Make one measured minimal optimization**

If centroid division is material, remove per-face division by comparing summed vertices against scaled bounds:

```python
centers = t_verts[t_faces].sum(dim=1)
tile_min = (aabb_tensor[0] + lower * voxel_size) * 3
tile_max = (aabb_tensor[0] + upper * voxel_size) * 3
inside = (centers >= tile_min) & (
    (centers < tile_max) | ((upper > max_coord) & (centers <= tile_max))
)
t_faces = t_faces[inside.all(dim=1)]
```

If profiling instead identifies final deduplication or synchronization as dominant, retain centroid ownership and change only that measured stage. Do not combine multiple speculative optimizations.

- [ ] **Step 5: Verify topology and measure the same profiler workload**

Run the commands from Steps 2 and 3, writing `o-voxel-profile-after.json`. Require identical topology results. Compare like-for-like timings and retain the change only if it does not regress correctness; report repeated measurements if the difference is within run-to-run noise.

- [ ] **Step 6: Synchronize and validate the portable patch**

Run `sha256sum` on source and installed copies. Regenerate `o-voxel-tiled-faces.patch` with `git diff --output=...` and validate it using `git apply --check --reverse` from `src/TRELLIS.2`.

- [ ] **Step 7: Run focused regressions and native 512 E2E**

```bash
node --test tests/pixal3d-native.test.mjs tests/pixal3d-backends.test.mjs tests/pixal3d-native-installer.test.mjs
python -I scripts/native/test_native_tools.py
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_extension_probe.py
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_trellis2_parity.py
node scripts/native/run_native_single512.mjs
```

Expected: 24 unit tests pass; native 512 GLB is accepted with `backend=windows-rocm` and `fallbackEvents=[]`.

---

### Task 2: Add the native UV-1024 workflow contract with failing tests first

**Files:**
- Modify: `server/schema.mjs`
- Modify: `server/pixal3d.mjs`
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs`
- Modify: `server/mesh-validation.mjs` only if dimension validation belongs in the shared validator; otherwise use the hardware runner
- Test: `tests/pixal3d.test.mjs`
- Test: `tests/pixal3d-native.test.mjs`
- Test: `tests/amd-pixal3d-e2e.test.mjs`

**Interfaces:**
- `pixal3dWorkflow(..., { runtimeBackend:'windows-rocm', quality:'draft', uvTexture:true })` emits `texture_size:1024` and `bake_on_vertices:false`.
- `validatePixal3dRequest()` checks native `uvTexture1024` instead of rejecting every native texture request.
- WSL UV remains draft-only with `texture_size:512`; native standard geometry with UV remains rejected.

- [ ] **Step 1: Add failing workflow and request-validation tests**

Add assertions that native draft UV emits 1024, native UV requires the `uvTexture1024` capability, native standard UV still fails, WSL UV remains 512, and shape-only defaults are unchanged.

- [ ] **Step 2: Run the tests and confirm the intended failures**

```bash
node --test tests/pixal3d.test.mjs tests/pixal3d-native.test.mjs tests/amd-pixal3d-e2e.test.mjs
```

Expected: failures on the current WSL-only UV guard, fixed 512 atlas and blanket native texturing rejection.

- [ ] **Step 3: Implement the narrow workflow and capability changes**

In `schema.mjs`, permit UV for `wsl` and `windows-rocm`, then select the atlas explicitly:

```js
const uvTextureSize = runtimeBackend === 'windows-rocm' ? 1024 : 512;
```

Use `texture_size:uvTextureSize` only in the existing texturing node. In `pixal3d.mjs`, route native draft UV to `uvTexture1024`; keep the standard-texturing and multi-view-texturing guards. In the hardware option parser, allow native UV only with `quality='draft'` and `views='single'`.

- [ ] **Step 4: Add exact embedded-PNG dimension validation**

Decode the PNG IHDR width and height from the embedded image bytes and require `1024 × 1024` for this hardware report. Keep the existing generic GLB validator compatible with other texture sizes by passing an explicit expected size from the runner rather than hard-coding 1024 globally.

- [ ] **Step 5: Run focused tests and formatting checks**

```bash
node --test tests/pixal3d.test.mjs tests/pixal3d-native.test.mjs tests/amd-pixal3d-e2e.test.mjs tests/pixal3d-backends.test.mjs tests/pixal3d-native-installer.test.mjs
python -I scripts/native/test_native_tools.py
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_trellis2_parity.py
git diff --check
```

Expected: all selected tests pass and no whitespace errors are reported.

---

### Task 3: Run native single-view shape 1024 on isolated ports

**Files:**
- Create: `scripts/native/run_native_single1024.mjs`
- Create: `data/runtime/diagnostics/native-amd/1024-single.json` (generated, ignored)
- Modify after acceptance only: `runtime/ComfyUI-Pixal3D-AMD-Native/native-ready.json`
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `scripts/native/README.md`

**Interfaces:**
- Consumes: existing studio runner and `AMD_PIXAL3D_QUALITY=standard`, `AMD_PIXAL3D_VIEWS=single`, `AMD_PIXAL3D_BACKEND=windows-rocm`.
- Produces: accepted standard-1024 GLB and diagnostic JSON; no UV output.

- [ ] **Step 1: Add an isolated runner with temporary capability restoration**

Use the process ownership and shutdown pattern from `run_native_single512.mjs`. Set report `1024-single.json`, server log `1024-single-server.log`, quality `standard`, UV false and output suffix `-standard`. Before launch, back up `native-ready.json` in memory and enable only `single1024` for this isolated process; restore the original JSON in `finally` if the run fails. Never enable multi-view or UV for this run.

- [ ] **Step 2: Run the real hardware job**

```bash
node scripts/native/run_native_single1024.mjs
```

Expected: standard single-view generation on `windows-rocm`, no WSL process, no fallback events, accepted GLB and clean owned-process shutdown. On OOM or ROCm failure, stop and preserve the server log without retrying blindly.

- [ ] **Step 3: Inspect geometry and four views**

Render `amd-pixal3d-native-single-standard.glb` with the existing local preview renderer. Check finite geometry, expected scale, complete surface and absence of obvious tiled seams or duplicate artifacts. Record counts and phase timings from the JSON.

- [ ] **Step 4: Persist only the verified capability and documentation**

After successful technical and visual checks, set `single1024:true` while leaving every other unverified capability false. Document job id, time, geometry, memory evidence and artifact paths. Do not claim comfortable repeated 1024 operation from one run.

---

### Task 4: Run native draft-512 geometry with UV atlas 1024

**Files:**
- Create: `scripts/native/run_native_uv1024.mjs`
- Create: `data/runtime/diagnostics/native-amd/512-single-uv1024.json` (generated, ignored)
- Modify after acceptance only: `runtime/ComfyUI-Pixal3D-AMD-Native/native-ready.json`
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `scripts/native/README.md`

**Interfaces:**
- Consumes: Task 2 native draft UV workflow and `AMD_PIXAL3D_UV_TEXTURE=1`.
- Produces: GLB with `TEXCOORD_0` and embedded base-color PNG exactly 1024 × 1024.

- [ ] **Step 1: Add the isolated UV runner**

Use isolated ports and owned-process cleanup. Set quality `draft`, views `single`, UV true, report `512-single-uv1024.json`, log `512-single-uv1024-server.log`, and a distinct `amd-pixal3d-native-single-uv1024.glb`. Temporarily enable only `uvTexture1024`; restore the original readiness file on failure.

- [ ] **Step 2: Run the real native UV job**

```bash
node scripts/native/run_native_uv1024.mjs
```

Expected: no WSL or shape-only fallback, valid GLB, explicit 1024 atlas validation and clean process exit. If it fails, retain `uvTexture1024:false` and record the exact stage and error.

- [ ] **Step 3: Inspect atlas and textured model**

Extract the embedded PNG, verify IHDR dimensions 1024 × 1024 and non-uniform RGB content, compute non-empty UV coverage, and render four textured views. Inspect black regions, seams, mirrored UVs and missing material references.

- [ ] **Step 4: Persist only the verified UV capability and documentation**

After acceptance set `uvTexture1024:true`; do not infer `uvTexture512`, standard-1024 UV or multi-view support. Document measurements and visual results.

---

### Task 5: Final responsiveness and regression gate

**Files:**
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `scripts/native/README.md`

**Interfaces:**
- Consumes: all preceding reports and visual artifacts.
- Produces: a factual capability matrix and remaining performance/responsiveness limits.

- [ ] **Step 1: Check for avoidable long synchronous operations in changed paths**

Review profiling and phase reports for long device-wide synchronization and large readbacks. Make another code change only if evidence identifies a specific AstraForge-owned bottleneck; repeat the associated correctness test afterward.

- [ ] **Step 2: Run final focused verification**

```bash
node --test tests/pixal3d-native.test.mjs tests/pixal3d-backends.test.mjs tests/pixal3d-native-installer.test.mjs tests/pixal3d.test.mjs tests/amd-pixal3d-e2e.test.mjs
python -I scripts/native/test_native_tools.py
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_extension_probe.py
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_trellis2_parity.py
git diff --check
```

Expected: zero test failures. Broad server and browser suites remain explicitly out of this hardware pass.

- [ ] **Step 3: Report actual results and remaining locks**

Provide links to shape-1024 and UV-1024 GLBs, reports and textured preview. State measured generation times, any observed pauses or disconnects, enabled capabilities, and the fact that multi-view 1024 and standard-1024+UV remain locked. Do not commit or push.
