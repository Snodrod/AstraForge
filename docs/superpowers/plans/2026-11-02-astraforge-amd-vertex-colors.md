# AMD Pixal3D Vertex Colors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in single-view Pixal3D mode that exports vertex-colored GLB on ROCm without CUDA, UV unwrap or nvdiffrast.

**Architecture:** Reuse the upstream texture latent and `bake_on_vertices`, but patch the AMD pipeline so vertex mode skips the UV branch entirely. Add a `vertexColors` workflow/API option, preserve shape-only defaults, and validate `COLOR_0` before and after normalization.

**Tech Stack:** Python patch script, Pixal3D/TRELLIS.2, Node.js, Zod, GLB 2.0, ROCm.

## Global Constraints

- Vertex colors are not UV/PBR texture maps; no 4K or 1536 claim.
- No `nvdiffrast`, CUDA wheels, CuMesh UV unwrap or rasterizer in this path.
- Shape-only remains the default; multi-view coloring is not enabled before single-view hardware verification.
- No driver/ROCm changes, reboot or global WSL shutdown.

---

### Task 1: Skip UV unwrap for vertex baking

**Files:**
- Modify: `scripts/wsl/patch_trellis2_amd.py`
- Test: `tests/amd-wsl-scripts.test.mjs`

**Interfaces:**
- Produces: `patch_vertex_colors(node)` applied by `main()` after precision patches.

- [ ] **Step 1: Write failing patch test**

Create a temporary `trellis2/pipelines/trellis2_image_to_3d.py` containing the two upstream anchors, call `patch_vertex_colors`, then assert:

```js
assert.match(patched, /if not bake_on_vertices and hasattr\(mesh, 'visual'\)/);
assert.match(patched, /elif not bake_on_vertices:/);
```

- [ ] **Step 2: Verify failure**

Run: `node --test --test-name-pattern="vertex" tests/amd-wsl-scripts.test.mjs`
Expected: FAIL because `patch_vertex_colors` does not exist.

- [ ] **Step 3: Implement patch**

Use `replace_once` for the exact upstream anchors:

```python
def patch_vertex_colors(node):
    path = node / 'trellis2/pipelines/trellis2_image_to_3d.py'
    text = path.read_text(encoding='utf-8')
    text = replace_once(text, "        if hasattr(mesh, 'visual') and hasattr(mesh.visual, 'uv') and mesh.visual.uv is not None:\n", "        if not bake_on_vertices and hasattr(mesh, 'visual') and hasattr(mesh.visual, 'uv') and mesh.visual.uv is not None:\n", 'vertex color UV guard')
    text = replace_once(text, "        else:\n            _cumesh = cumesh.CuMesh()\n", "        elif not bake_on_vertices:\n            _cumesh = cumesh.CuMesh()\n", 'vertex color unwrap skip')
    path.write_text(text, encoding='utf-8')
```

Call it from `main()` only for the AMD node patch.

- [ ] **Step 4: Verify patch tests**

