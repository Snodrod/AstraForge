// "Views": multi-view workflow, schema, prompts and the /api/views routes (stub runner: no ComfyUI, Codex or Python).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import express from 'express';
import { generationSchema, hunyuanWorkflow, hunyuanMultiViewWorkflow, MV_CHECKPOINT, MV_SLOTS } from '../server/schema.mjs';
import { createViewsService, registerViews, generateSchema } from '../server/views.mjs';
import * as real from '../scripts/views/views_generate.mjs';
import { SLOTS, SLOT_META, codexPrompt, generationOrder, GENERATED_SLOTS } from '../scripts/views/views_prompts.mjs';
import { mvadapterWorkflow, safeSize } from '../scripts/views/views_mvadapter.mjs';

const PNG = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), Buffer.alloc(24, 7)]);
const ID = '0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f';
const uid = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;   // one concept image per test: sets are reused per image
const MISSING = uid(999);

test('schema: a generation job may name a view set instead of a prompt', () => {
  const ok = generationSchema.parse({ imageId: ID, viewSetId: ID });
  assert.equal(ok.viewSetId, ID);
  assert.equal(generationSchema.parse({ viewSetId: ID }).prompt, '');
  assert.throws(() => generationSchema.parse({ imageId: ID, viewSetId: 'not-a-uuid' }));
  assert.throws(() => generationSchema.parse({}), /concept/);
  assert.equal(generationSchema.parse({ imageId: ID }).viewSetId, undefined);
});

test('multi-view workflow: front + left + back + right in Hunyuan3Dv2ConditioningMultiView', () => {
  const w = hunyuanMultiViewWorkflow({ front: 'f.png', left: 'l.png', back: 'b.png', right: 'r.png' }, MV_CHECKPOINT, 'high', 703, 'p', 0);
  assert.equal(w['1'].inputs.ckpt_name, 'hunyuan3d-dit-v2-mv.safetensors');
  assert.equal(w['4'].class_type, 'Hunyuan3Dv2ConditioningMultiView');
  assert.deepEqual(Object.keys(w['4'].inputs).sort(), ['back', 'front', 'left', 'right']);
  for (const slot of MV_SLOTS) {
    const [encode] = w['4'].inputs[slot];
    assert.equal(w[encode].class_type, 'CLIPVisionEncode');
    assert.equal(w[encode].inputs.crop, 'none');
    assert.equal(w[w[encode].inputs.image[0]].class_type, 'LoadImage');
  }
  assert.equal(w['22'].inputs.image, 'l.png');
  assert.equal(w['32'].inputs.image, 'r.png');
  assert.equal(w['12'].inputs.image, 'b.png');
  // the rest of the graph is the single-view one: same sampler, decode and mesh nodes
  const single = hunyuanWorkflow('f.png', MV_CHECKPOINT, 'high', 703, 'p', 0);
  for (const id of ['1', '5', '6', '7', '8', '9']) assert.deepEqual(w[id], single[id]);
  assert.equal(w['6'].inputs.steps, 45);
  assert.equal(w['5'].inputs.resolution, 3072);
});

test('multi-view workflow: any subset with a front; front-only and missing front', () => {
  const two = hunyuanMultiViewWorkflow({ front: 'f.png', back: 'b.png' }, MV_CHECKPOINT, 'standard', 1, 'p');
  assert.deepEqual(Object.keys(two['4'].inputs).sort(), ['back', 'front']);
  assert.equal(two['22'], undefined);
  assert.equal(two['32'], undefined);
  assert.deepEqual(Object.keys(hunyuanMultiViewWorkflow({ front: 'f.png' }, MV_CHECKPOINT, 'draft', 1, 'p')['4'].inputs), ['front']);
  assert.throws(() => hunyuanMultiViewWorkflow({ back: 'b.png' }, MV_CHECKPOINT, 'high', 1, 'p'), /front view/);
});

test('prompts: left and right are different, mirror conventions are stated, back is generated first', () => {
  const left = codexPrompt('left');
  const right = codexPrompt('right');
  assert.match(left, /LEFT-SIDE/);
  assert.match(left, /toward the LEFT edge/);
  assert.match(right, /RIGHT-SIDE/);
  assert.match(right, /toward the RIGHT edge/);
  assert.match(left, /foreshortened/);
  assert.match(codexPrompt('back', { hasBack: true }), /BACK view/);
  assert.match(codexPrompt('left', { hasBack: true }), /image 2 is the BACK view/);
  assert.match(codexPrompt('left', { note: 'blue tabard' }), /blue tabard/);
  assert.throws(() => codexPrompt('front'), /No generation prompt/);
  assert.deepEqual(generationOrder(['right', 'left', 'back']), ['back', 'left', 'right']);
  assert.deepEqual(SLOTS.slice(0, 4), ['front', 'left', 'back', 'right']);
  assert.ok(GENERATED_SLOTS.every((s) => SLOTS.includes(s) && s !== 'front'));
  assert.equal(SLOT_META.left.azimuth, 90);
  assert.equal(SLOT_META.right.azimuth, 270);
});

