// Pixal3D as the second 3D engine (docs/forge/pixal3d.md): workflow builder, settings, install detection, runtime start,
// GLB normalisation, the whole generation against a fake ComfyUI. No GPU, no real Pixal3D, no PowerShell.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { WebSocketServer } from 'ws';
import { generationSchema, pixal3dWorkflow, PIXAL3D_MODEL, MV_SLOTS } from '../server/schema.mjs';
import {
  pixal3dConfig, pixal3dInstallState, pixal3dStatus, resolveEngine, startPixal3dRuntime, pixal3dArgs, pixal3dEnv,
  PIXAL3D_REQUIREMENTS, _resetPixal3dRuntime, DEFAULT_PIXAL3D_DIR, DEFAULT_PIXAL3D_URL,
} from '../server/pixal3d.mjs';
import { normalizeGlb, buildGlb, parseGlb, glbBounds } from '../server/glb-normalize.mjs';
import { generatePixal3d, explainPixal3dError, PIXAL3D_PHASES } from '../server/pixal3d-run.mjs';
import { validateGeneratedMesh } from '../server/mesh-validation.mjs';

const tmp = mkdtempSync(path.join(tmpdir(), 'af-pixal3d-'));
after(() => rmSync(tmp, { recursive: true, force: true }));
const ID = '0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f';
const classes = (w) => Object.values(w).map((n) => n.class_type);

// ---------- workflow ----------

test('pixal3dWorkflow: four views -> multi-view graph (front, left, back, right = azimuth 0, 90, 180, 270)', () => {
  const w = pixal3dWorkflow({ front: 'f.png', left: 'l.png', back: 'b.png', right: 'r.png' }, { seed: 1000, prefix: 'astra/x' });
  assert.equal(w['1'].inputs.modelname, PIXAL3D_MODEL);
  assert.equal(w['1'].inputs.pixal3d_multiview, true);
  assert.equal(w['1'].inputs.keep_models_loaded, false);
  assert.equal(w['38'].class_type, 'Trellis2Pixal3DMultiViewConfig');
  assert.equal(w['38'].inputs.azimuths, '0,90,180,270');
  assert.equal(w['38'].inputs.elevations, '0,0,0,0');
  assert.deepEqual(w['37'].inputs, { fov: 0.1, unit: 'rad', extend_pixel: 0, mesh_scale: 1 });
  assert.deepEqual([w['30'], w['31'], w['32'], w['33']].map((n) => n.inputs.image), ['f.png', 'l.png', 'b.png', 'r.png']);
  assert.equal(w['2'].inputs.image, 'f.png');
  // ImageBatch chain keeps the order of the azimuth list
  assert.deepEqual(w['34'].inputs, { image1: ['30', 2], image2: ['31', 2] });
  assert.deepEqual(w['35'].inputs, { image1: ['34', 0], image2: ['32', 2] });
  assert.deepEqual(w['36'].inputs, { image1: ['35', 0], image2: ['33', 2] });
  assert.deepEqual(w['38'].inputs.images, ['36', 0]);
  for (const id of ['5', '6', '7']) {
    assert.deepEqual(w[id].inputs.moge_camera_config, ['38', 1]);
    assert.deepEqual(w[id].inputs.pixal3d_mv_views, ['38', 0]);
  }
  assert.equal(w['5'].inputs.seed, 1000);
  assert.equal(w['5'].inputs.sparse_structure_sampler, 'heun');
  assert.equal(w['6'].inputs.shape_sampler, 'heun');
  assert.equal(w['23'].inputs.target_face_num, 700000);
  assert.equal(w['25'].inputs.target_face_num, 700000);
  assert.equal(w['27'].inputs.filename_prefix, 'astra/x');
  assert.equal(w['27'].inputs.file_format, 'glb');
});

test('pixal3dWorkflow: front only -> single-view graph with the MoGe camera and no view nodes', () => {
  const w = pixal3dWorkflow({ front: 'f.png' }, { seed: 7, targetFaces: 200000, sampler: 'euler' });
  assert.equal(w['1'].inputs.pixal3d_multiview, false);
  for (const id of ['30', '31', '32', '33', '34', '35', '36', '37', '38']) assert.equal(w[id], undefined, 'node ' + id);
  for (const id of ['5', '6', '7']) {
    assert.deepEqual(w[id].inputs.moge_camera_config, ['4', 3]);
    assert.equal(w[id].inputs.pixal3d_mv_views, undefined);
  }
  assert.equal(w['5'].inputs.seed, 7);
  assert.equal(w['5'].inputs.sparse_structure_sampler, 'euler');
  assert.equal(w['23'].inputs.target_face_num, 200000);
  assert.equal(w['3'].inputs.remove_background, false);
  assert.equal(pixal3dWorkflow({ front: 'f.png' }, { removeBackground: true })['3'].inputs.remove_background, true);
});