Run: `node --test tests/amd-wsl-scripts.test.mjs`
Expected: PASS; existing NAF/precision tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add scripts/wsl/patch_trellis2_amd.py tests/amd-wsl-scripts.test.mjs
git commit -m "Skip CUDA UV unwrap for AMD vertex colors"
```

### Task 2: Opt-in workflow and COLOR_0 validation

**Files:**
- Modify: `server/schema.mjs:47-90`
- Modify: `server/mesh-validation.mjs`
- Test: `tests/pixal3d.test.mjs`

**Interfaces:**
- Produces: `pixal3dWorkflow(images, { vertexColors: boolean })`; `validateGeneratedMesh(buffer, { requireVertexColors: boolean })` returns `vertexColors`.

- [ ] **Step 1: Write failing workflow tests**

```js
const w = pixal3dWorkflow({ front: 'f.png' }, { vertexColors: true, runtimeBackend: 'wsl' });
assert.equal(w['40'].class_type, 'Trellis2MeshTexturingPixal3DMultiView');
assert.equal(w['40'].inputs.bake_on_vertices, true);
assert.equal(w['40'].inputs.trimesh[0], '26');
assert.deepEqual(w['27'].inputs.trimesh, ['40', 0]);
assert.equal(pixal3dWorkflow({ front: 'f' })['40'], undefined);
assert.throws(() => pixal3dWorkflow({ front: 'f', back: 'b' }, { vertexColors: true }), /single-view/);
```

Replace the blanket texturing prohibition with a forbidden-class test that still rejects `/nvdiff|UnWrap|Rasteriz|Render|Bake|TexSlat/i` and permits node 40 only when `vertexColors` is true.

- [ ] **Step 2: Write failing GLB tests**

Extend the box fixture with `COLOR_0`, then assert missing/mismatched colors fail only when required:

```js
assert.throws(() => validateGeneratedMesh(boxGlb(), { requireVertexColors: true }), /COLOR_0/);
assert.equal(validateGeneratedMesh(coloredBoxGlb(), { requireVertexColors: true }).vertexColors, true);
assert.equal(validateGeneratedMesh(boxGlb()).accepted, true);
```

- [ ] **Step 3: Run tests to verify failure**

Run: `node --test --test-name-pattern="vertex|shape only" tests/pixal3d.test.mjs`
Expected: FAIL on absent workflow option and validator parameter.

- [ ] **Step 4: Implement workflow and validation**

Add `vertexColors=false` to workflow options; reject multi-view when true. Node 40 uses pipeline `[decoded,2]`, `trimesh:['26',0]`, `image:['3',0]`, `moge_camera_config:['4',3]`, `resolution:1024`, `texture_size:512`, `bake_on_vertices:true`, `mesh_orientation:'none'`, `sampler:'euler'`, and upstream defaults for remaining texture parameters. Route node 27 to `['40',0]`.

Extend validation without changing defaults:

```js
export function validateGeneratedMesh(buffer, { requireVertexColors = false } = {}) {
  // Existing header/triangle checks remain. For every triangle primitive:
  const color = doc.accessors?.[primitive.attributes?.COLOR_0];
  if (requireVertexColors && (!color || color.count !== position.count || !['VEC3','VEC4'].includes(color.type) || ![5121,5123,5126].includes(color.componentType))) {
    throw new Error('Invalid or missing COLOR_0 vertex colors.');
  }
  return { bytes: buffer.length, triangles, vertices, accepted: true, vertexColors: requireVertexColors, scope: 'technical bounds; anatomy is not validated' };
}
```

- [ ] **Step 5: Verify and commit**

Run: `node --test tests/pixal3d.test.mjs`
Expected: PASS, including shape-only regression tests.

```bash
git add server/schema.mjs server/mesh-validation.mjs tests/pixal3d.test.mjs
git commit -m "Add AMD Pixal3D vertex color workflow"
```

### Task 3: Thread option through generation and E2E

**Files:**
- Modify: `server/schema.mjs:4-11`
- Modify: `server/jobs.mjs:89-92`
- Modify: `server/pixal3d-run.mjs:139-217`
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs`
- Test: `tests/pixal3d.test.mjs`, `tests/amd-pixal3d-e2e.test.mjs`

**Interfaces:**
- Consumes: `generationSchema.parse(...).vertexColors`.
- Produces: E2E env `AMD_PIXAL3D_VERTEX_COLORS=1`, `AMD_PIXAL3D_QUALITY=draft|standard`.

- [ ] **Step 1: Write failing option tests**

```js
assert.equal(generationSchema.parse({ imageId: ID }).vertexColors, false);
assert.equal(generationSchema.parse({ imageId: ID, vertexColors: true }).vertexColors, true);
assert.deepEqual(parseOptions({ AMD_PIXAL3D_VERTEX_COLORS: '1', AMD_PIXAL3D_QUALITY: 'standard' }), { views: 'single', jobId: null, vertexColors: true, quality: 'standard' });
```

Add a fake ComfyUI generation assertion that raw and normalized buffers require colors only for the opt-in run.

- [ ] **Step 2: Verify failure**

Run: `node --test tests/pixal3d.test.mjs tests/amd-pixal3d-e2e.test.mjs`
Expected: FAIL on missing options.

- [ ] **Step 3: Implement propagation**

Add `vertexColors:z.boolean().default(false)` to the generation schema. Pass `j.vertexColors` from jobs to `generatePixal3d`, then into `pixal3dWorkflow`. Validate with `{ requireVertexColors: !!o.vertexColors }` before and after normalization and return `vertexColors` in the result. E2E sends the selected quality/color values and validates the downloaded GLB with the same requirement.

- [ ] **Step 4: Run focused tests**

Run: `node --test tests/pixal3d.test.mjs tests/amd-pixal3d-e2e.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/schema.mjs server/jobs.mjs server/pixal3d-run.mjs scripts/verify_amd_pixal3d_e2e.mjs tests/pixal3d.test.mjs tests/amd-pixal3d-e2e.test.mjs
git commit -m "Expose opt-in Pixal3D vertex coloring"
```

### Task 4: Hardware verification and documentation

**Files:**
- Modify: `scripts/wsl/README.md`, `docs/forge/pixal3d.md`

- [ ] **Step 1: Reapply only the project AMD patch**

Do not run the installer or change dependencies. Restart only the project Pixal3D runtime if the patched module is already loaded.

- [ ] **Step 2: Run one single-view vertex-color job**

Use draft shape plus vertex colors first, with the existing VRAM probe. Do not combine this first test with the full 1024 shape profile.
Expected: completed GLB with `COLOR_0`, nonzero color variation, accepted technical validation and successful reload in Edit3D.

- [ ] **Step 3: Record honest outcome**

Document job ID, seconds, peak memory, geometry/color counts and output path. If the texture latent fails, record the exact stage and leave the mode unverified.

- [ ] **Step 4: Run full tests and commit**

Run: `npm test`
Expected: 0 failed. Then:

```bash
git add scripts/wsl/README.md docs/forge/pixal3d.md
git commit -m "Document AMD Pixal3D vertex color verification"
```