test('MV-Adapter workflow and the memory-safe size', () => {
  const w = mvadapterWorkflow({ refName: 'r.png', azimuths: [0, 90, 180, 270], size: 640, steps: 30, cfg: 3, seed: 21, prefix: 'x' });
  assert.equal(w['2'].class_type, 'AstraMVAdapterViews');
  assert.equal(w['2'].inputs.azimuths, '0,90,180,270');
  assert.equal(w['2'].inputs.ckpt_name, 'sd_xl_base_1.0.safetensors');
  assert.equal(w['2'].inputs.adapter_name, 'mvadapter_i2mv_sdxl.safetensors');
  assert.equal(w['2'].inputs.offload, 'text+vae');
  assert.deepEqual(w['3'].inputs.images, ['2', 0]);
  assert.equal(safeSize(768, 4), 640);   // 768 px pages on a 12 GB card
  assert.equal(safeSize(640, 6), 512);   // six views need less
  assert.equal(safeSize(undefined, 4), 640);
  assert.equal(safeSize(100, 4), 512);
});

test('generate request schema', () => {
  assert.equal(generateSchema.parse({ provider: 'codex' }).overwrite, false);
  assert.throws(() => generateSchema.parse({ provider: 'dalle' }));
  assert.throws(() => generateSchema.parse({ provider: 'codex', slots: ['front'] }));
  assert.throws(() => generateSchema.parse({ provider: 'codex', extra: 1 }));
  assert.throws(() => generateSchema.parse({ provider: 'mvadapter', size: 4096 }));
});

// ------------------------------------------------------------------ routes
let root, server, base, calls;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

before(async () => {
  root = mkdtempSync(path.join(tmpdir(), 'af-views-'));
  calls = [];
  const runner = {
    ...real,
    async normalizeViews(dir) {
      const views = await real.loadViews(dir);
      const files = {};
      for (const slot of Object.keys(views.slots)) {
        await fs.mkdir(path.join(dir, 'norm'), { recursive: true });
        await fs.writeFile(path.join(dir, 'norm', `norm_${slot}.png`), PNG);
        await fs.writeFile(path.join(dir, 'norm', `thumb_${slot}.jpg`), PNG);
        await fs.writeFile(path.join(dir, 'norm', `cut_${slot}.png`), PNG);
        files[slot] = { norm: `norm/norm_${slot}.png`, cut: `norm/cut_${slot}.png`, thumb: `norm/thumb_${slot}.jpg` };
      }
      views.normalized = { files, stale: false, at: new Date().toISOString(), canvas: 1536 };
      return real.saveViews(dir, views);
    },
    async views_generate(o) {
      calls.push(o);
      o.onProgress?.('drawing');
      await sleep(250);
      o.signal?.throwIfAborted();
      const views = await real.loadViews(o.outDir);
      for (const slot of o.slots) {
        if (views.slots[slot] && !o.overwrite) continue;   // like the real one: filled slots are kept
        await fs.writeFile(real.rawPath(o.outDir, slot), PNG);
        views.slots[slot] = { source: o.provider, provider: o.provider, file: `raw/${slot}.png`, seconds: 1.5 };
      }
      views.normalized.stale = true;
      await real.saveViews(o.outDir, views);
      await runner.normalizeViews(o.outDir);
    },
  };
  const frontFile = path.join(root, 'front-asset.png');
  await fs.writeFile(frontFile, PNG);
  const service = createViewsService({
    root: path.join(root, 'sets'),
    runner,
    assetFile: async () => frontFile,
    getAsset: async (id) => { if (id === MISSING) throw Object.assign(new Error('File not found'), { status: 404 }); return { id, extension: '.png', name: 'front.png' }; },
  });
  const app = express();
  app.use(express.json());
  const asyncRoute = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
  registerViews(app, asyncRoute, { service, getConfig: () => ({ comfyUrl: 'http://127.0.0.1:1' }) });
  app.use((err, req, res, next) => res.status(err.status || 400).json({ error: err.message }));
  await new Promise((resolve) => { server = app.listen(0, '127.0.0.1', resolve); });
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await new Promise((r) => server.close(r));
  await fs.rm(root, { recursive: true, force: true });
});