test('pixal3dWorkflow: any subset with a front view; azimuths follow the views that exist', () => {
  const two = pixal3dWorkflow({ front: 'f.png', back: 'b.png' });
  assert.equal(two['38'].inputs.azimuths, '0,180');
  assert.equal(two['38'].inputs.elevations, '0,0');
  assert.deepEqual(two['34'].inputs, { image1: ['30', 2], image2: ['32', 2] });
  assert.equal(two['35'], undefined);
  assert.deepEqual(two['38'].inputs.images, ['34', 0]);
  const three = pixal3dWorkflow({ front: 'f.png', left: 'l.png', right: 'r.png' });
  assert.equal(three['38'].inputs.azimuths, '0,90,270');
  assert.deepEqual(three['35'].inputs, { image1: ['34', 0], image2: ['33', 2] });
  assert.equal(three['36'], undefined);
  // views outside the model's four (frontLeft / frontRight of the view sets) are ignored
  assert.equal(pixal3dWorkflow({ front: 'f.png', frontLeft: 'x.png' })['1'].inputs.pixal3d_multiview, false);
  assert.throws(() => pixal3dWorkflow({ back: 'b.png' }), /front view/);
  assert.throws(() => pixal3dWorkflow({}), /front view/);
});

test('pixal3dWorkflow is shape only: no texturing, rasterising or nvdiffrast node (non-commercial licence) can appear', () => {
  const forbidden = /nvdiff|Nvdiffrast|Texturing|TexSlat|UnWrap|Rasteriz|Render|Bake|Texture/i;
  for (const images of [{ front: 'f' }, { front: 'f', left: 'l' }, { front: 'f', left: 'l', back: 'b', right: 'r' }]) {
    const used = classes(pixal3dWorkflow(images));
    assert.deepEqual(used.filter((c) => forbidden.test(c)), []);
    assert.ok(used.every((c) => /^Trellis2/.test(c) || c === 'ImageBatch'), 'only Trellis2 shape nodes and ImageBatch: ' + used.join(','));
  }
  assert.ok(MV_SLOTS.includes('front'));
});

test('pixal3dWorkflow reproduces the graphs that were evaluated on the militia (when the evaluation files exist)', { skip: !existsSync('C:/AstraForge/incoming/units_1013/militia/pixal3d/pixal3d_mv.api.json') }, () => {
  // the evaluated graphs used meshlib for the second hole fill; it is replaced by CuMesh (MIT) — meshlib is non-commercial only
  const noMeshlib = (g) => { if (g['24']?.class_type === 'Trellis2FillHolesNicelyWithMeshlib') g['24'] = { class_type: 'Trellis2FillHolesWithCuMesh', inputs: { mesh: ['23', 0], max_permieters: 1 } }; return g; };
  const load = (f) => noMeshlib(JSON.parse(readFileSync('C:/AstraForge/incoming/units_1013/militia/pixal3d/' + f, 'utf8')));
  const norm = (g) => JSON.parse(JSON.stringify(g, (k, v) => (k === 'image' && typeof v === 'string' ? 'IMG' : k === 'filename_prefix' ? 'P' : v)));
  const sorted = (o) => JSON.stringify(o, (k, v) => (v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b))) : v));
  assert.equal(sorted(norm(pixal3dWorkflow({ front: 'f', left: 'l', back: 'b', right: 'r' }, { seed: 1000 }))), sorted(norm(load('pixal3d_mv.api.json'))));
  assert.equal(sorted(norm(pixal3dWorkflow({ front: 'f' }, { seed: 1000 }))), sorted(norm(load('pixal3d_single.api.json'))));
});

test('generation schema: engine defaults to hunyuan (old API clients unchanged), accepts pixal3d and auto', () => {
  assert.equal(generationSchema.parse({ imageId: ID }).engine, 'hunyuan');
  assert.equal(generationSchema.parse({ imageId: ID, engine: 'pixal3d' }).engine, 'pixal3d');
  assert.equal(generationSchema.parse({ viewSetId: ID, engine: 'auto' }).engine, 'auto');
  assert.throws(() => generationSchema.parse({ imageId: ID, engine: 'trellis' }));
});

// ---------- settings and installation ----------

