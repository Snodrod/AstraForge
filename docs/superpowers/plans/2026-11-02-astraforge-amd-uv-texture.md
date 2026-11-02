# AMD UV Base Color Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export a real AMD-generated single-view Pixal3D GLB with `TEXCOORD_0` and an embedded 512 × 512 `baseColorTexture`, without CUDA or nvdiffrast.

**Architecture:** Reuse ComfyUI's existing UV unwrap, tiled UV rasterizer and barycentric interpolation inside the narrow AMD branch of `postprocess_mesh()`. Keep Pixal3D latent generation, voxel sampling, inpainting and trimesh export unchanged. Add an explicit `uvTexture` opt-in and strict GLB material validation.

**Tech Stack:** Python 3.12, PyTorch ROCm, existing ComfyUI UV helpers, trimesh, Node.js test runner, WSL2, RX 9070 XT 16 GB.

## Global Constraints

- Do not update the AMD driver, ROCm or system components; no reboot and no global `wsl --shutdown`.
- No CUDA, NATTEN, nvdiffrast, CuMesh UV unwrap or new dependencies in the AMD UV path.
- First hardware run: single-view, draft geometry, texture latent 1024, atlas 512 × 512.
- Shape-only remains default; existing `COLOR_0` behavior remains unchanged.
- Stop only `AstraForge-AMD` when a code reload is required.
- Runtime files, weights, logs, GLB and PNG are never committed.
- Commit/push source changes to `amd-rocm`; do not merge PR #1.

## File Structure

- `scripts/wsl/patch_trellis2_amd.py`: adds the AMD-only UV unwrap/rasterizer substitutions.
- `tests/amd-wsl-scripts.test.mjs`: executes the patch against focused upstream fixtures.
- `server/schema.mjs`: owns the `uvTexture` API option and workflow graph.
- `server/jobs.mjs`, `server/pixal3d-run.mjs`: propagate the option and validation requirement.
- `server/mesh-validation.mjs`: validates UV, material, image reference and embedded PNG.
- `tests/pixal3d.test.mjs`: workflow, fake engine, UV GLB and normalization tests.
- `scripts/verify_amd_pixal3d_e2e.mjs`, `tests/amd-pixal3d-e2e.test.mjs`: hardware runner option and result naming.
- `docs/forge/pixal3d.md`, `scripts/wsl/README.md`: verified behavior and measurements.
- `../render_glb_preview.py`, `../pixal3d-amd-preview`: uncommitted local visual verification surfaces.

---

### Task 1: AMD UV unwrap and rasterization patch

**Files:**
- Modify: `scripts/wsl/patch_trellis2_amd.py:152-178`
- Test: `tests/amd-wsl-scripts.test.mjs`

**Interfaces:**
- Consumes: existing `patch_vertex_colors(node)` and upstream `postprocess_mesh()` anchors.
- Produces: AMD calls to `_uv_unwrap()`, `_rasterize_uv_barycentric()` and `_interp_vertex_attr()`; NVIDIA and vertex-color branches remain present.

- [ ] **Step 1: Write the failing patch test**

Add a fixture containing the current UV and rasterizer anchors, run `patch_vertex_colors()`, then assert:

```js
assert.match(patched, /from comfy_extras\.nodes_mesh_postprocess import _uv_unwrap, _rasterize_uv_barycentric, _interp_vertex_attr/);
assert.match(patched, /_uv_unwrap\(\s*vertices_torch\.cpu\(\), faces_torch\.cpu\(\)\.long\(\),\s*'adaptive', 1024, 2, 0\.0\)/);
assert.match(patched, /_rasterize_uv_barycentric\(faces, uvs, texture_size\)/);
assert.match(patched, /_interp_vertex_attr\(vertices_torch, faces_torch\.long\(\), face_idx, bary, mask\)/);
assert.match(patched, /AMD UV rasterization produced an empty texture/);
assert.match(patched, /ctx = dr\.RasterizeCudaContext\(\)/);
assert.match(patched, /if bake_on_vertices:/);
```

The fixture must include these exact current anchors so the test exercises migration rather than only searching the patch source:

