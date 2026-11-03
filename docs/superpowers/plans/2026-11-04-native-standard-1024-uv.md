# Native AMD Standard 1024 + UV 1024 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This session uses inline execution because subagents are disabled.

**Goal:** Add and verify a fail-closed native AMD single-view Standard 1024 + UV 1024 workflow.

**Architecture:** Reuse the existing Standard shape and UV workflow graph. Gate the previously blocked combination with one explicit native capability and use a temporary readiness override only inside a dedicated hardware runner.

**Tech Stack:** Node.js ESM, `node:test`, existing AstraForge native ComfyUI/TRELLIS.2 ROCm runtime.

## Global Constraints

- RX 9070 XT native Windows ROCm only; no driver or network changes.
- Do not open multi-view, UV 512, Standard vertex colors, WSL, or NVIDIA paths.
- No silent CPU, WSL, or model fallback; no quality reduction after OOM.
- One GPU job at a time. No commit or push unless separately requested.

---

### Task 1: Request and E2E option gates

**Files:**
- Modify: `server/pixal3d-backends.mjs`
- Modify: `server/pixal3d.mjs:43-65`
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs:10-27,185-189`
- Test: `tests/pixal3d-native.test.mjs`
- Test: `tests/pixal3d-backends.test.mjs`
- Test: `tests/amd-pixal3d-e2e.test.mjs`

**Interfaces:**
- Consumes: `validatePixal3dRequest(input, cfg, { multi })`, `parseOptions(env)`.
- Produces: boolean native capability `uvTextureStandard1024`; Standard native UV requests accepted only when explicitly ready.

- [ ] **Step 1: Write failing tests**

Add assertions equivalent to:

```js
assert.throws(
  () => validatePixal3dRequest(standardUv, nativeCfgWithoutNewCapability, { multi: false }),
  (error) => error.code === 'pixal3d_native_not_ready' && error.capability === 'uvTextureStandard1024',
);
assert.doesNotThrow(
  () => validatePixal3dRequest(standardUv, nativeCfgWithSingle1024AndNewCapability, { multi: false }),
);
assert.deepEqual(parseOptions({
  AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_BACKEND: 'windows-rocm', AMD_PIXAL3D_QUALITY: 'standard',
}), { ...defaults, uvTexture: true, backend: 'windows-rocm', quality: 'standard' });
```

Also assert the default capability is false, Standard WSL UV and native vertex colors remain blocked, and the Standard native workflow still has `resolution: 1024`, `texture_size: 1024`, and `bake_on_vertices: false`.

- [ ] **Step 2: Verify focused failure**

Run: `node --test tests/pixal3d-native.test.mjs tests/pixal3d-backends.test.mjs tests/amd-pixal3d-e2e.test.mjs`
Expected: FAIL only on the new Standard native UV expectations.

- [ ] **Step 3: Implement the minimal gate**

Add `uvTextureStandard1024: false` to `freezeCapabilities`. Replace the global draft guard with an exception only for native single-view Standard UV, and select capabilities with:

```js
const nativeStandardUv = cfg.backend === 'windows-rocm' && !multi && input.quality === 'standard' && input.uvTexture && !input.vertexColors;
const capability = nativeStandardUv ? 'uvTextureStandard1024' : input.uvTexture ? 'uvTexture1024' : input.quality === 'draft' ? (multi ? 'multi512' : 'single512') : (multi ? 'multi1024' : 'single1024');
if (nativeStandardUv && !cfg.nativeCapabilities?.single1024) {
  throw pixal3dRequestError('pixal3d_native_not_ready', 'Native AMD Pixal3D is not verified for this workflow. Run the native probes or select the WSL backend.', { capability: 'single1024' });
}
```

Permit Standard UV in `parseOptions` only when backend is `windows-rocm` and views are `single`. Prefix the native Standard UV output suffix with `-standard` to preserve the draft GLB.

- [ ] **Step 4: Run focused tests**

Run: `node --test tests/pixal3d-native.test.mjs tests/pixal3d.test.mjs tests/pixal3d-backends.test.mjs tests/amd-pixal3d-e2e.test.mjs`
Expected: PASS, with only pre-existing conditional skips.

### Task 2: Isolated hardware runner

**Files:**
- Create: `scripts/native/run_native_standard_uv1024.mjs`

**Interfaces:**
- Consumes: existing `scripts/verify_amd_pixal3d_e2e.mjs` and `native-ready.json`.
- Produces: `data/runtime/diagnostics/native-amd/1024-single-uv1024.json`, server log, and `data/runtime/amd-pixal3d-native-single-standard-uv1024.glb`.

- [ ] **Step 1: Add the runner**

Copy the established UV runner's lifecycle and change only its identity and environment: log `1024-single-uv1024-server.log`; temporarily set `uvTextureStandard1024: true`; set `AMD_PIXAL3D_QUALITY: 'standard'`; set `AMD_PIXAL3D_JSON` to `1024-single-uv1024.json`. Keep single view, UV on, vertex colors off, and byte-for-byte readiness restoration in `finally`.

- [ ] **Step 2: Check runner syntax and process exclusivity**

Run: `node --check scripts/native/run_native_standard_uv1024.mjs`
Expected: exit 0. Confirm no owned native/studio GPU job is active before continuing.

- [ ] **Step 3: Run one hardware E2E**

Run: `node scripts/native/run_native_standard_uv1024.mjs`
Expected: one accepted Standard 1024 + UV 1024 job; no fallback. On OOM, preserve the report/log and stop without lowering quality.

### Task 3: Evidence and bounded readiness

**Files:**
- Modify after success only: `runtime/ComfyUI-Pixal3D-AMD-Native/native-ready.json`
- Modify after result: `docs/forge/pixal3d-native-amd.md`
- Modify after result: `scripts/native/README.md`

**Interfaces:**
- Consumes: E2E JSON, GLB, atlas metadata, rendered preview.
- Produces: evidence-linked readiness only if every acceptance check passes; otherwise documentation retains the lock and records the failure.

- [ ] **Step 1: Validate artifact and preview**

Confirm accepted GLB, finite in-range UVs, non-uniform 1024 × 1024 atlas, empty fallback events, and visually inspect the existing four-view preview renderer output. Record the job, duration, geometry, artifact sizes, and any observed defect.

- [ ] **Step 2: Update readiness and documentation**

After full success, add `uvTextureStandard1024: true` and its report path to `native-ready.json`; otherwise leave it false. Update the two existing documents with measured facts and remaining limits, without claiming freeze-free operation.

- [ ] **Step 3: Final focused verification and cleanup**

Run the Task 1 test command again, verify readiness restoration/readiness evidence as applicable, and stop only runner-owned processes. Do not commit or push.