test('settings: ASTRA_PIXAL3D_DIR beats config/paths.json beats the default; the URL must be local', () => {
  const app = path.join(tmp, 'app1');
  mkdirSync(path.join(app, 'config'), { recursive: true });
  assert.equal(pixal3dConfig({ env: {}, appRoot: app }).dir, path.resolve(DEFAULT_PIXAL3D_DIR));
  assert.equal(pixal3dConfig({ env: {}, appRoot: app }).source, 'default');
  assert.equal(pixal3dConfig({ env: {}, appRoot: app }).url, DEFAULT_PIXAL3D_URL);
  writeFileSync(path.join(app, 'config', 'paths.json'), JSON.stringify({ pixal3dDir: 'D:/Engines/Pixal3D' }));
  const fromFile = pixal3dConfig({ env: {}, appRoot: app });
  assert.equal(fromFile.dir, path.resolve('D:/Engines/Pixal3D'));
  assert.equal(fromFile.source, 'paths.json');
  const fromEnv = pixal3dConfig({ env: { ASTRA_PIXAL3D_DIR: 'E:/px', ASTRA_PIXAL3D_URL: 'http://127.0.0.1:9191/' }, appRoot: app });
  assert.equal(fromEnv.dir, path.resolve('E:/px'));
  assert.equal(fromEnv.source, 'env');
  assert.equal(fromEnv.url, 'http://127.0.0.1:9191');
  assert.equal(fromEnv.port, 9191);
  assert.equal(pixal3dConfig({ env: {}, appRoot: app }).port, 8191);
  assert.throws(() => pixal3dConfig({ env: { ASTRA_PIXAL3D_URL: 'http://example.com:8191' }, appRoot: app }), /local/);
  writeFileSync(path.join(app, 'config', 'paths.json'), '{ broken');
  assert.equal(pixal3dConfig({ env: {}, appRoot: app }).source, 'default', 'a broken paths.json does not break the studio');
});

function fakeInstall(name, { skip = [] } = {}) {
  const dir = path.join(tmp, name);
  for (const [rel] of PIXAL3D_REQUIREMENTS) {
    if (skip.includes(rel)) continue;
    mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    writeFileSync(path.join(dir, rel), 'x');
  }
  mkdirSync(dir, { recursive: true });
  return { dir, url: 'http://127.0.0.1:8191', port: 8191, source: 'env' };
}

test('install detection: complete / missing folder / missing pieces are told apart, in Russian', () => {
  const full = fakeInstall('full');
  assert.deepEqual(pixal3dInstallState(full), { installed: true, missing: [], message: '' });

  const none = { dir: path.join(tmp, 'nothing-here'), url: DEFAULT_PIXAL3D_URL, port: 8191 };
  const noFolder = pixal3dInstallState(none);
  assert.equal(noFolder.installed, false);
  assert.match(noFolder.message, /Pixal3D is not installed/);
  assert.match(noFolder.message, /ASTRA_PIXAL3D_DIR/);

  const dino = 'ComfyUI/models/facebook/dinov3-vitl16-pretrain-lvd1689m/model.safetensors';
  const partial = pixal3dInstallState(fakeInstall('partial', { skip: [dino] }));
  assert.equal(partial.installed, false);
  assert.deepEqual(partial.missing.map((m) => m.path), [dino]);
  assert.match(partial.message, /DINOv3/);
});

test('resolveEngine: hunyuan stays, auto picks Pixal3D only when installed, explicit pixal3d without an installation is an error', () => {
  const full = fakeInstall('full2');
  const none = { dir: path.join(tmp, 'absent'), url: DEFAULT_PIXAL3D_URL, port: 8191 };
  assert.equal(resolveEngine(undefined, none), 'hunyuan');
  assert.equal(resolveEngine('hunyuan', full), 'hunyuan');
  assert.equal(resolveEngine('auto', full), 'pixal3d');
  assert.equal(resolveEngine('auto', none), 'hunyuan');
  assert.equal(resolveEngine('pixal3d', full), 'pixal3d');
  assert.throws(() => resolveEngine('pixal3d', none), (e) => e.code === 'pixal3d_not_installed' && /Pixal3D is not installed/.test(e.message));
  assert.throws(() => resolveEngine('trellis', full), /Unknown 3D engine/);
});

test('pixal3dStatus: not installed -> never probes the network; installed -> running flag from /system_stats', async () => {
  const none = { dir: path.join(tmp, 'absent2'), url: DEFAULT_PIXAL3D_URL, port: 8191 };
  let calls = 0;
  const st = await pixal3dStatus({ cfg: none, fetchFn: async () => { calls += 1; return { ok: true }; } });
  assert.equal(st.installed, false);
  assert.equal(st.running, false);
  assert.equal(calls, 0);
  const full = fakeInstall('full3');
  assert.equal((await pixal3dStatus({ cfg: full, fetchFn: async () => ({ ok: true }) })).running, true);
  assert.equal((await pixal3dStatus({ cfg: full, fetchFn: async () => { throw new Error('ECONNREFUSED'); } })).running, false);
});

// ---------- runtime ----------

function fakeChild(pid = 4242) {
  const c = new EventEmitter();
  Object.assign(c, { pid, killed: false, exitCode: null, unref() {}, kill() { this.killed = true; } });
  return c;
}

test('runtime start: not installed -> clear error and nothing is spawned', async () => {
  _resetPixal3dRuntime();
  let spawned = 0;
  await assert.rejects(startPixal3dRuntime({ cfg: { dir: path.join(tmp, 'gone'), url: DEFAULT_PIXAL3D_URL, port: 8191 }, spawnFn: () => { spawned += 1; }, runtimeDir: path.join(tmp, 'rt0') }),
    (e) => e.code === 'pixal3d_not_installed');
  assert.equal(spawned, 0);
});

