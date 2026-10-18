// "VFX" tab: catalog of texture generators for visual effects (server/vfx-tools-catalog.json).
//
//   GET  /api/vfx-tools                        -> {root, rootExists, kinds, tools}; ?fresh=1 resets the installed-program search cache (30 s)
//   POST /api/vfx-tools/:id/launch             launch a program (desktop / installed installer) or open a template file in Material Maker
//   POST /api/vfx-tools/:id/install            run the installer (kind installer only)
//   POST /api/vfx-tools/:id/folder             open the program folder in the file explorer
//   POST /api/vfx-tools/folder   {where?}      open the tools root (where: "textures" - the folder of ready textures)
//   GET  /api/vfx-tools/textures               -> ready PNG/JPG/WEBP files from <root>/textures, newest first
//   GET  /api/vfx-tools/textures/:name         -> the file itself
//   POST /api/vfx-tools/textures/:name/import  -> a copy into the studio assets (same response as POST /api/assets)
//   GET  http://127.0.0.1:<webPort>/vfx-web/:id/*   static files of the web generators of kind web-local (work offline); a separate server, not the studio origin
//
// Web generators are third-party code, so they live on their own port (ASTRA_VFX_WEB_PORT, default 3211; options.webPort, 0 = random) and start on the first GET /api/vfx-tools.
// If the port is busy the studio does not crash: webUrl = null, the reason is in webError. The "Refresh" button (?fresh=1) tries to start the server again.
// Security: ids come from the catalog only, paths from the request are not accepted; every catalog path goes through resolveInside().
import express from 'express';
import path from 'node:path';
import { promises as fs, readFileSync } from 'node:fs';
import { spawn as nodeSpawn } from 'node:child_process';
import { createServer } from 'node:http';
import { saveAsset as defaultSaveAsset } from './storage.mjs';

export const DEFAULT_ROOT = 'C:\\AstraForge\\tools\\vfx';
export const DEFAULT_WEB_PORT = 3211;
export const CACHE_MS = 30000;
const TEXTURE_LIMIT = 200;
const TEXTURE_MAX_BYTES = 100 * 1024 * 1024;
const INSTALL_HINT = 'Run scripts\\vfx\\Install-VfxTools.ps1.';

const fail = (message, status = 400) => Object.assign(new Error(message), { status });
const notFound = (res) => res.status(404).type('text/plain; charset=utf-8').send('Not found');

/** Tool catalog from the JSON file next to the module. */
export function loadCatalog(file = new URL('./vfx-tools-catalog.json', import.meta.url)) {
  return JSON.parse(readFileSync(file, 'utf8'));
}

/** Path rel inside root. Throws if the path is absolute or escapes root (via "..", a drive letter, a null byte). */
export function resolveInside(root, rel) {
  if (typeof rel !== 'string' || !rel || rel.includes('\0')) throw fail('Invalid tool path.');
  if (path.isAbsolute(rel)) throw fail('The tool path must be relative.');
  const base = path.resolve(root);
  const full = path.resolve(base, rel);
  const back = path.relative(base, full);
  if (path.isAbsolute(back) || back === '..' || back.startsWith('..' + path.sep)) throw fail('The path escapes the tools folder.');
  return full;
}

