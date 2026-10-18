// Forge HTTP routes under /api/forge (docs/forge/API.md).
//
// server/index.mjs registers them with one call after its security middleware, so every
// mutating request already passed the loopback Host/Origin check and the x-astra-client header.
import express from 'express';
import multer from 'multer';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { ForgeEngine, MAX_CLIP_FILES, forgeError, isFbxBuffer } from './forge-pipeline.mjs';
import { listPresets, listSteps } from './forge-steps.mjs';
import { ForgeReview } from './forge-review.mjs';

const MB = 1024 * 1024;
const FILE_FIELDS = ['files[]', 'files'];

function sendError(res, err) {
  if (res.headersSent) return;
  let status = Number(err?.status) || 500;
  let code = err?.code || (status >= 500 ? 'internal' : 'bad_request');
  let message = err?.message || 'Forge error';
  if (err?.name === 'ZodError' && Array.isArray(err.issues)) {
    status = 400;
    code = 'validation';
    message = err.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ');
  }
  if (err instanceof multer.MulterError) {
    status = err.code === 'LIMIT_FILE_SIZE' ? 413 : 400;
    code = 'upload_' + String(err.code).toLowerCase();
    message = {
      LIMIT_FILE_SIZE: 'File is too large.',
      LIMIT_FILE_COUNT: 'Too many files.',
      LIMIT_UNEXPECTED_FILE: 'Unexpected file field in the request.',
    }[err.code] || `Upload error: ${err.message}`;
  }
  if (err?.type === 'entity.parse.failed') {
    status = 400;
    code = 'bad_json';
    message = 'Request body is not valid JSON (parse error).';
  } else if (err?.type === 'entity.too.large') {
    status = 413;
    code = 'too_large';
    message = 'Request body is too large.';
  } else if (typeof err?.type === 'string' && err.type.startsWith('encoding.')) {
    status = 415;
    code = 'bad_encoding';
    message = 'Unsupported request body encoding.';
  }
  if (status >= 500) console.warn('[forge]', err?.stack || err);
  res.status(status).json({ error: message, code });
}

function parseJsonField(value, name) {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    throw forgeError(`Field "${name}" must be JSON.`, 400, 'bad_json');
  }
}

const safeName = (name) => String(name || 'file').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/^\.+/, '_').slice(-120);

/**
 * Register /api/forge routes.
 * @param {import('express').Express} app
 * @param {Function} [asyncRoute] unused (kept for symmetry with registerTripo); errors use {error, code}
 * @param {{engine?: ForgeEngine, getGenerationJob?: (id:string) => any, review?: object}} [options]
 *   `review`: {stateFile, rolesFile, decisionsFile} for the Review view (defaults: env ASTRA_FORGE_REVIEW_*)
 * @returns {ForgeEngine}
 */