test('runtime start: an engine that already answers is reused, not started twice', async () => {
  _resetPixal3dRuntime();
  let spawned = 0;
  const r = await startPixal3dRuntime({ cfg: fakeInstall('reuse'), fetchFn: async () => ({ ok: true }), spawnFn: () => { spawned += 1; }, runtimeDir: path.join(tmp, 'rt1') });
  assert.deepEqual(r, { running: true, reused: true });
  assert.equal(spawned, 0);
});

test('runtime start: spawns the portable python hidden, with the same arguments and environment as the bat file, and waits for it', async () => {
  _resetPixal3dRuntime();
  const cfg = fakeInstall('spawn'), runtimeDir = path.join(tmp, 'rt2');
  let up = false, spawnedArgs = null, probes = 0;
  const fetchFn = async () => { probes += 1; if (probes >= 4) up = true; return up ? { ok: true } : (() => { throw new Error('ECONNREFUSED'); })(); };
  const messages = [];
  const r = await startPixal3dRuntime({
    cfg, runtimeDir, fetchFn, sleep: async () => {}, onProgress: (t) => messages.push(t),
    spawnFn: (exe, args, options) => { spawnedArgs = { exe, args, options }; return fakeChild(); },
    timeoutMs: 5000,
  });
  assert.equal(r.running, true);
  assert.equal(r.pid, 4242);
  assert.equal(spawnedArgs.exe, path.join(cfg.dir, 'python_embeded', 'python.exe'));
  assert.equal(spawnedArgs.options.cwd, cfg.dir);
  assert.equal(spawnedArgs.options.windowsHide, true);
  const a = spawnedArgs.args;
  assert.deepEqual(a.slice(0, 3), ['-I', '-W', 'ignore::FutureWarning']);
  assert.equal(a[a.indexOf('--port') + 1], '8191');
  assert.equal(a[a.indexOf('--listen') + 1], '127.0.0.1');
  assert.ok(a.includes('--disable-auto-launch'));
  assert.ok(a.includes('--windows-standalone-build'));
  assert.equal(a[a.indexOf('--temp-directory') + 1], path.join(runtimeDir, 'pixal3d-temp'), 'the marker scripts/Stop.ps1 uses to find the instance the studio started');
  assert.equal(a[a.indexOf('--database-url') + 1], 'sqlite:///' + path.join(runtimeDir, 'pixal3d.db').replaceAll('\\', '/'), 'own database: no clash with an instance started by hand');
  assert.equal(spawnedArgs.options.env.HF_HOME, path.join(cfg.dir, 'cache', 'huggingface'));
  assert.equal(spawnedArgs.options.env.PYTHONUTF8, '1');
  assert.ok(messages.some((m) => /Starting the Pixal3D engine/.test(m)));
  assert.equal(readFileSync(path.join(runtimeDir, 'pixal3d.pid'), 'utf8'), '4242');
  _resetPixal3dRuntime();
});

test('runtime start: the environment drops system CUDA / Python variables like the bat file does', () => {
  const cfg = { dir: 'C:/AstraForge/ComfyUI-Pixal3D', port: 8191 };
  const env = pixal3dEnv(cfg, { cuda_path: 'x', CUDA_HOME: 'y', PYTHONPATH: 'z', VIRTUAL_ENV: 'v', Path: 'C:\\Tools', windir: 'C:\\Windows', KEEP: '1' });
  for (const k of ['cuda_path', 'CUDA_HOME', 'PYTHONPATH', 'VIRTUAL_ENV']) assert.equal(env[k], undefined, k);
  assert.equal(env.KEEP, '1');
  assert.match(env.Path, /^C:\\Windows[\\/]System32;.*C:\\Tools$/, 'System32 first, the old PATH kept, one PATH key only');
  assert.equal(Object.keys(env).filter((k) => k.toUpperCase() === 'PATH').length, 1);
  assert.match(pixal3dEnv(cfg, {}).PATH, /^C:\\Windows[\\/]System32;/, 'default Windows folder when windir is unset');
  const args = pixal3dArgs({ port: 8200 }, 'T');
  assert.equal(args[args.indexOf('--port') + 1], '8200');
  assert.deepEqual(args.slice(-3), ['--disable-auto-launch', '--temp-directory', 'T']);
});

test('runtime start: an engine that dies on start is reported with the end of its log; a silent one times out', async () => {
  _resetPixal3dRuntime();
  const cfg = fakeInstall('dies'), runtimeDir = path.join(tmp, 'rt3');
  mkdirSync(runtimeDir, { recursive: true });
  writeFileSync(path.join(runtimeDir, 'pixal3d.log'), 'line1\nImportError: flash_attn failed\n');
  let child;
  await assert.rejects(startPixal3dRuntime({
    cfg, runtimeDir, fetchFn: async () => { throw new Error('ECONNREFUSED'); }, sleep: async () => { child.emit('exit', 1); },
    spawnFn: () => (child = fakeChild()), timeoutMs: 5000,
  }), /exited on startup[\s\S]*flash_attn failed/);
  _resetPixal3dRuntime();
  await assert.rejects(startPixal3dRuntime({
    cfg, runtimeDir: path.join(tmp, 'rt4'), fetchFn: async () => { throw new Error('ECONNREFUSED'); }, sleep: async () => {},
    spawnFn: () => fakeChild(), timeoutMs: 0,
  }), /did not respond within 0 s/);
  _resetPixal3dRuntime();
});

