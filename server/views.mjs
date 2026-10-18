// "Views": view sets for 3D from several views (docs/forge/views.md).
//
// A view set is a folder data/views/<id>/ made by scripts/views/views_generate.mjs (raw/, norm/, views.json).
// This module wraps it for the Studio:
//   POST   /api/views                        {imageId}            -> set with the front view taken from a concept image
//   GET    /api/views/status                                      -> which providers work right now
//   GET    /api/views/:id                                         -> slots, thumbnails, running task
//   GET    /api/views/:id/image/:slot?kind=thumb|norm|raw         -> the picture (norm = generator input)
//   PUT    /api/views/:id/:slot        multipart file             -> own view for left / back / right / front
//   DELETE /api/views/:id/:slot                                   -> remove a view
//   POST   /api/views/:id/generate     {provider, slots?, ...}    -> fill the missing views (codex | mvadapter)
//   POST   /api/views/:id/cancel
// "3D from views" is an ordinary generation job: POST /api/jobs with {imageId, viewSetId} (server/jobs.mjs).
import multer from 'multer';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { promises as fs, existsSync } from 'node:fs';
import { z } from 'zod';
import { data, isId, assetInfo } from './storage.mjs';
import { SLOTS, GENERATED_SLOTS } from '../scripts/views/views_prompts.mjs';
import * as defaultViews from '../scripts/views/views_generate.mjs';
import { codexStatus } from '../scripts/views/codex_image.mjs';
import { mvadapterStatus } from '../scripts/views/views_mvadapter.mjs';
import { MV_SLOTS } from './schema.mjs';

export const VIEWS_ROOT = path.join(data, 'views');
const slotSchema = z.enum(SLOTS);
export const generateSchema = z.object({
  provider: z.enum(['codex', 'mvadapter']),
  slots: z.array(z.enum(GENERATED_SLOTS)).min(1).max(5).optional(),
  overwrite: z.boolean().default(false),
  seed: z.number().int().min(0).max(2147483647).optional(),
  size: z.number().int().min(512).max(768).optional(),
  steps: z.number().int().min(8).max(60).optional(),
  note: z.string().max(500).optional(),
}).strict();
const createSchema = z.object({ imageId: z.string().uuid() }).strict();
const httpError = (message, status = 400) => Object.assign(new Error(message), { status });