/** List of catalog problems (empty means the catalog is valid). Used by both the test and developers. */
export function validateCatalog(catalog) {
  const problems = [];
  const kinds = catalog?.kinds && typeof catalog.kinds === 'object' ? catalog.kinds : {};
  const tools = Array.isArray(catalog?.tools) ? catalog.tools : [];
  if (!tools.length) problems.push('the catalog has no tools');
  const relOk = (p) => typeof p === 'string' && !!p && !path.isAbsolute(p) && !/^[a-z]:/i.test(p) && !p.includes('\0') && !p.split(/[\\/]/).includes('..');
  const ids = new Set();
  for (const t of tools) {
    const who = `«${t?.id}»`;
    if (!/^[a-z0-9][a-z0-9-]*$/.test(t?.id || '')) problems.push(`${who}: id must consist of a-z, 0-9 and "-"`);
    if (ids.has(t?.id)) problems.push(`${who}: duplicate id`);
    ids.add(t?.id);
    if (!t?.name) problems.push(`${who}: no name`);
    if (!Object.hasOwn(kinds, t?.kind)) problems.push(`${who}: kind "${t?.kind}" is not described in kinds`);
    if (!/^https?:\/\//.test(t?.url || '')) problems.push(`${who}: no url link`);
    if (!Array.isArray(t?.tags)) problems.push(`${who}: tags must be an array`);
    if (!t?.ru) problems.push(`${who}: no ru description`);
    const l = t?.local;
    const need = { desktop: ['exe'], installer: ['installer'], 'web-local': ['web'], file: ['file', 'openWith'] }[t?.kind] || [];
    for (const key of need) if (!l?.[key]) problems.push(`${who}: local.${key} is required for kind ${t.kind}`);
    if (l) for (const key of ['exe', 'installer', 'web', 'file']) if (l[key] != null && !relOk(l[key])) problems.push(`${who}: local.${key} must be a relative path without ".."`);
    if (l?.extras) for (const x of l.extras) if (!relOk(x)) problems.push(`${who}: local.extras contains an invalid path`);
    if (t?.kind === 'installer' && !(Array.isArray(l?.find?.hints) && l.find.hints.length && /^[^\\/]+\.exe$/i.test(l.find.exe || ''))) problems.push(`${who}: local.find must contain hints and an exe name`);
  }
  for (const t of tools) if (t?.kind === 'file') {
    const opener = tools.find((x) => x.id === t.local?.openWith);
    if (!opener || !['desktop', 'installer'].includes(opener.kind)) problems.push(`"${t.id}": openWith does not point to a catalog program`);
  }
  return problems;
}

/** Roots where installed programs are searched for. */
export function defaultSearchRoots(env = process.env) {
  const roots = [env.ProgramFiles, env['ProgramFiles(x86)'], env.LOCALAPPDATA && path.join(env.LOCALAPPDATA, 'Programs')];
  return [...new Set(roots.filter(Boolean))];
}

/** Valid file name of a ready texture: basename only, PNG/JPG/WEBP only. */
const TEXTURE_NAME = /^(?![. ])[\p{L}\p{N} _.,()+\-\[\]'~#@!&]{1,150}\.(png|jpe?g|webp)$/iu;
export const isTextureName = (name) => typeof name === 'string' && TEXTURE_NAME.test(name) && path.basename(name) === name;

const globRegExp = (pattern) => new RegExp('^' + pattern.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$', 'i');
const isFile = (file) => fs.stat(file).then((s) => s.isFile(), () => false);
const isDir = (dir) => fs.stat(dir).then((s) => s.isDirectory(), () => false);
const listDir = (dir) => fs.readdir(dir, { withFileTypes: true }).catch(() => []);

/** The first file whose name passes test, in dir and up to maxDepth levels below: level by level, alphabetically within a level. */
export async function findFile(dir, test, maxDepth) {
  let level = [dir];
  for (let depth = 0; depth <= maxDepth && level.length; depth++) {
    const next = [];
    for (const d of level) {
      const entries = (await listDir(d)).sort((a, b) => a.name.localeCompare(b.name));
      const hit = entries.find((e) => e.isFile() && test(e.name));
      if (hit) return path.join(d, hit.name);
      for (const e of entries) if (e.isDirectory()) next.push(path.join(d, e.name));
    }
    level = next;
  }
  return null;
}

/** Program exe by the catalog path. A path with "*" in the name: the first matching .exe in the folder (up to 2 levels), excluding UnityCrashHandler*. */
export async function findExe(root, rel) {
  const full = resolveInside(root, rel);
  const name = path.basename(full);
  if (!name.includes('*')) return (await isFile(full)) ? full : null;
  const match = globRegExp(name);
  return findFile(path.dirname(full), (n) => /\.exe$/i.test(n) && match.test(n) && !/^UnityCrashHandler/i.test(n), 2);
}

/** Installed program: child folders of the roots whose name contains the hint and, inside (up to 3 levels), an exe. */
export async function findInstalled(find, searchRoots) {
  const hints = (find?.hints || []).map((h) => h.toLowerCase());
  const wanted = String(find?.exe || '').toLowerCase();
  if (!hints.length || !wanted) return null;
  for (const base of searchRoots) {
    for (const e of await listDir(base)) {
      if (!e.isDirectory() || !hints.some((h) => e.name.toLowerCase().includes(h))) continue;
      const hit = await findFile(path.join(base, e.name), (n) => n.toLowerCase() === wanted, 3);
      if (hit) return hit;
    }
  }
  return null;
}

/** The file content matches the extension (same checks as POST /api/assets). */
function formatMatches(b, ext) {
  if (ext === '.png') return b.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  if (ext === '.jpg' || ext === '.jpeg') return b[0] === 255 && b[1] === 216;
  if (ext === '.webp') return b.subarray(0, 4).toString() === 'RIFF' && b.subarray(8, 12).toString() === 'WEBP';
  return false;
}

/** Start a separate process: wait for the spawn event to catch ENOENT and similar errors, then release it (unref). */
function startProcess(spawnFn, exe, args = [], { cwd, elevate = true } = {}) {
  return new Promise((resolve, reject) => {
    const bad = (e) => fail(`Failed to start "${path.basename(exe)}": ${e.message}`);
    let child;
    try { child = spawnFn(exe, args, { detached: true, stdio: 'ignore', cwd, windowsHide: false }); } catch (e) { return reject(bad(e)); }
    child.once('error', (e) => {
      // Installers often need administrator rights: CreateProcess answers EACCES/UNKNOWN (error 740), while the Windows shell shows a UAC prompt.
      if (elevate && !args.length && ['EACCES', 'EPERM', 'UNKNOWN'].includes(e.code)) return startProcess(spawnFn, 'explorer.exe', [exe], { cwd, elevate: false }).then(resolve, reject);
      reject(bad(e));
    });
    child.once('spawn', () => { child.unref(); resolve(); });
  });
}

const stripDownload = ({ download, ...tool }) => tool;

export function createVfxTools(options = {}) {
  const root = path.resolve(options.root || process.env.ASTRA_VFX_TOOLS || DEFAULT_ROOT);
  const catalog = options.catalog || loadCatalog();
  const spawnFn = options.spawn || nodeSpawn;
  const saveAsset = options.saveAsset || defaultSaveAsset;
  const searchRoots = options.searchRoots || defaultSearchRoots();
  const byId = new Map(catalog.tools.map((t) => [t.id, t]));
  const installedCache = new Map();
  const texturesDir = () => resolveInside(root, 'textures');
  const webPort = options.webPort ?? (Number(process.env.ASTRA_VFX_WEB_PORT) || DEFAULT_WEB_PORT);
  const web = { server: null, port: null, error: null, starting: null, closed: false };

  const tool = (id) => {
    const t = byId.get(String(id));
    if (!t) throw fail('Unknown tool.', 404);
    return t;
  };

  async function installedExe(t, fresh = false) {
    const hit = installedCache.get(t.id);
    if (!fresh && hit && Date.now() - hit.at < CACHE_MS) return hit.exe;
    const exe = await findInstalled(t.local.find, searchRoots);
    installedCache.set(t.id, { at: Date.now(), exe });
    return exe;
  }

  /** Program executable (desktop - downloaded, installer - installed) or null. */
  async function exeOf(t, fresh = false) {
    if (t.kind === 'desktop') return findExe(root, t.local.exe);
    if (t.kind === 'installer') return installedExe(t, fresh);
    return null;
  }

  const openerOf = (t) => { const o = byId.get(t.local?.openWith); return o ? exeOf(o) : null; };

  async function stateOf(t) {
    const s = { available: false, launchable: false, installerAvailable: false, installed: false };
    try {
      if (t.kind === 'desktop') s.available = s.launchable = !!(await findExe(root, t.local.exe));
      else if (t.kind === 'installer') {
        s.installerAvailable = await isFile(resolveInside(root, t.local.installer));
        s.installed = !!(await installedExe(t));
        s.launchable = s.installed;
        s.available = s.installed || s.installerAvailable;
      } else if (t.kind === 'web-local') s.available = await isFile(path.join(resolveInside(root, t.local.web), 'index.html'));
      else if (t.kind === 'file') {
        s.available = await isFile(resolveInside(root, t.local.file));
        s.launchable = s.available && !!(await openerOf(t));
      }
    } catch { /* a broken catalog entry must not break the whole list */ }
    return s;
  }

  async function list({ fresh = false } = {}) {
    if (fresh) {
      installedCache.clear();
      if (web.error) { web.error = null; web.starting = null; }   // the port may have been freed
    }
    await startWeb();
    const tools = await Promise.all(catalog.tools.map(async (t) => {
      const item = { ...stripDownload(t), ...(await stateOf(t)), webUrl: null };
      if (t.kind === 'web-local') {
        if (web.port) item.webUrl = `http://127.0.0.1:${web.port}/vfx-web/${t.id}/`;
        else item.webError = web.error || 'The generators web server is not running.';
      }
      return item;
    }));
    return { root, rootExists: await isDir(root), kinds: catalog.kinds, webError: web.error, tools };
  }

  async function launch(id) {
    const t = tool(id);
    if (t.kind === 'file') {
      const exe = await openerOf(t);
      const file = resolveInside(root, t.local.file);
      if (!(await isFile(file))) throw fail(`File "${path.basename(file)}" is not downloaded. ${INSTALL_HINT}`);
      if (!exe) throw fail(`Opening requires "${byId.get(t.local.openWith)?.name || t.local.openWith}", which is not downloaded. ${INSTALL_HINT}`);
      await startProcess(spawnFn, exe, [file], { cwd: path.dirname(exe) });
    } else if (t.kind === 'desktop' || t.kind === 'installer') {
      const exe = await exeOf(t, true);
      if (!exe) throw fail(t.kind === 'desktop' ? `"${t.name}" is not downloaded. ${INSTALL_HINT}` : `"${t.name}" is not installed. Click "Install".`);
      await startProcess(spawnFn, exe, [], { cwd: path.dirname(exe) });
    } else throw fail('This tool cannot be launched from the computer: open it through the "Website" link.');
    return { ok: true, id: t.id };
  }

  async function install(id) {
    const t = tool(id);
    if (t.kind !== 'installer') throw fail('This tool has no installer.');
    const file = resolveInside(root, t.local.installer);
    if (!(await isFile(file))) throw fail(`Installer "${path.basename(file)}" is not downloaded. ${INSTALL_HINT}`);
    installedCache.delete(t.id);
    await startProcess(spawnFn, file, [], { cwd: path.dirname(file) });
    return { ok: true, id: t.id };
  }

  /** Tool folder: for a program, the exe folder; for a web generator, the site folder; for a template, the file folder. */
  async function toolFolder(t) {
    let dir = null;
    if (t.kind === 'desktop') { const exe = await findExe(root, t.local.exe); dir = exe && path.dirname(exe); }
    else if (t.kind === 'installer') {
      const exe = await installedExe(t, true);
      dir = exe ? path.dirname(exe) : (await isFile(resolveInside(root, t.local.installer))) ? path.dirname(resolveInside(root, t.local.installer)) : null;
    } else if (t.kind === 'web-local') dir = resolveInside(root, t.local.web);
    else if (t.kind === 'file') dir = path.dirname(resolveInside(root, t.local.file));
    else throw fail('This tool has no local folder.');
    if (!dir || !(await isDir(dir))) throw fail(`Folder of "${t.name}" not found: the tool is not downloaded. ${INSTALL_HINT}`);
    return dir;
  }

  async function openFolder(id) {
    const dir = await toolFolder(tool(id));
    await startProcess(spawnFn, 'explorer.exe', [dir], { elevate: false });
    return { ok: true };
  }

  async function openRoot(where) {
    await fs.mkdir(texturesDir(), { recursive: true });
    await startProcess(spawnFn, 'explorer.exe', [where === 'textures' ? texturesDir() : root], { elevate: false });
    return { ok: true };
  }

  async function textures() {
    const dir = texturesDir();
    const items = [];
    for (const e of await listDir(dir)) {
      if (!e.isFile() || !isTextureName(e.name)) continue;
      const st = await fs.stat(path.join(dir, e.name)).catch(() => null);
      if (st?.isFile()) items.push({ name: e.name, size: st.size, mtimeMs: st.mtimeMs });
    }
    items.sort((a, b) => b.mtimeMs - a.mtimeMs || a.name.localeCompare(b.name));
    return items.slice(0, TEXTURE_LIMIT).map(({ name, size, mtimeMs }) => ({ name, size, mtime: new Date(mtimeMs).toISOString(), url: '/api/vfx-tools/textures/' + encodeURIComponent(name) }));
  }

  async function texturePath(name) {
    if (!isTextureName(name)) throw fail('Invalid file name: a PNG, JPG or WEBP without a path is required.');
    const file = path.join(texturesDir(), name);
    if (!(await isFile(file))) throw fail('Texture not found.', 404);
    return file;
  }

  async function importTexture(name) {
    const file = await texturePath(name);
    if ((await fs.stat(file)).size > TEXTURE_MAX_BYTES) throw fail('The file is larger than 100 MB.');
    const buffer = await fs.readFile(file);
    const ext = path.extname(name).toLowerCase();
    if (!formatMatches(buffer, ext)) throw fail('The file content does not match its format.');
    return saveAsset(buffer, ext, name);
  }

  const statics = new Map();
  /** express.static for a web generator folder; null if the id is not in the catalog or is not web-local. */
  function staticFor(id) {
    const t = byId.get(String(id));
    if (!t || t.kind !== 'web-local') return null;
    if (!statics.has(t.id)) statics.set(t.id, express.static(resolveInside(root, t.local.web), { index: 'index.html', dotfiles: 'ignore' }));
    return statics.get(t.id);
  }

  // Pages served from this port cannot reach the studio API: index.mjs rejects a foreign Origin and requests without x-astra-client.
  function webApp() {
    const app = express();
    app.disable('x-powered-by');
    app.use((req, res, next) => {
      const host = req.headers.host?.split(':')[0];
      if (!['127.0.0.1', 'localhost', '['].includes(host)) return res.status(403).type('text/plain; charset=utf-8').send('Invalid Host');
      res.setHeader('X-Content-Type-Options', 'nosniff');
      next();
    });
    app.use('/vfx-web/:id', (req, res, next) => {
      const serve = staticFor(req.params.id);
      if (!serve) return notFound(res);
      serve(req, res, (err) => (err ? next(err) : notFound(res)));
    });
    app.use((req, res) => notFound(res));
    app.use((err, req, res, next) => (res.headersSent ? next(err) : res.status(err.status || 500).type('text/plain; charset=utf-8').send('Request error')));
    return app;
  }

  /** Starts the web generators server once; does not throw: on an error (for example, EADDRINUSE) it writes the reason to web.error. */
  function startWeb() {
    if (!web.starting) web.starting = new Promise((resolve) => {
      if (web.closed) return resolve();
      const server = createServer(webApp());
      const onError = (e) => {
        web.error = e.code === 'EADDRINUSE' ? `Port ${webPort} is used by another program, so the web generators are unavailable. Set another port in ASTRA_VFX_WEB_PORT.` : `The web generators server is not running: ${e.message}`;
        console.warn('VFX: ' + web.error);
        resolve();
      };
      server.once('error', onError);
      server.listen(webPort, '127.0.0.1', () => {
        server.removeListener('error', onError);
        server.on('error', (e) => console.warn('VFX: web generators server: ' + e.message));
        web.server = server;
        web.port = server.address().port;
        resolve();
      });
    });
    return web.starting;
  }

  async function close() {
    web.closed = true;
    await web.starting;
    const server = web.server;
    web.server = null;
    if (server) await new Promise((resolve) => { server.close(() => resolve()); server.closeAllConnections?.(); });
  }

  return {
    root, catalog, list, launch, install, openFolder, openRoot, textures, texturePath, importTexture, staticFor, startWeb, close,
    get webPort() { return web.port; },
    get webError() { return web.error; },
    clearCache: () => installedCache.clear(),
  };
}

export function registerVfxTools(app, asyncRoute, options = {}) {
  const svc = createVfxTools(options);
  app.get('/api/vfx-tools', asyncRoute(async (req, res) => res.json(await svc.list({ fresh: req.query.fresh === '1' }))));
  app.get('/api/vfx-tools/textures', asyncRoute(async (req, res) => res.json(await svc.textures())));
  app.get('/api/vfx-tools/textures/:name', asyncRoute(async (req, res, next) => {
    const file = await svc.texturePath(req.params.name);
    res.sendFile(file, (err) => { if (err && !res.headersSent) next(err); });
  }));
  app.post('/api/vfx-tools/textures/:name/import', asyncRoute(async (req, res) => res.json(await svc.importTexture(req.params.name))));
  app.post('/api/vfx-tools/folder', asyncRoute(async (req, res) => res.json(await svc.openRoot(req.body?.where))));
  app.post('/api/vfx-tools/:id/launch', asyncRoute(async (req, res) => res.json(await svc.launch(req.params.id))));
  app.post('/api/vfx-tools/:id/install', asyncRoute(async (req, res) => res.json(await svc.install(req.params.id))));
  app.post('/api/vfx-tools/:id/folder', asyncRoute(async (req, res) => res.json(await svc.openFolder(req.params.id))));
  return svc;   // svc.close() closes the web generators server, svc.webPort is its port after the first GET /api/vfx-tools (or await svc.startWeb())
}