// ---------- GLB normalisation ----------

/** A box mesh in a glTF file: positions float32 (optionally interleaved with normals), indices uint32. */
function boxGlb({ min = [-0.4, -0.3, -0.1], max = [0.6, 0.7, 0.2], interleaved = false } = {}) {
  const corners = [];
  for (const x of [min[0], max[0]]) for (const y of [min[1], max[1]]) for (const z of [min[2], max[2]]) corners.push([x, y, z]);
  const normal = [0, 0, 1], indices = [0, 1, 2, 1, 3, 2, 4, 6, 5, 5, 6, 7];
  const stride = interleaved ? 24 : 12;
  const vbuf = Buffer.alloc(corners.length * stride);
  corners.forEach((p, i) => { p.forEach((v, a) => vbuf.writeFloatLE(v, i * stride + a * 4)); if (interleaved) normal.forEach((v, a) => vbuf.writeFloatLE(v, i * stride + 12 + a * 4)); });
  const ibuf = Buffer.alloc(indices.length * 4);
  indices.forEach((v, i) => ibuf.writeUInt32LE(v, i * 4));
  const bin = Buffer.concat([vbuf, ibuf]);
  const json = {
    asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    meshes: [{ primitives: [{ attributes: interleaved ? { POSITION: 0, NORMAL: 1 } : { POSITION: 0 }, indices: interleaved ? 2 : 1, mode: 4 }] }],
    accessors: interleaved
      ? [{ bufferView: 0, componentType: 5126, count: 8, type: 'VEC3', min, max }, { bufferView: 0, byteOffset: 12, componentType: 5126, count: 8, type: 'VEC3' }, { bufferView: 1, componentType: 5125, count: indices.length, type: 'SCALAR' }]
      : [{ bufferView: 0, componentType: 5126, count: 8, type: 'VEC3', min, max }, { bufferView: 1, componentType: 5125, count: indices.length, type: 'SCALAR' }],
    bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: vbuf.length, ...(interleaved ? { byteStride: 24 } : {}) }, { buffer: 0, byteOffset: vbuf.length, byteLength: ibuf.length }],
    buffers: [{ byteLength: bin.length }],
  };
  return buildGlb(json, bin);
}

test('normalizeGlb: centred on the origin, height exactly 2, uniform scale, indices and the rest untouched', () => {
  const src = boxGlb();   // extent 1.0 x 1.0 x 0.3, centre (0.1, 0.2, 0.05)
  const r = normalizeGlb(src);
  assert.equal(r.buffer.length % 4, 0);
  const b = glbBounds(r.buffer);
  assert.ok(Math.abs(b.extent[1] - 2) < 1e-6);
  assert.ok(Math.abs(b.extent[0] - 2) < 1e-6);
  assert.ok(Math.abs(b.extent[2] - 0.6) < 1e-6, 'uniform scale keeps the proportions');
  for (let a = 0; a < 3; a++) assert.ok(Math.abs(b.min[a] + b.max[a]) < 1e-6, 'centred on axis ' + a);
  assert.ok(Math.abs(r.scale - 2) < 1e-6);
  const out = parseGlb(r.buffer), original = parseGlb(src);
  assert.deepEqual(out.json.accessors[0].min.map((v) => Math.round(v * 1e5) / 1e5), [-1, -1, -0.3]);
  assert.deepEqual(out.json.accessors[0].max.map((v) => Math.round(v * 1e5) / 1e5), [1, 1, 0.3]);
  assert.deepEqual(out.bin.subarray(8 * 12), original.bin.subarray(8 * 12), 'index buffer unchanged');
  assert.equal(validateGeneratedMesh(r.buffer).triangles, 4);
});

test('normalizeGlb: interleaved vertex data keeps the normals, a second run changes nothing', () => {
  const r = normalizeGlb(boxGlb({ interleaved: true }));
  const { json, bin } = parseGlb(r.buffer);
  for (let i = 0; i < 8; i++) {
    assert.equal(bin.readFloatLE(i * 24 + 12 + 8), 1, 'normal z of vertex ' + i);
    assert.equal(bin.readFloatLE(i * 24 + 12), 0);
  }
  assert.equal(json.accessors[0].min[1], -1);
  const again = normalizeGlb(r.buffer);
  assert.ok(Math.abs(again.scale - 1) < 1e-6);
  assert.deepEqual(glbBounds(again.buffer).min.map((v) => Math.round(v * 1e5)), glbBounds(r.buffer).min.map((v) => Math.round(v * 1e5)));
});