```python
from comfy.utils import ProgressBar
        elif not bake_on_vertices:
            _cumesh = cumesh.CuMesh()
        print('Finalizing mesh ...')
        ctx = dr.RasterizeCudaContext()
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `node --test tests/amd-wsl-scripts.test.mjs`

Expected: the new test fails because the ComfyUI UV imports and calls are absent.

- [ ] **Step 3: Implement the minimal patch additions**

Add the import through `replace_once()`:

```python
text = replace_once(
    text,
    'from comfy.utils import ProgressBar\n',
    'from comfy.utils import ProgressBar\nfrom comfy_extras.nodes_mesh_postprocess import _uv_unwrap, _rasterize_uv_barycentric, _interp_vertex_attr\n',
    'AMD UV helpers',
)
```

In the AMD `elif not bake_on_vertices` branch, use this exact data flow and keep the complete existing CuMesh code under `else`:

```python
            if os.environ.get('ASTRA_AMD_ROCM') == '1':
                vmapping, faces, uvs = _uv_unwrap(
                    vertices_torch.cpu(), faces_torch.cpu().long(),
                    'adaptive', 1024, 2, 0.0)
                vertices = vertices[vmapping]
                normals = normals[vmapping]
                vertices_torch = torch.from_numpy(vertices).float().cuda()
                faces_torch = torch.from_numpy(faces).int().cuda()
                uvs_torch = torch.from_numpy(uvs).float().cuda()
            else:
                _cumesh = cumesh.CuMesh()
                _cumesh.init(vertices_torch, faces_torch)
                print('Unwrapping mesh ...')
                vertices_torch, faces_torch, uvs_torch, vmap = _cumesh.uv_unwrap(
                    compute_charts_kwargs={
                        'threshold_cone_half_angle_rad': np.radians(mesh_cluster_threshold_cone_half_angle_rad),
                        'refine_iterations': 0,
                        'global_iterations': 1,
                        'smooth_strength': 1,
                    },
                    return_vmaps=True,
                    verbose=True,
                )
                del _cumesh
                gc.collect()
                vertices_torch = vertices_torch.cuda()
                faces_torch = faces_torch.cuda()
                uvs_torch = uvs_torch.cuda()
                vertices = vertices_torch.cpu().numpy()
                faces = faces_torch.cpu().numpy()
                uvs = uvs_torch.cpu().numpy()
                normals = normals[vmap.cpu().numpy()]
```

Replace only the AMD rasterizer path and retain the complete NVIDIA `else`:

```python
        if os.environ.get('ASTRA_AMD_ROCM') == '1':
            face_idx, bary, mask = _rasterize_uv_barycentric(faces, uvs, texture_size)
            if not mask.any():
                raise RuntimeError('AMD UV rasterization produced an empty texture')
            pos = _interp_vertex_attr(vertices_torch, faces_torch.long(), face_idx, bary, mask)
        else:
            ctx = dr.RasterizeCudaContext()
            uvs_torch = torch.cat([uvs_torch * 2 - 1, torch.zeros_like(uvs_torch[:, :1]), torch.ones_like(uvs_torch[:, :1])], dim=-1).unsqueeze(0)
            rast, _ = dr.rasterize(
                ctx, uvs_torch, faces_torch,
                resolution=[texture_size, texture_size],
            )
            torch.cuda.synchronize()
            mask = rast[0, ..., 3] > 0
            pos = dr.interpolate(vertices_torch.unsqueeze(0), rast, faces_torch)[0][0]