const post = (url, body) => fetch(base + url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
const put = (url, bytes, name = 'left.png') => {
  const form = new FormData();
  form.append('file', new Blob([bytes]), name);
  return fetch(base + url, { method: 'PUT', body: form });
};

test('routes: a set is made from a concept image and reused for the same image', async () => {
  const res = await post('/api/views', { imageId: ID });
  assert.equal(res.status, 201);
  const set = await res.json();
  assert.equal(set.frontAssetId, ID);
  assert.equal(set.slots.front.filled, true);
  assert.equal(set.slots.left.filled, false);
  assert.deepEqual(set.order, SLOTS);
  assert.equal(set.canBuild, true);
  assert.equal(set.extraViews, 0);
  const again = await (await post('/api/views', { imageId: ID })).json();
  assert.equal(again.id, set.id);
  assert.equal((await post('/api/views', { imageId: MISSING })).status, 404);
  assert.equal((await post('/api/views', {})).status, 400);
});

test('routes: upload, preview, delete a view', async () => {
  const { id } = await (await post('/api/views', { imageId: uid(2) })).json();
  assert.equal((await put(`/api/views/${id}/left`, Buffer.from('not an image'))).status, 400);
  assert.equal((await put(`/api/views/${id}/up`, PNG)).status, 400);
  const res = await put(`/api/views/${id}/left`, PNG);
  assert.equal(res.status, 200);
  const set = await res.json();
  assert.equal(set.slots.left.filled, true);
  assert.equal(set.slots.left.source, 'manual');
  assert.equal(set.extraViews, 1);
  assert.match(set.slots.left.thumb, new RegExp(`/api/views/${id}/image/left\\?kind=thumb`));
  for (const kind of ['thumb', 'norm', 'raw']) {
    const img = await fetch(`${base}/api/views/${id}/image/left?kind=${kind}`);
    assert.equal(img.status, 200);
    assert.equal(Buffer.from(await img.arrayBuffer()).length, PNG.length);
  }
  assert.equal((await fetch(`${base}/api/views/${id}/image/back?kind=thumb`)).status, 404);
  const gone = await (await fetch(`${base}/api/views/${id}/left`, { method: 'DELETE' })).json();
  assert.equal(gone.slots.left.filled, false);
  assert.equal((await fetch(`${base}/api/views/${id}/front`, { method: 'DELETE' })).status, 400);
  assert.equal((await fetch(`${base}/api/views/nope`)).status, 400);
  assert.equal((await fetch(`${base}/api/views/22222222-2222-4222-8222-222222222222`)).status, 404);
});

test('routes: generation is a background task; a second one is refused; own views are not regenerated', async () => {
  const { id } = await (await post('/api/views', { imageId: uid(3) })).json();
  await put(`/api/views/${id}/back`, PNG, 'back.png');
  assert.equal((await post(`/api/views/${id}/generate`, { provider: 'dalle' })).status, 400);
  const start = await post(`/api/views/${id}/generate`, { provider: 'codex', slots: ['left', 'back', 'right'] });
  assert.equal(start.status, 202);
  assert.equal((await start.json()).task.status, 'running');
  assert.equal((await post(`/api/views/${id}/generate`, { provider: 'mvadapter' })).status, 409);
  assert.equal((await put(`/api/views/${id}/right`, PNG)).status, 409);
  let set;
  for (let i = 0; i < 40; i++) {
    set = await (await fetch(`${base}/api/views/${id}`)).json();
    if (set.task.status !== 'running') break;
    await sleep(100);
  }
  assert.equal(set.task.status, 'completed');
  const call = calls[calls.length - 1];
  assert.equal(call.provider, 'codex');
  assert.deepEqual(call.slots, ['left', 'back', 'right']);
  assert.equal(call.overwrite, false);                     // the supplied back view stays: generation skips filled slots
  assert.equal(set.slots.back.source, 'manual');
  assert.equal(set.slots.right.source, 'codex');
  assert.equal(set.slots.left.filled, true);
  assert.equal(set.extraViews, 3);
  assert.equal(set.canBuild, true);
});

test('routes: a running task can be cancelled', async () => {
  const { id } = await (await post('/api/views', { imageId: uid(4) })).json();
  await post(`/api/views/${id}/generate`, { provider: 'mvadapter', slots: ['left'] });
  const cancelled = await (await post(`/api/views/${id}/cancel`, {})).json();
  assert.ok(['running', 'cancelled'].includes(cancelled.task.status));
  let set;
  for (let i = 0; i < 40; i++) {
    set = await (await fetch(`${base}/api/views/${id}`)).json();
    if (set.task.status !== 'running') break;
    await sleep(100);
  }
  assert.equal(set.task.status, 'cancelled');
});

test('forJob: generator inputs of a set, front included', async () => {
  const { id } = await (await post('/api/views', { imageId: uid(5) })).json();
  await put(`/api/views/${id}/right`, PNG, 'r.png');
  const { dir, views } = { dir: path.join(root, 'sets', id), views: await real.loadViews(path.join(root, 'sets', id)) };
  const files = real.normFiles(dir, views, MV_SLOTS);
  assert.deepEqual(Object.keys(files).sort(), ['front', 'right']);
  assert.match(files.front, /norm_front\.png$/);
});