test('normalizeGlb: refuses broken or flat files with a Russian message', () => {
  assert.throws(() => normalizeGlb(Buffer.from('not a glb at all, just some bytes here....')), /Invalid GLB/);
  assert.throws(() => normalizeGlb(boxGlb({ min: [0, 0, 0], max: [1, 0, 1] })), /has no height/);
  const truncated = boxGlb().subarray(0, 60);
  assert.throws(() => normalizeGlb(truncated), /Invalid GLB/);
});

test('normalizeGlb gives the same vertices as the evaluated Python normaliser (when the militia files exist)',
  { skip: !existsSync('C:/AstraForge/incoming/units_1013/militia/mv/mesh_mv_pixal3d.glb') }, () => {
    const raw = readFileSync('C:/AstraForge/incoming/units_1013/militia/pixal3d/pixal3d_mv.glb');
    const ref = parseGlb(readFileSync('C:/AstraForge/incoming/units_1013/militia/mv/mesh_mv_pixal3d.glb'));
    const out = parseGlb(normalizeGlb(raw).buffer);
    const at = (g) => { const acc = g.json.accessors[g.json.meshes[0].primitives[0].attributes.POSITION]; return [acc.count, g.json.bufferViews[acc.bufferView].byteOffset || 0]; };
    const [n, oa] = at(out), [m, ob] = at(ref);
    assert.equal(n, m);
    let worst = 0;
    for (let i = 0; i < n * 3; i++) worst = Math.max(worst, Math.abs(out.bin.readFloatLE(oa + i * 4) - ref.bin.readFloatLE(ob + i * 4)));
    assert.ok(worst < 1e-6, 'max vertex difference ' + worst);
  });

// ---------- a whole generation against a fake ComfyUI ----------

function fakeEngine({ outcome = 'success', pollsBeforeDone = 1, foreign = false } = {}) {
  const state = { uploads: [], prompt: null, frees: 0, interrupts: 0, queueDeletes: 0, polls: 0, vramStarted: 0, sockets: [] };
  const glb = boxGlb({ min: [-0.47, -0.5, -0.1], max: [0.47, 0.5, 0.1] });   // what Pixal3D exports: ~1 unit tall
  const needed = new Set(Object.values(pixal3dWorkflow({ front: 'f', left: 'l', back: 'b', right: 'r' })).map((n) => n.class_type));
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks);
      const json = (v, code = 200) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(v)); };
      if (url.pathname === '/system_stats') return json({ system: {}, devices: [] });
      if (url.pathname === '/object_info') return json(Object.fromEntries([...needed].map((c) => [c, {}])));
      if (url.pathname === '/upload/image') {
        const name = /filename="([^"]+)"/.exec(body.toString('latin1'))?.[1] || 'x.png';
        state.uploads.push(name);
        return json({ name, subfolder: '', type: 'input' });
      }
      if (url.pathname === '/af/vram/start') { state.vramStarted += 1; return json({ baseline_used_mb: 1500 }); }
      if (url.pathname === '/af/vram/stop') return json({ baseline_used_mb: 1500, peak_used_mb: 11200, peak_delta_mb: 9700, samples: 10 });
      if (url.pathname === '/prompt') {
        state.prompt = JSON.parse(body.toString('utf8'));
        setTimeout(() => {
          const send = (type, data) => state.sockets.forEach((s) => s.send(JSON.stringify({ type, data: { prompt_id: 'p1', ...data } })));
          send('executing', { node: '1' });
          send('executing', { node: '5' }); send('progress', { value: 5, max: 10, node: '5' });
          send('executing', { node: '7' }); send('progress', { value: 12, max: 12, node: '7' });
          send('executing', { node: '27' });
        }, 30);
        return json({ prompt_id: 'p1', number: 1 });
      }
      if (url.pathname === '/history/p1') {
        state.polls += 1;
        if (state.polls <= pollsBeforeDone) return json({});
        if (outcome === 'oom') return json({ p1: { status: { status_str: 'error', completed: false, messages: [['execution_error', { exception_message: 'CUDA out of memory. Tried to allocate 2.00 GiB' }]] }, outputs: {} } });
        return json({ p1: { status: { status_str: 'success', completed: true, messages: [] }, outputs: { 27: { '3d': [{ filename: 'px_00001_.glb', subfolder: 'astra', type: 'output' }] } } } });
      }
      if (url.pathname === '/view') { res.writeHead(200, { 'Content-Type': 'model/gltf-binary' }); return res.end(glb); }
      if (url.pathname === '/queue' && req.method === 'POST') { state.queueDeletes += 1; return json({}); }
      if (url.pathname === '/queue') return json({ queue_running: outcome === 'abort' && state.prompt ? [[1, 'p1']] : foreign && !state.prompt ? [[1, 'someone-else']] : [], queue_pending: [] });
      if (url.pathname === '/interrupt') { state.interrupts += 1; return json({}); }
      if (url.pathname === '/free') { state.frees += 1; return json({}); }
      json({ error: 'not found: ' + url.pathname }, 404);
    });
  });
  const wss = new WebSocketServer({ server });
  wss.on('connection', (socket) => { state.sockets.push(socket); });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({
    state, url: 'http://127.0.0.1:' + server.address().port, glb,
    close: () => new Promise((r) => { wss.clients.forEach((c) => c.terminate()); wss.close(); server.closeAllConnections?.(); server.close(r); }),
  })));
}