```

`faces` and `uvs` are NumPy arrays in the AMD branch. The existing `pos[mask]`, voxel sampling, texture construction and final V flip remain unchanged.

- [ ] **Step 4: Run focused tests and syntax checks**

Run: `node --test tests/amd-wsl-scripts.test.mjs && python -m py_compile scripts/wsl/patch_trellis2_amd.py`

Expected: all tests PASS and Python compilation exits 0.

- [ ] **Step 5: Commit the patch and tests**

```bash
git add scripts/wsl/patch_trellis2_amd.py tests/amd-wsl-scripts.test.mjs
git commit -m "Add AMD UV unwrap and rasterization"
```

---

### Task 2: `uvTexture` workflow and strict GLB validation

**Files:**
- Modify: `server/schema.mjs`, `server/jobs.mjs`, `server/pixal3d-run.mjs`, `server/mesh-validation.mjs`
- Test: `tests/pixal3d.test.mjs`

**Interfaces:**
- Consumes: Task 1 AMD behavior when node 40 runs with `bake_on_vertices=false`.
- Produces: `uvTexture: boolean` end to end; `validateGeneratedMesh(buffer, { requireUvTexture: boolean })`; result metadata `uvTexture` and validation flag `uvTexture`.

- [ ] **Step 1: Write failing workflow and GLB tests**

Add workflow assertions:

```js
const w = pixal3dWorkflow({ front: 'f.png' }, { uvTexture: true, runtimeBackend: 'wsl' });
assert.equal(w['40'].inputs.bake_on_vertices, false);
assert.equal(w['40'].inputs.texture_size, 512);
assert.deepEqual(w['27'].inputs.trimesh, ['40', 0]);
assert.throws(() => pixal3dWorkflow({ front: 'f', back: 'b' }, { uvTexture: true, runtimeBackend: 'wsl' }), /single-view/);
assert.throws(() => pixal3dWorkflow({ front: 'f' }, { uvTexture: true }), /AMD|WSL/);
assert.throws(() => pixal3dWorkflow({ front: 'f' }, { uvTexture: true, vertexColors: true, runtimeBackend: 'wsl' }), /mutually exclusive|cannot be combined/);
assert.equal(pixal3dWorkflow({ front: 'f' })['40'], undefined);
assert.equal(generationSchema.parse({ imageId: ID }).uvTexture, false);
```

Create `texturedBoxGlb()` by extending the existing `boxGlb()` fixture with:

- eight float32 `VEC2` UV values and `TEXCOORD_0`;
- a valid PNG buffer (the tests may use the existing zlib PNG helper or a fixed 1 × 1 PNG byte array);
- `bufferViews` for UV and PNG;
- `images: [{ bufferView, mimeType: 'image/png' }]`;
- `textures: [{ source: 0 }]`;
- one material whose `pbrMetallicRoughness.baseColorTexture.index` is `0`;
- primitive `material: 0`.

Add assertions:

```js
const textured = texturedBoxGlb();
assert.equal(validateGeneratedMesh(textured, { requireUvTexture: true }).uvTexture, true);
assert.equal(validateGeneratedMesh(normalizeGlb(textured).buffer, { requireUvTexture: true }).uvTexture, true);
assert.throws(() => validateGeneratedMesh(boxGlb(), { requireUvTexture: true }), /TEXCOORD_0|baseColorTexture/);
assert.throws(() => validateGeneratedMesh(texturedBoxGlb({ png: Buffer.from('broken') }), { requireUvTexture: true }), /PNG/);
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `node --test tests/pixal3d.test.mjs`

Expected: FAIL because `uvTexture` and UV/material validation are not implemented.

- [ ] **Step 3: Implement workflow propagation**

Add `uvTexture: z.boolean().default(false)` beside `vertexColors`. Add the workflow option and guards:

```js
if (uvTexture && vertexColors) throw new Error('Pixal3D UV texture and vertex colors cannot be combined.');
if (uvTexture && multi) throw new Error('Pixal3D UV texture currently requires the single-view workflow.');
if (uvTexture && runtimeBackend !== 'wsl') throw new Error('Pixal3D UV texture currently requires the AMD WSL runtime.');
```

Build node 40 when `vertexColors || uvTexture`; use `bake_on_vertices: !uvTexture` and route node 27 to it. Thread `!!j.uvTexture` through `jobs.mjs` and `!!o.uvTexture` through `generatePixal3d()`, workflow construction, result metadata and both validation calls.

- [ ] **Step 4: Implement UV/material/PNG validation**

Locate the BIN payload from the GLB chunk immediately after JSON:

```js
const binHeader = 20 + size;
const binStart = binHeader + 8;
const hasBin = binHeader + 8 <= buffer.length && buffer.toString('ascii', binHeader + 4, binHeader + 8) === 'BIN\0';
```

For every primitive, require `TEXCOORD_0` with `VEC2`, count equal to `POSITION`, and component type 5121, 5123 or 5126. Resolve `primitive.material → pbrMetallicRoughness.baseColorTexture.index → textures[index].source → images[source].bufferView`. Check view bounds against the BIN chunk and the eight-byte PNG signature `89 50 4e 47 0d 0a 1a 0a`.