export function registerForge(app, asyncRoute, options = {}) {
  const engine = options.engine || new ForgeEngine({ getGenerationJob: options.getGenerationJob, host: 'server' });
  const review = new ForgeReview(engine, options.review);
  const ready = engine.init();
  ready.catch((err) => console.warn('[forge] init failed:', err.message));
  const router = express.Router();
  const route = (fn) => async (req, res) => {
    try {
      await ready;
      await fn(req, res);
    } catch (err) {
      sendError(res, err);
    }
  };

  // Uploads are staged on disk (never kept whole in memory) and validated by signature.
  const stagingStorage = multer.diskStorage({
    destination: (req, file, cb) => {
      if (!req.forgeUploadDir) req.forgeUploadDir = path.join(engine.workDir, 'uploads', randomUUID());
      fs.mkdir(req.forgeUploadDir, { recursive: true }).then(() => cb(null, req.forgeUploadDir), cb);
    },
    filename: (req, file, cb) => {
      req.forgeUploadCount = (req.forgeUploadCount || 0) + 1;
      cb(null, `${String(req.forgeUploadCount).padStart(3, '0')}_${safeName(file.originalname)}`);
    },
  });
  const meshUpload = multer({ storage: stagingStorage, limits: { fileSize: 300 * MB, files: 1, fields: 20 } });
  const batchUpload = multer({
    storage: stagingStorage,
    limits: { fileSize: 300 * MB, files: 50, fields: 20 },
    fileFilter: (req, file, cb) => cb(null, FILE_FIELDS.includes(file.fieldname)),
  });
  // Clip uploads are staged next to mesh uploads (not inside the library); the clip_library
  // import copies them into animations/mixamo/ and the job removes the staging folder when it ends.
  // The original file name is kept: the clip id is derived from it.
  const clipsUpload = multer({
    storage: multer.diskStorage({
      destination: (req, file, cb) => {
        if (!req.forgeUploadDir) req.forgeUploadDir = path.join(engine.workDir, 'uploads', randomUUID());
        fs.mkdir(req.forgeUploadDir, { recursive: true }).then(() => cb(null, req.forgeUploadDir), cb);
      },
      filename: (req, file, cb) => cb(null, safeName(path.basename(file.originalname))),
    }),
    limits: { fileSize: 200 * MB, files: MAX_CLIP_FILES, fields: 10 },
    fileFilter: (req, file, cb) => {
      if (!FILE_FIELDS.includes(file.fieldname) || path.extname(file.originalname).toLowerCase() !== '.fbx') {
        (req.forgeSkipped ||= []).push(file.originalname);
        return cb(null, false);
      }
      cb(null, true);
    },
  });
  // Staged uploads are our own temporary copies (the asset keeps its own copy), so they are removed.
  const cleanupStaging = async (req) => {
    if (req.forgeUploadDir && req.forgeUploadDir.startsWith(path.join(engine.workDir, 'uploads') + path.sep)) {
      await fs.rm(req.forgeUploadDir, { recursive: true, force: true }).catch(() => {});
    }
  };
  const withUpload = (mw) => (req, res, next) => {
    if (!req.is('multipart/form-data')) return next();
    mw(req, res, async (err) => {
      if (!err) return next();
      await cleanupStaging(req);
      sendError(res, err);
    });
  };
  const stagedFile = (f) => ({ path: f.path, originalName: f.originalname, staged: true });

  // ---------------------------------------------------------------- state & registries
  router.get('/status', route(async (req, res) => res.json(await engine.status())));
  router.get('/steps', route(async (req, res) => res.json(listSteps())));
  router.get('/presets', route(async (req, res) => res.json(listPresets())));

  // ---------------------------------------------------------------- assets
  router.get('/assets', route(async (req, res) => res.json(await engine.listAssets())));

  router.post('/assets', withUpload(meshUpload.single('file')), route(async (req, res) => {
    let manifest;
    try {
      const body = req.body || {};
      const source = parseJsonField(body.source, 'source') || (req.file ? { type: 'upload' } : undefined);
      const profile = parseJsonField(body.profile, 'profile');
      const file = req.file ? stagedFile(req.file) : null;
      manifest = await engine.createAsset({ name: body.name, kind: body.kind, source, profile }, file);
    } finally {
      await cleanupStaging(req);
    }
    res.status(201).json(manifest);
  }));

  router.get('/assets/:id', route(async (req, res) => res.json(await engine.getAsset(req.params.id))));
  router.patch('/assets/:id', route(async (req, res) => res.json(await engine.patchAsset(req.params.id, req.body))));
  router.delete('/assets/:id', route(async (req, res) => res.json(await engine.trashAsset(req.params.id))));

  router.post('/assets/:id/run', route(async (req, res) => {
    const body = req.body || {};
    res.status(202).json(await engine.startRun(req.params.id, { steps: body.steps, preset: body.preset, from: body.from }));
  }));

  router.post('/assets/:id/export', route(async (req, res) => {
    const body = req.body || {};
    res.status(202).json(await engine.startExport(req.params.id, { profile: body.profile, params: body.params }));
  }));

  router.get('/assets/:id/files/*rest', route(async (req, res) => {
    const segments = [].concat(req.params.rest || []);
    const file = await engine.resolveAssetFile(req.params.id, segments);
    if (file.download || req.query.download === '1') res.attachment(file.name);
    res.setHeader('Cache-Control', 'no-store');
    await new Promise((resolve, reject) => res.sendFile(file.path, (err) => (err && !res.headersSent ? reject(err) : resolve())));
  }));

  // ---------------------------------------------------------------- review (Review view, docs/forge/REVIEW.md)
  const batchOf = (req) => (typeof req.query.batch === 'string' ? req.query.batch : typeof req.body?.batch === 'string' ? req.body.batch : undefined);
  router.get('/review', route(async (req, res) => res.json(await review.list({ batch: batchOf(req) }))));
  router.post('/review/export', route(async (req, res) => res.json(await review.exportDecisions({ batch: batchOf(req) }))));

  // ---------------------------------------------------------------- jobs
  router.get('/jobs', route(async (req, res) => {
    const limit = Number(req.query.limit) || 50;
    res.json(await engine.listJobs({ limit, assetId: typeof req.query.assetId === 'string' ? req.query.assetId : null }));
  }));
  router.get('/jobs/:jobId', route(async (req, res) => res.json(await engine.getJob(req.params.jobId))));
  router.post('/jobs/:jobId/cancel', route(async (req, res) => res.json(await engine.cancelJob(req.params.jobId))));
  router.get('/jobs/:jobId/steps/:index/log', route(async (req, res) => {
    const file = await engine.jobLog(req.params.jobId, req.params.index);
    res.type('text/plain; charset=utf-8');
    if (req.query.download === '1') res.attachment(path.basename(file));
    await new Promise((resolve, reject) => res.sendFile(file, (err) => (err && !res.headersSent ? reject(err) : resolve())));
  }));

  // ---------------------------------------------------------------- clip library
  router.get('/clips', route(async (req, res) => res.json(await engine.clipIndex())));

  router.post('/clips', withUpload(clipsUpload.any()), route(async (req, res) => {
    let job;
    let accepted = [];
    const rejected = [];
    const skipped = req.forgeSkipped || [];
    try {
      const files = req.files || [];
      if (!files.length && !skipped.length) throw forgeError('Select FBX files (field files[]).', 400, 'no_files');
      for (const f of files) {
        const fd = await fs.open(f.path, 'r');
        const head = Buffer.alloc(2048);
        try { await fd.read(head, 0, 2048, 0); } finally { await fd.close(); }
        if (isFbxBuffer(head)) accepted.push(f);
        else rejected.push(f);
      }
      if (!accepted.length) throw forgeError('None of the files is an FBX (extension .fbx and signature "Kaydara FBX Binary" or "; FBX").', 400, 'bad_signature');
      for (const f of rejected) await fs.rm(f.path, { force: true }).catch(() => {});
      job = await engine.importClips({ files: accepted.map((f) => f.path), stagedDir: req.forgeUploadDir });
    } catch (err) {
      await cleanupStaging(req); // the import job owns the staging folder only once it exists
      accepted = [];
      throw err;
    }
    res.status(202).json({ ...job, files: accepted.map((f) => f.originalname), rejected: [...skipped, ...rejected.map((f) => f.originalname)] });
  }));

  router.get('/clips/:clipId/poses', route(async (req, res) => {
    const file = await engine.clipPosesPath(req.params.clipId);
    res.type('application/json');
    await new Promise((resolve, reject) => res.sendFile(file, (err) => (err && !res.headersSent ? reject(err) : resolve())));
  }));
  router.delete('/clips/:clipId', route(async (req, res) => res.json(await engine.removeClip(req.params.clipId))));

  // ---------------------------------------------------------------- batches
  router.post('/batch', withUpload(batchUpload.any()), route(async (req, res) => {
    let started;
    try {
      const body = req.body || {};
      const items = parseJsonField(body.items, 'items');
      const files = (req.files || []).map(stagedFile);
      started = await engine.startBatch({ items }, files);
    } finally {
      await cleanupStaging(req);
    }
    res.status(202).json(started);
  }));
  router.get('/batch/:batchId', route(async (req, res) => res.json(await engine.getBatch(req.params.batchId))));

  router.use((req, res) => res.status(404).json({ error: 'No such Forge route.', code: 'not_found' }));
  router.use((err, req, res, next) => sendError(res, err)); // eslint-disable-line no-unused-vars

  app.use('/api/forge', router);
  // Errors raised before the router — the app-wide express.json body parser (malformed JSON,
  // too large) — answer in Russian with a code for /api/forge instead of the generic handler.
  app.use('/api/forge', (err, req, res, next) => { // eslint-disable-line no-unused-vars
    if (res.headersSent) return next(err);
    sendError(res, err);
  });
  return engine;
}