const PNG = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), Buffer.alloc(32, 3)]);
function cutFiles(slots) {
  const dir = mkdtempSync(path.join(tmp, 'cuts-'));
  return Object.fromEntries(slots.map((s) => { const f = path.join(dir, `cut_${s}.png`); writeFileSync(f, PNG); return [s, f]; }));
}
const fakeLock = () => { const lock = { released: 0, release: async () => { lock.released += 1; } }; return lock; };
const startOk = async () => ({ running: true, reused: true });

test('generatePixal3d: uploads the cut-outs, queues the graph, follows the progress, returns a normalised GLB, frees the card', async () => {
  const engine = await fakeEngine();
  const lock = fakeLock(), progress = [], waits = [];
  lock.handover = { waited: false, freed: ['hunyuan'], skipped: [] };
  let acquired = null, promptSeen = null, freed = 0;
  try {
    const result = await generatePixal3d({
      images: cutFiles(['front', 'left', 'back', 'right']), seed: 1000, tag: 'ab12cd34', cfg: { url: engine.url, dir: tmp, port: 1 },
      onProgress: (p) => progress.push(p), onPrompt: (id) => { promptSeen = id; }, onWait: (t) => waits.push(t),
      deps: { start: startOk, acquire: async (name, opts) => { acquired = { name, opts }; return lock; }, free: async () => { freed += 1; }, tickMs: 120 },
    });
    assert.equal(acquired.name, 'pixal3d', 'the card is taken for the Pixal3D side of the hand-over');
    assert.equal(acquired.opts.urls.pixal3d, engine.url);
    assert.deepEqual(engine.state.uploads, ['astra-px-ab12cd34-front.png', 'astra-px-ab12cd34-left.png', 'astra-px-ab12cd34-back.png', 'astra-px-ab12cd34-right.png']);
    const graph = engine.state.prompt.prompt;
    assert.equal(graph['38'].inputs.azimuths, '0,90,180,270');
    assert.equal(graph['30'].inputs.image, 'astra-px-ab12cd34-front.png');
    assert.equal(graph['33'].inputs.image, 'astra-px-ab12cd34-right.png');
    assert.equal(graph['27'].inputs.filename_prefix, 'astra/px-ab12cd34');
    assert.equal(graph['5'].inputs.seed, 1000);
    assert.equal(promptSeen, 'p1');
    assert.equal(result.promptId, 'p1');
    assert.deepEqual(result.handover.freed, ['hunyuan'], 'the hand-over report travels with the result (stored on the job)');
    assert.deepEqual(result.slots, ['front', 'left', 'back', 'right']);
    assert.equal(result.vram.peak_used_mb, 11200);
    // geometry: normalised like the Hunyuan seeds
    const b = glbBounds(result.buffer);
    assert.ok(Math.abs(b.extent[1] - 2) < 1e-5);
    for (let a = 0; a < 3; a++) assert.ok(Math.abs(b.min[a] + b.max[a]) < 1e-5);
    assert.ok(Math.abs(result.normalization.scale - 2) < 0.02);
    assert.equal(validateGeneratedMesh(result.buffer).accepted, true);
    // progress: monotone, phase labels from the websocket, never 100 (the job sets that)
    const percents = progress.map((p) => p.percent);
    assert.deepEqual(percents, [...percents].sort((x, y) => x - y));
    assert.ok(percents.every((p) => p <= 98));
    assert.ok(progress.some((p) => p.message === 'Building the coarse shape · 5/10'), JSON.stringify(progress.slice(0, 12)));
    assert.ok(progress.some((p) => /Refining the shape · 1024 · 12\/12/.test(p.message)));
    assert.ok(progress.some((p) => p.message.startsWith('Fetching')));
    // the engine reports no steps inside a phase: the follower shows the time spent and creeps, but never past the end of the phase
    const ticks = progress.filter((p) => /· \d+:\d\d$/.test(p.message));
    assert.ok(ticks.length >= 2, 'time ticks during a long phase: ' + JSON.stringify(progress.map((p) => p.message)));
    for (const tick of ticks) assert.ok(tick.percent <= PIXAL3D_PHASES.Trellis2ExportMesh[0]);
    assert.equal(lock.released, 1);
    assert.equal(freed, 1, 'the engine is asked to give the memory back after the job');
    assert.equal(engine.state.vramStarted, 1);
  } finally { await engine.close(); }
});