Aggregate `uvTexture` exactly like `vertexColors`; only throw when `requireUvTexture` is true:

```js
if (requireUvTexture && !uvTexture) throw new Error('Invalid or missing TEXCOORD_0/baseColorTexture PNG.');
```

Return `uvTexture` in the validation result without changing existing shape-only or `COLOR_0` behavior.

- [ ] **Step 5: Extend fake generation and run tests**

Add `uvTexture=false` to `fakeEngine()`; return `texturedBoxGlb()` when true. Assert node 40 has `bake_on_vertices=false`, result `uvTexture===true`, and strict validation passes after normalization.

Run: `node --test tests/pixal3d.test.mjs`

Expected: all tests PASS.

- [ ] **Step 6: Commit API and validation**

```bash
git add server/schema.mjs server/jobs.mjs server/pixal3d-run.mjs server/mesh-validation.mjs tests/pixal3d.test.mjs
git commit -m "Add AMD UV texture workflow and validation"
```

---

### Task 3: Hardware runner opt-in

**Files:**
- Modify: `scripts/verify_amd_pixal3d_e2e.mjs`, `tests/amd-pixal3d-e2e.test.mjs`

**Interfaces:**
- Consumes: Task 2 `uvTexture` API and validator.
- Produces: `AMD_PIXAL3D_UV_TEXTURE=1`; output `data/runtime/amd-pixal3d-single-uv.glb`.

- [ ] **Step 1: Write failing option tests**

```js
assert.deepEqual(parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1' }), {
  views: 'single', jobId: null, quality: 'draft', vertexColors: false, uvTexture: true,
});
assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_VERTEX_COLORS: '1' }), /cannot be combined/);
assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_VIEWS: 'multi' }), /single-view/);
```

Update previous expected option objects with `uvTexture: false`.

- [ ] **Step 2: Run and confirm failure**

Run: `node --test tests/amd-pixal3d-e2e.test.mjs`

Expected: FAIL because `uvTexture` is absent.

- [ ] **Step 3: Implement the runner option**

Parse `const uvTexture = env.AMD_PIXAL3D_UV_TEXTURE === '1'`, reject multi-view and conflict with `vertexColors`, send `uvTexture` in the job body, use suffix `-uv`, and call:

```js
validateGeneratedMesh(glb, {
  requireVertexColors: options.vertexColors,
  requireUvTexture: options.uvTexture,
});
```

Include `uvTexture` in the JSON result. Do not overwrite `-colors`, `-standard`, draft or multi-view files.

- [ ] **Step 4: Test and commit**

Run: `node --test tests/amd-pixal3d-e2e.test.mjs tests/pixal3d.test.mjs`

Expected: all tests PASS.

```bash
git add scripts/verify_amd_pixal3d_e2e.mjs tests/amd-pixal3d-e2e.test.mjs
git commit -m "Add AMD UV texture hardware runner"
```

---

### Task 4: Real RX 9070 XT UV generation and visual review

**Files:**
- Runtime only: patched `runtime/ComfyUI-Pixal3D-AMD/ComfyUI/custom_nodes/ComfyUI-Trellis2/trellis2/pipelines/trellis2_image_to_3d.py`
- Local only: `data/runtime/amd-pixal3d-single-uv.glb`, `../preview-renders/*`, `../pixal3d-amd-preview/src/project.js`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: measured job id, seconds, VRAM, geometry, GLB size, nonuniform base color atlas and four-view render.

- [ ] **Step 1: Reapply only the project patch and reload its distro**

Do not run the installer. Apply the existing patch to the node root, then terminate only `AstraForge-AMD` so the next job loads the new Python code:

```bash
MSYS2_ARG_CONV_EXCL='*' wsl.exe -d AstraForge-AMD -- env HSA_ENABLE_DXG_DETECTION=1 LD_LIBRARY_PATH=/opt/rocm/lib /opt/astraforge/pixal3d-venv/bin/python /mnt/c/Users/User/Documents/kimi/tasks/2026-11-01/18-59-30-895229e4/AstraForge-amd/scripts/wsl/patch_trellis2_amd.py /mnt/c/Users/User/Documents/kimi/tasks/2026-11-01/18-59-30-895229e4/AstraForge-amd/runtime/ComfyUI-Pixal3D-AMD/ComfyUI/custom_nodes/ComfyUI-Trellis2
wsl.exe -t AstraForge-AMD
```

