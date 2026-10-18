#!/usr/bin/env node
// views_3d — 3D from front / left / back / right (any subset, at least front) -> GLB, with one of two engines:
//   pixal3d  Pixal3D (shape only, separate ComfyUI-Pixal3D on 8191, ~4 min, ~10 GB VRAM): more accurate, crisper. docs/forge/pixal3d.md
//   hunyuan  Hunyuan3D-2mv in the studio ComfyUI on 8190 (~3 min, faster, softer)
//   auto     (default) pixal3d when it is installed, otherwise hunyuan
// Takes a views folder made by views_generate (norm/ images) and runs the same graph the Studio runs for a "3D from views" job
// (server/schema.mjs: pixal3dWorkflow / hunyuanMultiViewWorkflow). A Pixal3D GLB comes out normalised like the Hunyuan seeds
// (centred, 2 units tall, facing -Y after Blender's glTF import).
//
//   node scripts/views/views_3d.mjs --views <dir> --out mesh.glb [--engine pixal3d|hunyuan|auto] [--slots front,left,back,right]
//        [--seed 703]                                   (Pixal3D default seed 1000)
//        [--quality draft|standard|high] [--level 0] [--swap-sides] [--comfy http://127.0.0.1:8190]     (Hunyuan only)
//        [--faces 700000]                               (Pixal3D only: target face count)
//
// --swap-sides feeds the left image as "right" and vice versa (to check the camera convention of the model).
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { MV_CHECKPOINT, MV_SLOTS, hunyuanMultiViewWorkflow } from '../../server/schema.mjs';
import { withEngineGpu } from '../../server/gpu-handover.mjs';
import { resolveEngine } from '../../server/pixal3d.mjs';
import { generatePixal3d, cutFilesOf } from '../../server/pixal3d-run.mjs';
import { loadViews, normFiles } from './views_generate.mjs';
import { DEFAULT_COMFY_URL, requireNodes, uploadImage, runWorkflow, downloadOutput, comfyQueue } from './comfy_client.mjs';

async function pixal3d3d(o, dir, views, wanted) {
  const images = await cutFilesOf(dir, views, wanted);
  if (!images.front) throw new Error('The front view is required for geometry from views (norm/cut_front.png is missing: run views_generate).');
  const seed = Number.isFinite(Number(o.seed)) ? Number(o.seed) : 1000;
  const res = await generatePixal3d({
    images, seed, targetFaces: o.faces, signal: o.signal, tag: 'cli-' + Date.now().toString(36),
    onProgress: ({ percent, message }) => o.onProgress?.(`Pixal3D · ${message} (${percent}%)`),
    onWait: (text) => o.onProgress?.(text),
  });
  const out = path.resolve(o.out);
  await fs.mkdir(path.dirname(out), { recursive: true });
  await fs.writeFile(out, res.buffer);
  return { engine: 'pixal3d', out, seconds: res.seconds, slots: res.slots, seed, bytes: res.buffer.length, faces: res.targetFaces, vram: res.vram, normalization: res.normalization };
}

export async function views_3d(o) {
  const dir = path.resolve(o.viewsDir);
  const views = await loadViews(dir);
  if (!views?.normalized) throw new Error('The folder has no prepared views (run views_generate).');
  const wanted = (o.slots?.length ? o.slots : MV_SLOTS).filter((s) => MV_SLOTS.includes(s));
  const engine = resolveEngine(o.engine || 'auto');
  o.onProgress?.('3D engine: ' + (engine === 'pixal3d' ? 'Pixal3D' : 'Hunyuan3D-2mv'));
  if (engine === 'pixal3d') return pixal3d3d(o, dir, views, wanted);

  const comfyUrl = o.comfyUrl || DEFAULT_COMFY_URL;
  const files = normFiles(dir, views, wanted);
  if (!files.front) throw new Error('The front view is required for geometry from views.');
  const quality = o.quality || 'high';
  const seed = Number.isFinite(Number(o.seed)) ? Number(o.seed) : 703;
  const info = await requireNodes(comfyUrl, ['Hunyuan3Dv2ConditioningMultiView', 'SaveGLB', 'AstraMarchingCubes']);
  const names = info.ImageOnlyCheckpointLoader?.input?.required?.ckpt_name?.[0] || [];
  if (!names.includes(MV_CHECKPOINT)) throw new Error('Model not installed: ' + MV_CHECKPOINT + ' (Hunyuan3D-2mv).');
  const tag = 'views-' + Date.now();
  const uploaded = {};
  for (const [slot, file] of Object.entries(files)) uploaded[slot] = await uploadImage(comfyUrl, file, `astra-${tag}-${slot}.png`);
  if (o.swapSides) [uploaded.left, uploaded.right] = [uploaded.right, uploaded.left];
  const workflow = hunyuanMultiViewWorkflow(uploaded, MV_CHECKPOINT, quality, seed, 'astra_views/' + tag, o.meshLevel ?? 0);
  const q = await comfyQueue(comfyUrl);
  if (q && (q.queue_running?.length || q.queue_pending?.length)) o.onProgress?.('The engine queue already has jobs, waiting');
  const started = Date.now();
  // the GPU mutex plus the hand-over: a loaded Pixal3D engine is unloaded first (server/gpu-handover.mjs)
  const run = await withEngineGpu('hunyuan', () => runWorkflow(comfyUrl, workflow, { signal: o.signal, onProgress: (t) => o.onProgress?.('Hunyuan3D-2mv · ' + t) }),
    { signal: o.signal, failOpen: true, timeoutMs: 60 * 60 * 1000, urls: { hunyuan: comfyUrl }, onWait: () => o.onProgress?.('Waiting for the GPU to become free'), onStep: (t) => o.onProgress?.(t) });
  const item = Object.values(run.outputs).flatMap((x) => x['3d'] || [])[0];
  if (!item) throw new Error('The engine returned no GLB.');
  const out = path.resolve(o.out);
  await downloadOutput(comfyUrl, item, out);
  const seconds = Math.round((Date.now() - started) / 100) / 10;
  return { engine: 'hunyuan', out, seconds, slots: Object.keys(files), quality, seed, bytes: (await fs.stat(out)).size, swapSides: !!o.swapSides };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const a = { };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--views') a.viewsDir = argv[++i];
    else if (k === '--out') a.out = argv[++i];
    else if (k === '--engine') a.engine = argv[++i];
    else if (k === '--slots') a.slots = argv[++i].split(',').map((s) => s.trim()).filter(Boolean);
    else if (k === '--quality') a.quality = argv[++i];
    else if (k === '--seed') a.seed = Number(argv[++i]);
    else if (k === '--level') a.meshLevel = Number(argv[++i]);
    else if (k === '--faces') a.faces = Number(argv[++i]);
    else if (k === '--comfy') a.comfyUrl = argv[++i];
    else if (k === '--swap-sides') a.swapSides = true;
  }
  if (!a.viewsDir || !a.out) {
    console.log('node scripts/views/views_3d.mjs --views <dir> --out mesh.glb [--engine pixal3d|hunyuan|auto] [--slots front,left,back,right]\n'
      + '   [--seed 703|1000] [--quality draft|standard|high] [--level 0] [--swap-sides]   (Hunyuan)   [--faces 700000]   (Pixal3D)');
  } else {
    try {
      console.log(JSON.stringify(await views_3d({ ...a, onProgress: (t) => console.error('·', t) }), null, 2));
    } catch (e) {
      console.error('Error:', e.message);
      process.exit(1);
    }
  }
}