test('generatePixal3d: a job of another program already on the Pixal3D engine is announced and waited for, not an error', async () => {
  const engine = await fakeEngine({ foreign: true, pollsBeforeDone: 0 });
  const waits = [];
  try {
    const result = await generatePixal3d({
      images: cutFiles(['front']), tag: 'queue', cfg: { url: engine.url, dir: tmp, port: 1 }, onWait: (t) => waits.push(t),
      deps: { start: startOk, acquire: async () => fakeLock(), free: async () => {} },
    });
    assert.equal(result.promptId, 'p1');
    assert.ok(waits.some((t) => /other jobs/.test(t)), JSON.stringify(waits));
  } finally { await engine.close(); }
});

test('generatePixal3d: one front view builds the single-view graph', async () => {
  const engine = await fakeEngine({ pollsBeforeDone: 0 });
  try {
    const result = await generatePixal3d({
      images: { ...cutFiles(['front']), left: null }, seed: 5, tag: 'single', cfg: { url: engine.url, dir: tmp, port: 1 },
      deps: { start: startOk, acquire: async () => fakeLock(), free: async () => {} },
    });
    assert.deepEqual(result.slots, ['front']);
    assert.equal(engine.state.prompt.prompt['1'].inputs.pixal3d_multiview, false);
    assert.equal(engine.state.uploads.length, 1);
  } finally { await engine.close(); }
});

test('generatePixal3d: an out-of-memory error from the engine becomes a clear Russian message; the lock is still released', async () => {
  const engine = await fakeEngine({ outcome: 'oom' });
  const lock = fakeLock();
  let freed = 0;
  try {
    await assert.rejects(generatePixal3d({
      images: cutFiles(['front', 'back']), tag: 'oom', cfg: { url: engine.url, dir: tmp, port: 1 },
      deps: { start: startOk, acquire: async () => lock, free: async () => { freed += 1; } },
    }), /Pixal3D ran out of VRAM/);
    assert.equal(lock.released, 1);
    assert.equal(freed, 1);
  } finally { await engine.close(); }
});

test('generatePixal3d: cancelling interrupts the running prompt on the Pixal3D engine and releases the lock', async () => {
  const engine = await fakeEngine({ outcome: 'abort', pollsBeforeDone: 1e9 });
  const lock = fakeLock(), controller = new AbortController();
  try {
    await assert.rejects(generatePixal3d({
      images: cutFiles(['front']), tag: 'stop', cfg: { url: engine.url, dir: tmp, port: 1 }, signal: controller.signal,
      onPrompt: () => setTimeout(() => controller.abort(), 50),
      deps: { start: startOk, acquire: async () => lock, free: async () => {} },
    }), (e) => e.name === 'AbortError');
    assert.equal(engine.state.interrupts, 1, 'POST /interrupt for the running prompt');
    assert.equal(engine.state.queueDeletes, 0);
    assert.equal(lock.released, 1);
  } finally { await engine.close(); }
});

test('generatePixal3d: without a front view or without an installation it stops before touching the GPU', async () => {
  let acquired = 0;
  const acquire = async () => { acquired += 1; return fakeLock(); };
  await assert.rejects(generatePixal3d({ images: { left: '/x/l.png' }, deps: { acquire } }), /front view/);
  await assert.rejects(generatePixal3d({ images: cutFiles(['front']), cfg: { dir: path.join(tmp, 'no-px'), url: DEFAULT_PIXAL3D_URL, port: 8191 }, deps: { acquire } }),
    (e) => e.code === 'pixal3d_not_installed' && /Pixal3D is not installed/.test(e.message));
  assert.equal(acquired, 0);
});

test('error texts and progress phases', () => {
  assert.match(explainPixal3dError('torch.OutOfMemoryError: CUDA out of memory'), /ran out of VRAM/);
  assert.match(explainPixal3dError('FileNotFoundError: dinov3 model not found'), /DINOv3/);
  assert.equal(explainPixal3dError('Processing interrupted'), 'Generation aborted');
  assert.equal(explainPixal3dError('something odd'), 'something odd');
  // the phases cover the whole graph, never go backwards, and stay below 100
  const ranges = Object.values(PIXAL3D_PHASES);
  assert.ok(ranges.every(([from, to]) => from <= to && to <= 98));
  const used = new Set(classes(pixal3dWorkflow({ front: 'f', left: 'l' })));
  for (const cls of used) if (/^Trellis2/.test(cls) && !/LoadImage/.test(cls)) assert.ok(PIXAL3D_PHASES[cls], 'no progress phase for ' + cls);
});

test('pixal3dWorkflow never uses meshlib (non-commercial licence)', () => {
  for (const views of [{ front: 'f' }, { front: 'f', left: 'l', back: 'b', right: 'r' }]) {
    const g = pixal3dWorkflow(views, { seed: 1 });
    assert.ok(!JSON.stringify(g).toLowerCase().includes('meshlib'), 'graph must not contain meshlib nodes');
  }
});