Restart the project-owned AstraForge server as well, so the Windows Node process loads the new `uvTexture` schema and workflow code:

```bash
cmd.exe /c Stop.cmd
cmd.exe /c Start.cmd
```

Wait until `http://127.0.0.1:3210/api/status` responds before submitting the job. Expected: patch marker printed, project server restarted, no Windows reboot and no other distro stopped.

- [ ] **Step 2: Run the bounded single-view UV job**

From the repository root:

```powershell
$env:AMD_PIXAL3D_UV_TEXTURE='1'
$env:AMD_PIXAL3D_QUALITY='draft'
node scripts/verify_amd_pixal3d_e2e.mjs
```

Run through the existing logged background-runner pattern if the wait exceeds the foreground limit. Read the job id from the first log line; do not assume history ordering.

Expected: completed JSON with `uvTexture:true`, `validation.uvTexture:true`, valid geometry and `data/runtime/amd-pixal3d-single-uv.glb`. On failure, use the exact job's `promptId` and `/history/<promptId>` on port 8191; do not claim success without the GLB.

- [ ] **Step 3: Verify texture content and render four views**

Extend the local renderer to report base color texture dimensions, finite RGB range and distinct rounded RGB count, save the atlas as `preview-renders/amd-pixal3d-single-uv-atlas.png`, and render the GLB with its actual material.

Run from the workspace:

```bash
PYTHONPATH=.render-deps python render_glb_preview.py
```

Expected: nonempty 512 × 512 atlas, nonuniform RGB values, valid GLB render and `preview-renders/amd-pixal3d-single-uv.png`. Check image sizes before opening and visually inspect both atlas and four-view image.

- [ ] **Step 4: Add the verified model to the local preview**

Copy the GLB to `pixal3d-amd-preview/public/models/amd-pixal3d-single-uv.glb`; add a fourth scene with id `uv` and model URL `/models/amd-pixal3d-single-uv.glb`. Keep review status `pending` while Edit3D handshake remains unconfirmed.

Run: `npm run check && npm run build`

Expected: both exit 0 and `dist/public/models/amd-pixal3d-single-uv.glb` exists. Stop any temporary development server before delivery.

---

### Task 5: Documentation, full verification and delivery

**Files:**
- Modify: `docs/forge/pixal3d.md`, `scripts/wsl/README.md`

**Interfaces:**
- Consumes: actual Task 4 measurements and observed limitations.
- Produces: reproducible documentation and a source-only update to PR #1.

- [ ] **Step 1: Update documentation with observed facts**

Document `uvTexture`, `AMD_PIXAL3D_UV_TEXTURE=1`, reuse of ComfyUI UV helpers, 512 atlas, actual job id, seconds, VRAM peak/baseline, geometry and GLB bytes. State precisely which combinations remain unverified: multi-view UV, 1024 atlas and standard 1024 geometry with UV. Do not claim normal/AO support or successful Edit3D handshake.

- [ ] **Step 2: Run full verification**

```bash
npm test
python -m py_compile scripts/wsl/patch_trellis2_amd.py
git diff --check
```

Expected: all tests pass, zero failures, no whitespace errors. Existing skips must remain explicitly reported.

- [ ] **Step 3: Commit and push source-only changes**

```bash
git add docs/forge/pixal3d.md scripts/wsl/README.md
git commit -m "Document verified AMD UV texturing"
git push fork amd-rocm
git status --short --branch
git ls-remote fork refs/heads/amd-rocm
```

Expected: clean branch, remote hash equals local HEAD, PR #1 updated. Confirm that no runtime model, atlas, log or weight file is committed.

- [ ] **Step 4: Deliver the verified artifacts**

Report the commit and PR, actual hardware metrics, GLB/atlas/four-view links and remaining limitations. Include the existing preview project's absolute root and its single localhost preview URL. Do not leave a development server running.
