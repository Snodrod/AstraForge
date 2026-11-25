# Native AMD UV Atlas 2048–4096 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Re-texture the accepted baton GLB with a real 2048 × 2048 UV base-color atlas, then repeat at 4096 × 4096 without changing the 1024 denoiser or regenerating geometry.

**Architecture:** Add a standalone experimental Node runner outside the production AstraForge request path. A pure workflow builder creates a texture-only ComfyUI graph; the runner starts an isolated native AMD process, uploads the existing prepared image, reuses the accepted GLB, validates the exact embedded PNG size, and writes separate artifacts.

**Tech Stack:** Node.js 24, ComfyUI HTTP API, native Windows ROCm, existing `normalizeGlb` and `validateGeneratedMesh` utilities.

## Global Constraints

- Keep texture denoiser `resolution=1024`; only `texture_size` changes to 2048 or 4096.
- Reuse `data/runtime/baton/amd-pixal3d-native-single-standard-uv1024.glb` and prepared asset `data/assets/250bcec2-20af-4899-b954-0d706238aa6b.png`.
- Use `mesh_orientation='90 degrees'` for the Y-up GLB, as required by `Trellis2MeshTexturingPixal3DMultiView`.
- Preserve seed 42, 12 texture steps, Euler sampler, existing guidance, and native UV behavior.
- Use isolated port 8194 and separate files under `data/runtime/baton/` and `data/runtime/diagnostics/native-amd/`.
- Never modify `native-ready.json`, production workflow defaults, drivers, networking, WSL, or Windows priorities.
- Run one GPU job at a time. Stop on OOM, driver reset, invalid output, or GLB validation failure; no blind retry or fallback.
- Implementation changes and pushes are not committed without a separate user request.

---

### Task 1: Pure texture-only workflow builder

**Files:**
- Create: `scripts/native/uv_atlas_workflow.mjs`
- Test: `tests/native-uv-atlas.test.mjs`

**Interfaces:**
- Produces: `parseAtlasOptions(env)` returning `{ textureSize, inputImage, inputGlb, outputDir, jsonFile }`.
- Produces: `buildUvAtlasWorkflow({ imageName, glbPath, textureSize, seed = 42 })` returning a ComfyUI prompt object.

- [x] **Step 1: Write failing workflow tests**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildUvAtlasWorkflow, parseAtlasOptions } from '../scripts/native/uv_atlas_workflow.mjs';

test('native UV experiment is texture-only and keeps the 1024 denoiser', () => {
  const graph = buildUvAtlasWorkflow({ imageName: 'baton.png', glbPath: 'C:/baton.glb', textureSize: 2048 });
  assert.equal(graph['40'].inputs.resolution, 1024);
  assert.equal(graph['40'].inputs.texture_size, 2048);
  assert.equal(graph['40'].inputs.texture_steps, 12);
  assert.equal(graph['40'].inputs.mesh_orientation, '90 degrees');
  assert.equal(graph['39'].inputs.glb_path, 'C:/baton.glb');
  assert.equal(graph['27'].inputs.trimesh[0], '40');
  assert.ok(!Object.values(graph).some((node) => /Shape|Sparse|DecodeLatents/.test(node.class_type)));
});

test('native UV experiment accepts only 2048 or 4096 and explicit existing paths', () => {
  const base = { AMD_UV_SIZE: '2048', AMD_UV_INPUT: 'C:/image.png', AMD_UV_GLB: 'C:/mesh.glb' };
  assert.deepEqual(parseAtlasOptions(base), {
    textureSize: 2048, inputImage: 'C:/image.png', inputGlb: 'C:/mesh.glb', outputDir: null, jsonFile: null,
  });
  assert.equal(parseAtlasOptions({ ...base, AMD_UV_SIZE: '4096' }).textureSize, 4096);
  assert.throws(() => parseAtlasOptions({ ...base, AMD_UV_SIZE: '1024' }), /2048 or 4096/);
  assert.throws(() => parseAtlasOptions({ ...base, AMD_UV_INPUT: '' }), /AMD_UV_INPUT/);
});
```

- [x] **Step 2: Run the tests and verify RED**

Run: `node --test tests/native-uv-atlas.test.mjs`
Expected: FAIL because `../scripts/native/uv_atlas_workflow.mjs` does not exist.

- [x] **Step 3: Implement the workflow builder**

```js
export function parseAtlasOptions(env = process.env) {
  const textureSize = Number(env.AMD_UV_SIZE || 2048);
  if (![2048, 4096].includes(textureSize)) throw new Error('AMD_UV_SIZE must be 2048 or 4096.');
  if (!env.AMD_UV_INPUT) throw new Error('AMD_UV_INPUT must name the prepared input PNG.');
  if (!env.AMD_UV_GLB) throw new Error('AMD_UV_GLB must name the accepted input GLB.');
  return {
    textureSize, inputImage: env.AMD_UV_INPUT, inputGlb: env.AMD_UV_GLB,
    outputDir: env.AMD_UV_OUTPUT_DIR || null, jsonFile: env.AMD_UV_JSON || null,
  };
}

