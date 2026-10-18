// One-off check server for the VFX tab: the real registerVfxTools plus stubs, no ComfyUI and no GPU.
// Run: node check_server.mjs  ->  http://127.0.0.1:3299 (studio: ASTRA_APP, port: CHECK_PORT). Stop: taskkill /PID <pid> /F
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { writeFileSync } from 'node:fs';
import path from 'node:path';

// by default, the studio that contains this skill (APP/ai/skills/astraforge-vfx/scripts)
const APP = process.env.ASTRA_APP || fileURLToPath(new URL('../../../../', import.meta.url));
const require = createRequire(path.join(APP, 'package.json'));
const express = require('express');
const multer = require('multer');
const { registerVfxTools } = await import(pathToFileURL(path.join(APP, 'server', 'vfx-tools.mjs')).href);
const { data, assetInfo, saveAsset } = await import(pathToFileURL(path.join(APP, 'server', 'storage.mjs')).href);

const app = express();
app.disable('x-powered-by');
app.use(express.json({ limit: '90mb' }));
const asyncRoute = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// stubs for what App.jsx requests on load
app.get('/api/status', (req, res) => res.json({ pixal3d: { installed: false, running: false, missing: [] }, ollama: { online: false, models: [] }, comfy: { online: false, ready: false, checkpoints: [] }, gpu: null, ram: null, modelDownload: false }));
app.get('/api/jobs', (req, res) => res.json([]));
app.get('/api/config', (req, res) => res.json({ config: { provider: 'ollama' }, keys: {} }));
app.get('/api/health', (req, res) => res.json({ app: 'AstraForge-vfx-check' }));

// assets as in index.mjs: file upload and serving by id (needed for "Upload file" and "Apply to selected object")
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 100 * 1024 * 1024, files: 1 } });
app.post('/api/assets', upload.single('file'), asyncRoute(async (req, res) => {
  const f = req.file;
  if (!f) throw new Error('No file selected.');
  const ext = path.extname(f.originalname).toLowerCase();
  if (!['.png', '.jpg', '.jpeg', '.webp'].includes(ext)) throw new Error('The check server accepts only PNG, JPG, WEBP.');
  res.json(await saveAsset(f.buffer, ext, f.originalname));
}));
app.get('/api/assets/:id', asyncRoute(async (req, res) => { const info = await assetInfo(req.params.id); res.sendFile(path.join(data, 'assets', info.id + info.extension)); }));

registerVfxTools(app, asyncRoute);

app.use((err, req, res, next) => { if (res.headersSent) return next(err); res.status(err.status || 400).json({ error: err.message || 'Application error' }); });
app.use(express.static(path.join(APP, 'dist')));
app.get('/{*splat}', (req, res) => res.sendFile(path.join(APP, 'dist', 'index.html')));

const server = app.listen(Number(process.env.CHECK_PORT) || 3299, '127.0.0.1', () => {
  console.log('vfx-check: http://127.0.0.1:3299 pid ' + process.pid);
  writeFileSync(path.join(process.env.TEMP || '.', 'astraforge-check.pid'), String(process.pid));
});
process.on('SIGINT', () => server.close(() => process.exit(0)));