function imageKind(buffer) {
  if (buffer.length > 8 && buffer.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return '.png';
  if (buffer.length > 3 && buffer[0] === 255 && buffer[1] === 216) return '.jpg';
  if (buffer.length > 12 && buffer.subarray(0, 4).toString() === 'RIFF' && buffer.subarray(8, 12).toString() === 'WEBP') return '.webp';
  return null;
}

export function createViewsService({ root = VIEWS_ROOT, runner = defaultViews, getConfig = () => ({ comfyUrl: 'http://127.0.0.1:8190' }), assetFile = defaultAssetFile, getAsset = assetInfo } = {}) {
  const tasks = new Map();
  const locks = new Map();
  const dirOf = (id) => {
    if (!isId(id)) throw httpError('Invalid view set number.', 400);
    return path.join(root, id);
  };
  const exclusive = (id, fn) => {
    const next = (locks.get(id) || Promise.resolve()).catch(() => {}).then(fn);
    locks.set(id, next);
    return next;
  };

  async function readViews(id) {
    const dir = dirOf(id);
    const views = await runner.loadViews(dir);
    if (!views) throw httpError('View set not found.', 404);
    return { dir, views };
  }

  function publicSet(id, views) {
    const rev = encodeURIComponent(views.updatedAt || '');
    const slots = {};
    for (const slot of SLOTS) {
      const s = views.slots[slot];
      if (!s) { slots[slot] = { filled: false }; continue; }
      const ready = !!views.normalized?.files?.[slot];
      slots[slot] = {
        filled: true, source: s.source, provider: s.provider || null, seconds: s.seconds ?? null, name: s.name || null,
        size: s.size ?? null, steps: s.steps ?? null, seed: s.seed ?? null, generatedAt: s.generatedAt || s.addedAt || null,
        thumb: `/api/views/${id}/image/${slot}?kind=${ready ? 'thumb' : 'raw'}&v=${rev}`,
        full: `/api/views/${id}/image/${slot}?kind=${ready ? 'norm' : 'raw'}&v=${rev}`,
      };
    }
    const task = tasks.get(id) || null;
    const front = !!views.slots.front;
    return {
      id, createdAt: views.createdAt, updatedAt: views.updatedAt, frontAssetId: views.frontAssetId || null,
      order: SLOTS, shapeSlots: MV_SLOTS, slots,
      normalized: views.normalized ? { at: views.normalized.at, stale: !!views.normalized.stale, canvas: views.normalized.canvas } : null,
      lastRun: views.lastRun || null,
      task: task ? { status: task.status, provider: task.provider, slots: task.slots, message: task.message, startedAt: task.startedAt, finishedAt: task.finishedAt || null, error: task.error || null } : null,
      extraViews: MV_SLOTS.filter((s) => s !== 'front' && views.slots[s]).length,
      canBuild: front && !!views.normalized && !views.normalized.stale && task?.status !== 'running',
    };
  }

  async function getSet(id) {
    const { views } = await readViews(id);
    return publicSet(id, views);
  }

  async function findByImage(imageId) {
    if (!existsSync(root)) return null;
    for (const name of await fs.readdir(root)) {
      if (!isId(name)) continue;
      const v = await runner.loadViews(path.join(root, name)).catch(() => null);
      if (v?.frontAssetId === imageId) return name;
    }
    return null;
  }

  async function createSet(imageId) {
    const existing = await findByImage(imageId);
    if (existing) return getSet(existing);
    const info = await getAsset(imageId);
    if (!['.png', '.jpg', '.jpeg', '.webp'].includes(info.extension)) throw httpError('Views need a concept in image form.');
    const id = randomUUID();
    const dir = dirOf(id);
    await fs.mkdir(dir, { recursive: true });
    await runner.putManualView(dir, 'front', await assetFile(info), { name: info.name });
    const views = await runner.loadViews(dir);
    views.frontAssetId = imageId;
    await runner.saveViews(dir, views);
    await runner.normalizeViews(dir);
    return getSet(id);
  }

  function assertIdle(id) {
    if (tasks.get(id)?.status === 'running') throw httpError('Wait for view generation to finish.', 409);
  }

  async function putView(id, slot, buffer, name) {
    slotSchema.parse(slot);
    const ext = imageKind(buffer);
    if (!ext) throw httpError('Supported formats: PNG, JPG and WEBP.');
    assertIdle(id);
    return exclusive(id, async () => {
      const { dir } = await readViews(id);
      await fs.mkdir(path.join(dir, 'work'), { recursive: true });
      const tmp = path.join(dir, 'work', 'upload_' + randomUUID() + ext);
      await fs.writeFile(tmp, buffer);
      try { await runner.putManualView(dir, slot, tmp, { name }); } finally { await fs.rm(tmp, { force: true }); }
      await runner.normalizeViews(dir);
      return getSet(id);
    });
  }

  async function deleteView(id, slot) {
    slotSchema.parse(slot);
    assertIdle(id);
    return exclusive(id, async () => {
      const { dir } = await readViews(id);
      await runner.removeView(dir, slot);
      await runner.normalizeViews(dir);
      return getSet(id);
    });
  }

  async function startGenerate(id, input) {
    const opts = generateSchema.parse(input);
    const { dir, views } = await readViews(id);
    if (!views.slots.front) throw httpError('A front view is needed first.');
    for (const t of tasks.values()) if (t.status === 'running') throw httpError('View generation is already running. Wait for it to finish or cancel it.', 409);
    const slots = opts.slots || ['left', 'back', 'right'];
    const task = { status: 'running', provider: opts.provider, slots, message: 'Starting', startedAt: new Date().toISOString(), controller: new AbortController() };
    tasks.set(id, task);
    void exclusive(id, async () => {
      try {
        await runner.views_generate({
          outDir: dir, provider: opts.provider, slots, overwrite: opts.overwrite, seed: opts.seed, size: opts.size, steps: opts.steps, note: opts.note,
          comfyUrl: getConfig().comfyUrl, signal: task.controller.signal,
          onProgress: (text) => { task.message = text; },
        });
        task.status = 'completed';
        task.message = 'Done';
      } catch (e) {
        task.status = e.name === 'AbortError' ? 'cancelled' : 'failed';
        task.message = e.name === 'AbortError' ? 'Cancelled' : e.message;
        task.error = task.status === 'failed' ? e.message : null;
        // a partly finished run (some views done) still left the folder consistent: re-cut what exists
        await runner.normalizeViews(dir).catch(() => {});
      } finally {
        task.finishedAt = new Date().toISOString();
        task.controller = null;
      }
    });
    return getSet(id);
  }

  function cancel(id) {
    const t = tasks.get(id);
    if (t?.status === 'running') t.controller?.abort();
    return getSet(id);
  }

  /** For generation jobs: the generator input images of a set (normalised if needed). */
  async function forJob(id, { signal } = {}) {
    let { dir, views } = await readViews(id);
    if (tasks.get(id)?.status === 'running') throw httpError('The views are still being generated. Wait for them to finish.', 409);
    if (!views.normalized || views.normalized.stale) views = await exclusive(id, () => runner.normalizeViews(dir, { signal }));
    const files = runner.normFiles(dir, views, MV_SLOTS);
    if (!files.front) throw httpError('The set has no front view.');
    const cutFiles = {};
    for (const slot of MV_SLOTS) {
      const cut = views.normalized?.files?.[slot]?.cut;
      if (cut && existsSync(path.join(dir, cut))) cutFiles[slot] = path.join(dir, cut);
    }
    return { dir, views, files, cutFiles, frontAssetId: views.frontAssetId || null, rev: views.normalized?.at || views.updatedAt };
  }

  async function imageFile(id, slot, kind) {
    slotSchema.parse(slot);
    const { dir, views } = await readViews(id);
    const f = views.normalized?.files?.[slot];
    let file;
    if (kind === 'raw' || !f) file = path.join(dir, 'raw', slot + '.png');
    else file = path.join(dir, kind === 'norm' ? f.norm : f.thumb);
    if (!existsSync(file)) throw httpError('View not found.', 404);
    return file;
  }

  return { getSet, createSet, putView, deleteView, startGenerate, cancel, forJob, imageFile, findByImage, tasks, root };
}

async function defaultAssetFile(info) {
  return path.join(data, 'assets', info.id + info.extension);
}

let statusCache = { at: 0, value: null };
let shared = null;
/** The service the generation jobs use (set by registerViews; created on demand in scripts and tests). */
export function getViewsService() { return shared || (shared = createViewsService()); }

export function registerViews(app, asyncRoute, options = {}) {
  const service = options.service || createViewsService(options);
  shared = service;
  const getConfig = options.getConfig || (() => ({ comfyUrl: 'http://127.0.0.1:8190' }));
  const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 40 * 1024 * 1024, files: 1 } });
  app.get('/api/views/status', asyncRoute(async (req, res) => {
    if (!statusCache.value || Date.now() - statusCache.at > 20000) {
      const [codex, mvadapter] = await Promise.all([codexStatus(), mvadapterStatus(getConfig().comfyUrl)]);
      statusCache = { at: Date.now(), value: { codex, mvadapter } };
    }
    res.json({ ...statusCache.value, slots: SLOTS, shapeSlots: MV_SLOTS });
  }));
  app.post('/api/views', asyncRoute(async (req, res) => {
    const input = createSchema.parse(req.body);
    res.status(201).json(await service.createSet(input.imageId));
  }));
  app.get('/api/views/:id', asyncRoute(async (req, res) => res.json(await service.getSet(req.params.id))));
  app.get('/api/views/:id/image/:slot', asyncRoute(async (req, res) => {
    const kind = ['thumb', 'norm', 'raw'].includes(req.query.kind) ? req.query.kind : 'thumb';
    res.setHeader('Cache-Control', 'no-cache');
    res.sendFile(await service.imageFile(req.params.id, req.params.slot, kind));
  }));
  app.put('/api/views/:id/:slot', upload.single('file'), asyncRoute(async (req, res) => {
    if (!req.file) throw httpError('No file selected.');
    res.json(await service.putView(req.params.id, req.params.slot, req.file.buffer, req.file.originalname));
  }));
  app.delete('/api/views/:id/:slot', asyncRoute(async (req, res) => res.json(await service.deleteView(req.params.id, req.params.slot))));
  app.post('/api/views/:id/generate', asyncRoute(async (req, res) => res.status(202).json(await service.startGenerate(req.params.id, req.body))));
  app.post('/api/views/:id/cancel', asyncRoute(async (req, res) => res.json(await service.cancel(req.params.id))));
  return service;
}