export function buildUvAtlasWorkflow({ imageName, glbPath, textureSize, seed = 42 }) {
  if (![2048, 4096].includes(textureSize)) throw new Error('Texture atlas must be 2048 or 4096.');
  return {
    '1': { class_type: 'Trellis2LoadModel', inputs: {
      modelname: 'TencentARC/Pixal3D', backend: 'sdpa', device: 'cuda', low_vram: true,
      keep_models_loaded: false, conv_backend: 'flex_gemm', sparse_backend: 'sdpa',
      use_reconviagen: false, pixal3d_multiview: false,
    } },
    '2': { class_type: 'Trellis2LoadImageWithTransparency', inputs: { image: imageName } },
    '3': { class_type: 'Trellis2PreProcessImage', inputs: {
      image: ['2', 2], padding: 5, remove_background: false, max_size: 1024,
    } },
    '4': { class_type: 'Trellis2ImageCondGenerator', inputs: { pipeline: ['1', 0], image: ['3', 0], max_views: 1 } },
    '39': { class_type: 'Trellis2LoadMesh', inputs: { glb_path: glbPath, only_vertices_and_faces: false } },
    '40': { class_type: 'Trellis2MeshTexturingPixal3DMultiView', inputs: {
      pipeline: ['4', 2], trimesh: ['39', 0], image: ['3', 0], moge_camera_config: ['4', 3], seed,
      texture_steps: 12, texture_guidance_strength: 1, texture_guidance_rescale: 0, texture_rescale_t: 3,
      resolution: 1024, texture_size: textureSize, texture_alpha_mode: 'OPAQUE', double_side_material: false,
      texture_guidance_interval_start: 0.6, texture_guidance_interval_end: 0.9, bake_on_vertices: false,
      use_custom_normals: false, mesh_cluster_threshold_cone_half_angle_rad: 60, mesh_orientation: '90 degrees',
      sampler: 'euler', inpainting: 'telea', verbose: false, dino_lock: 0, dino_substeps: 4, dino_foundation_cap: 1,
    } },
    '27': { class_type: 'Trellis2ExportMesh', inputs: {
      trimesh: ['40', 0], filename_prefix: `astra/amd-uv-${textureSize}`, file_format: 'glb',
    } },
  };
}
```

- [x] **Step 4: Run workflow and existing native tests**

Run: `node --test tests/native-uv-atlas.test.mjs tests/pixal3d-native.test.mjs tests/pixal3d.test.mjs`
Expected: all tests pass; existing conditional skips remain allowed.

### Task 2: Isolated native runner and exact atlas validation

**Files:**
- Create: `scripts/native/run_native_uv_atlas.mjs`
- Test: `tests/native-uv-atlas.test.mjs`

**Interfaces:**
- Consumes: `parseAtlasOptions()` and `buildUvAtlasWorkflow()` from Task 1.
- Uses: `buildNativePixal3DLaunch()`, `pixal3dConfig()`, `getPixal3DBackend()`, `normalizeGlb()`, and `validateGeneratedMesh()`.
- Produces: `data/runtime/baton/amd-pixal3d-native-single-uv<SIZE>.glb` and `data/runtime/diagnostics/native-amd/baton-uv<SIZE>.json`.

- [x] **Step 1: Add a failing report-contract test**

Export a pure `buildUvAtlasReport({ textureSize, started, glbPath, normalized, validation, vram, now })` helper from the runner. Guard `main()` so importing the runner in a test cannot start ComfyUI or submit GPU work. Test the actual helper, not a literal object:

```js
test('native UV report records the denoiser and exact atlas independently', async () => {
  const { buildUvAtlasReport } = await import('../scripts/native/run_native_uv_atlas.mjs');
  const report = buildUvAtlasReport({
    textureSize: 2048, started: 1000, now: 2500, glbPath: 'C:/baton.glb',
    normalized: { buffer: Buffer.alloc(128) }, validation: { accepted: true, uvTexture: true }, vram: null,
  });
  assert.equal(report.backend, 'windows-rocm');
  assert.equal(report.denoiserResolution, 1024);
  assert.equal(report.textureSize, 2048);
  assert.equal(report.seconds, 1.5);
  assert.equal(report.glbBytes, 128);
  assert.deepEqual(report.fallbackEvents, []);
});
```

Hardware acceptance is separately proven by the runner calling:

```js
validateGeneratedMesh(normalized.buffer, { requireUvTexture: true, expectedUvTextureSize: options.textureSize });
```

- [x] **Step 2: Run the test file and verify RED**

Run: `node --test tests/native-uv-atlas.test.mjs`
Expected: FAIL because the runner and its exported report helper do not exist yet. No ComfyUI process or GPU job may start during this import.

- [x] **Step 3: Implement the runner**

Create `scripts/native/run_native_uv_atlas.mjs` with these exact operations and no production-server startup:

```js
import { spawn, spawnSync } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildNativePixal3DLaunch } from '../../server/pixal3d-native.mjs';
import { pixal3dConfig } from '../../server/pixal3d.mjs';
import { getPixal3DBackend } from '../../server/pixal3d-backends.mjs';
import { normalizeGlb } from '../../server/glb-normalize.mjs';
import { validateGeneratedMesh } from '../../server/mesh-validation.mjs';
import { buildUvAtlasWorkflow, parseAtlasOptions } from './uv_atlas_workflow.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const options = parseAtlasOptions();
const url = 'http://127.0.0.1:8194';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const json = async (route, init) => {
  const response = await fetch(url + route, init);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || body.error || `${route}: ${response.status}`);
  return body;
};
```

Then implement `main()` as follows, using these literal API sequences:

```js
async function main() {
  if (!existsSync(options.inputImage)) throw new Error(`Input PNG not found: ${options.inputImage}`);
  if (!existsSync(options.inputGlb)) throw new Error(`Input GLB not found: ${options.inputGlb}`);
  const env = { ...process.env, ASTRA_PIXAL3D_BACKEND: 'windows-rocm', ASTRA_PIXAL3D_URL: url, ASTRA_AMD_NAF_TILE: '2' };
  const config = pixal3dConfig({ env, appRoot: root });
  const descriptor = getPixal3DBackend('windows-rocm', env, root);
  const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
  mkdirSync(diagnostics, { recursive: true });
  const launch = buildNativePixal3DLaunch({
    config, descriptor,
    tempDir: path.join(diagnostics, `uv-${options.textureSize}-temp`),
    dbFile: path.join(diagnostics, `uv-${options.textureSize}.db`), env,
  });
  const logFile = path.join(diagnostics, `baton-uv${options.textureSize}-server.log`);
  const fd = openSync(logFile, 'a');
  const child = spawn(launch.command, launch.args, {
    cwd: launch.cwd, env: launch.env, windowsHide: true, stdio: ['ignore', fd, fd],
  });
  closeSync(fd);
  let vramStarted = false;
  try {
    let ready = false;
    for (let i = 0; i < 180 && !ready; i += 1) {
      ready = await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false);
      if (!ready) await sleep(1000);
    }
    if (!ready) throw new Error('Native AMD ComfyUI did not start on port 8194.');

    const form = new FormData();
    form.append('image', new Blob([readFileSync(options.inputImage)], { type: 'image/png' }), `baton-uv-${options.textureSize}.png`);
    form.append('overwrite', 'true');
    const uploaded = await json('/upload/image', { method: 'POST', body: form });
    const workflow = buildUvAtlasWorkflow({ imageName: uploaded.name, glbPath: path.resolve(options.inputGlb), textureSize: options.textureSize });
    vramStarted = await fetch(url + '/af/vram/start', { method: 'POST', signal: AbortSignal.timeout(3000) }).then((r) => r.ok).catch(() => false);
    const started = Date.now();
    const queued = await json('/prompt', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: workflow }) });
    let item = null;
    for (let attempt = 0; attempt < 2400 && !item; attempt += 1) {
      const history = await json('/history/' + queued.prompt_id);
      const record = history[queued.prompt_id];
      const error = record?.status?.messages?.find((message) => message[0] === 'execution_error');
      if (error) throw new Error(error[1]?.exception_message || 'Native UV generation failed.');
      if (record?.status?.completed) item = Object.values(record.outputs || {}).flatMap((output) => output['3d'] || [])[0];
      if (!item) await sleep(1500);
    }
    if (!item) throw new Error('Native UV generation timed out after 60 minutes.');
    const params = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || '', type: item.type || 'output' });
    const response = await fetch(url + '/view?' + params);
    if (!response.ok) throw new Error(`GLB download failed: ${response.status}`);
    const raw = Buffer.from(await response.arrayBuffer());
    const normalized = normalizeGlb(raw);
    const validation = validateGeneratedMesh(normalized.buffer, { requireUvTexture: true, expectedUvTextureSize: options.textureSize });
    const outDir = path.resolve(options.outputDir || path.join(root, 'data', 'runtime', 'baton'));
    mkdirSync(outDir, { recursive: true });
    const glbPath = path.join(outDir, `amd-pixal3d-native-single-uv${options.textureSize}.glb`);
    writeFileSync(glbPath, normalized.buffer);
    const vram = vramStarted ? await json('/af/vram/stop', { method: 'POST' }).catch(() => null) : null;
    vramStarted = false;
    const report = buildUvAtlasReport({
      textureSize: options.textureSize, started, now: Date.now(), glbPath, normalized, validation, vram,
    });
    const jsonFile = path.resolve(options.jsonFile || path.join(diagnostics, `baton-uv${options.textureSize}.json`));
    writeFileSync(jsonFile, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  } finally {
    if (vramStarted) await fetch(url + '/af/vram/stop', { method: 'POST' }).catch(() => {});
    await fetch(url + '/free', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ unload_models: true, free_memory: true }) }).catch(() => {});
    if (child.exitCode === null && process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
```

- [x] **Step 4: Run static verification**

Run: `node --check scripts/native/run_native_uv_atlas.mjs && node --test tests/native-uv-atlas.test.mjs tests/pixal3d-native.test.mjs tests/pixal3d.test.mjs`
Expected: syntax check exits 0 and all applicable tests pass.

### Task 3: Native UV 2048 hardware and visual gate

**Files:**
- Create at runtime: `data/runtime/baton/amd-pixal3d-native-single-uv2048.glb`
- Create at runtime: `data/runtime/diagnostics/native-amd/baton-uv2048.json`
- Create at runtime: `preview-renders/baton-uv2048.png`
- Create at runtime: `preview-renders/baton-uv2048-atlas.png`

- [x] **Step 1: Confirm no active native job**

Run the existing process/job inspection used for native validation. Expected: no active Pixal3D job and no process bound to port 8194.

- [x] **Step 2: Run exactly one UV 2048 job**

```bash
AMD_UV_SIZE=2048 \
AMD_UV_INPUT="C:/Users/User/Documents/kimi/tasks/2026-11-01/18-59-30-895229e4/AstraForge-amd/data/assets/250bcec2-20af-4899-b954-0d706238aa6b.png" \
AMD_UV_GLB="C:/Users/User/Documents/kimi/tasks/2026-11-01/18-59-30-895229e4/AstraForge-amd/data/runtime/baton/amd-pixal3d-native-single-standard-uv1024.glb" \
node scripts/native/run_native_uv_atlas.mjs
```

Expected: `validation.accepted=true`, `denoiserResolution=1024`, `textureSize=2048`, and `fallbackEvents=[]`.

- [x] **Step 3: Render and inspect**

```bash
python render_native_result_preview.py \
  AstraForge-amd/data/runtime/baton/amd-pixal3d-native-single-uv2048.glb \
  preview-renders/baton-uv2048.png \
  --title "Baton | Native AMD | Denoiser 1024 | UV 2048"
```

Expected: preview tool reports atlas dimensions `[2048, 2048]`; inspect four views and the atlas before allowing Task 4.

### Task 4: Native UV 4096 hardware and final comparison

**Files:**
- Create at runtime: `data/runtime/baton/amd-pixal3d-native-single-uv4096.glb`
- Create at runtime: `data/runtime/diagnostics/native-amd/baton-uv4096.json`
- Create at runtime: `preview-renders/baton-uv4096.png`
- Create at runtime: `preview-renders/baton-uv4096-atlas.png`

- [x] **Step 1: Run UV 4096 only after UV 2048 passes**

Use the Task 3 command with `AMD_UV_SIZE=4096`. Expected: exact 4096 atlas, accepted GLB below 100 MiB, no fallback.

- [x] **Step 2: Render and compare all three atlas sizes**

```bash
python render_native_result_preview.py \
  AstraForge-amd/data/runtime/baton/amd-pixal3d-native-single-uv4096.glb \
  preview-renders/baton-uv4096.png \
  --title "Baton | Native AMD | Denoiser 1024 | UV 4096"
```

Expected: preview tool reports `[4096, 4096]`. Compare 1024, 2048, and 4096 for crust detail, scoring, backside/ends, stretching, color shift, and seams. Do not claim added detail solely from atlas size.

- [x] **Step 3: Final verification and delivery**

Run: `git diff --check && git status --short`
Expected: no whitespace errors; installer remains untracked and is never staged. Deliver GLB, atlas, preview, JSON, measured time, GLB size, and remaining visual limitations. Leave implementation uncommitted until the user explicitly requests a commit.
