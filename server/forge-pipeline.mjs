// Forge pipeline engine: asset manifests, versions, jobs, clip library, batches, exports.
// Used in-process by the server (server/forge.mjs) and by the CLI (scripts/forge.mjs --direct).
//
// Layout (docs/FORGE-CONTRACT.md §2, §8; docs/forge/API.md):
//   data/library/assets/<id>/asset.json            manifest (atomic writes)
//   data/library/assets/<id>/source/source.<ext>   copied input mesh (job / asset / upload)
//   data/library/assets/<id>/v001/version.json     recipe, base, final state of the version
//   data/library/assets/<id>/v001/01_cleanup/      out_dir of step 1 (request.json, result.json, files)
//   data/library/assets/<id>/exports/*.zip         zipped godot_package / render_sprites outputs
//   data/library/jobs/<jobId>.json                 job records (pid + heartbeat; a dead owner -> failed)
//   data/library/batches/<batchId>.json            batch records
//   data/library/animations/index.json             clip library (written by the clip_library step)
//   data/library/trash/                            deleted assets and clips (never hard-deleted)
import { randomUUID } from 'node:crypto';
import { createReadStream, createWriteStream, existsSync, promises as fs } from 'node:fs';
import path from 'node:path';
import JSZip from 'jszip';
import { z } from 'zod';
import { PROJECT_ROOT, StepQueue, blenderInfo, blenderQueue, godotInfo, runBlenderStep } from './blender.mjs';
import { gpuLockState } from './gpu-lock.mjs';
import {
  CLIP_CATEGORIES, EXPORT_PROFILES, KINDS, LOOP_CATEGORIES, applyProfileSize, blenderParams, creatureSizeWarning,
  defaultParams, getPreset, getStep, isGpuStep, normalizeSteps, pathParams, timeoutFor,
} from './forge-steps.mjs';

// ------------------------------------------------------------------ small helpers

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const isUuid = (v) => typeof v === 'string' && UUID_RE.test(v);
export const CLIP_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
export const isClipId = (v) => typeof v === 'string' && CLIP_ID_RE.test(v) && !v.includes('..');
export const FILE_EXTENSIONS = ['glb', 'gltf', 'fbx', 'blend', 'png', 'json', 'tres', 'tscn', 'gd', 'md', 'zip', 'txt', 'log'];
export const DOWNLOAD_EXTENSIONS = ['zip', 'blend', 'fbx', 'tres', 'tscn', 'gd', 'log', 'txt', 'md'];
export const UPLOAD_EXTENSIONS = ['.glb', '.fbx', '.obj'];
export const MAX_UPLOAD_BYTES = 300 * 1024 * 1024;
export const MAX_CLIP_FILES = 500;
const TERMINAL = ['completed', 'failed', 'cancelled'];
const SAFE_SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9._ ()+-]{0,200}$/;
// Owner liveness: the owning process rewrites heartbeatAt while a job is not finished. A job of
// another process counts as alive only if its pid is alive AND the heartbeat is fresh (Windows
// reuses PIDs quickly, so a live pid alone proves nothing).
const HEARTBEAT_MS = 20 * 1000;
const STALE_MS = 2 * 60 * 1000;
// A version that is queued/running but has no job record is an orphan after this grace period.
const ORPHAN_GRACE_MS = 2 * 60 * 1000;
const STALE_UPLOAD_MS = 24 * 60 * 60 * 1000;
const INTERRUPTED_MESSAGE = 'The job was interrupted: the process running it ended (a server restart or a closed window). Start it again.';

export function forgeError(message, status = 400, code = 'bad_request') {
  return Object.assign(new Error(message), { status, code });
}

const now = () => new Date().toISOString();
export const versionName = (n) => 'v' + String(n).padStart(3, '0');
export const parseVersion = (v) => (/^v(\d{3,})$/.test(String(v)) ? Number(String(v).slice(1)) : null);
export const stepDirName = (k, step) => String(k).padStart(2, '0') + '_' + step;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const abortError = (message = 'Cancelled') => Object.assign(new Error(message), { name: 'AbortError' });

/** setTimeout that rejects with AbortError when the signal aborts. */
function sleepAbortable(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const onAbort = () => { clearTimeout(timer); reject(abortError()); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

// Clip library lock (index.json and the clip files): clip_library jobs and clip removal hold it one
// at a time — in this process through a mutex, across processes (studio server + CLI --direct share
// data/library/animations) through a lock file created with 'wx'. The holder refreshes the file's
// mtime; a file whose owner pid is dead or that was not refreshed for staleMs is taken over.
export const LIBRARY_LOCK_FILE = '.library.lock';
const LIVE_LIBRARY_LOCKS = new Set(); // tokens held by engines of this process

class Mutex {
  constructor() { this.locked = false; this.queue = []; }

  tryLock() {
    if (this.locked) return false;
    this.locked = true;
    return true;
  }

  lock(signal) {
    if (!this.locked) { this.locked = true; return Promise.resolve(); }
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, cleanup: null };
      if (signal) {
        const onAbort = () => {
          const i = this.queue.indexOf(entry);
          if (i >= 0) this.queue.splice(i, 1);
          reject(abortError());
        };
        if (signal.aborted) return onAbort();
        signal.addEventListener('abort', onAbort, { once: true });
        entry.cleanup = () => signal.removeEventListener('abort', onAbort);
      }
      this.queue.push(entry);
    });
  }

  unlock() {
    const next = this.queue.shift();
    if (next) { next.cleanup?.(); next.resolve(); } else this.locked = false;
  }
}

export async function readJsonFile(file, fallback) {
  try {
    return JSON.parse(await fs.readFile(file, 'utf8'));
  } catch (err) {
    if (err.code === 'ENOENT') return fallback;
    throw err;
  }
}

/** Write JSON atomically (temp file + rename), retrying transient Windows sharing errors. */
export async function writeJsonAtomic(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.${randomUUID()}.tmp`;
  await fs.writeFile(temp, JSON.stringify(value, null, 2), 'utf8');
  for (let attempt = 0; ; attempt += 1) {
    try {
      await fs.rename(temp, file);
      return;
    } catch (err) {
      if (attempt >= 8 || !['EPERM', 'EBUSY', 'EACCES'].includes(err.code)) {
        await fs.rm(temp, { force: true }).catch(() => {});
        throw err;
      }
      await sleep(40 * (attempt + 1));
    }
  }
}

/** Move a file or folder, falling back to copy+remove across volumes. */
async function moveEntry(from, to) {
  await fs.mkdir(path.dirname(to), { recursive: true });
  for (let attempt = 0; ; attempt += 1) {
    try {
      await fs.rename(from, to);
      return;
    } catch (err) {
      if (err.code === 'EXDEV') {
        await fs.cp(from, to, { recursive: true });
        await fs.rm(from, { recursive: true, force: true });
        return;
      }
      // Windows: antivirus / indexer / a just-closed download can hold a handle for a moment.
      if (attempt >= 10 || !['EPERM', 'EBUSY', 'EACCES'].includes(err.code)) {
        if (['EPERM', 'EBUSY', 'EACCES'].includes(err.code)) {
          throw forgeError('The asset files are in use by another program. Close them and try again.', 409, 'busy');
        }
        throw err;
      }
      await sleep(100 * (attempt + 1));
    }
  }
}

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

/** Is `child` inside `parent` (both absolute)? */
export function isInside(parent, child) {
  const rel = path.relative(parent, child);
  return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel);
}

const fileExt = (p) => path.extname(String(p || '')).slice(1).toLowerCase();

const TRANSLIT = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm',
  н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'shch',
  ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
};
export const ASSET_ID_RE = /^[a-z][a-z0-9]*(_[a-z0-9]+)*$/;

/**
 * snake_case ASCII id for game packages (godot_package / render_sprites asset_id):
 * profile.asset_id when valid, else transliterated name + first 8 hex of the UUID.
 */
export function assetSlug(manifest) {
  const own = manifest?.profile?.asset_id;
  if (typeof own === 'string' && ASSET_ID_RE.test(own)) return own;
  const base = String(manifest?.name || '').toLowerCase().split('').map((ch) => TRANSLIT[ch] ?? ch).join('')
    .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40).replace(/_+$/, '');
  const head = /^[a-z]/.test(base) ? base : ('asset' + (base ? '_' + base : ''));
  const tail = String(manifest?.id || '').replace(/[^0-9a-f]/gi, '').slice(0, 8).toLowerCase();
  return tail ? `${head}_${tail}` : head;
}

// ------------------------------------------------------------------ upload sniffing

export function isFbxBuffer(buf) {
  if (!buf || buf.length < 8) return false;
  if (buf.subarray(0, 18).toString('latin1') === 'Kaydara FBX Binary') return true;
  const head = buf.subarray(0, 2048).toString('latin1').replace(/^﻿/, '');
  return /^\s*; FBX \d/m.test(head);
}

/** Validate an uploaded mesh by extension and magic bytes. Returns the normalised extension. */
export function sniffMeshUpload(buf, originalName) {
  const ext = path.extname(String(originalName || '')).toLowerCase();
  if (!UPLOAD_EXTENSIONS.includes(ext)) throw forgeError('Only GLB, FBX and OBJ files are supported.', 400, 'bad_format');
  if (!buf || !buf.length) throw forgeError('The file is empty.', 400, 'empty_file');
  if (buf.length > MAX_UPLOAD_BYTES) throw forgeError('The file is larger than 300 MB.', 413, 'too_large');
  let ok = false;
  if (ext === '.glb') ok = buf.subarray(0, 4).toString('latin1') === 'glTF';
  else if (ext === '.fbx') ok = isFbxBuffer(buf);
  else if (ext === '.obj') {
    const head = buf.subarray(0, 65536);
    ok = !head.includes(0) && /^(v|vt|vn|f|o|g|s|mtllib|usemtl|#)\s/m.test(head.toString('latin1'));
  }
  if (!ok) throw forgeError('The content does not match the file format.', 400, 'bad_signature');
  return ext;
}

// ------------------------------------------------------------------ clip library

/** Read index.json of the clip library ({clips: []} when missing). */
export async function readClipIndex(animationsDir) {
  const index = await readJsonFile(path.join(animationsDir, 'index.json'), null);
  if (!index) return { clips: [] };
  if (!Array.isArray(index.clips)) throw forgeError('The clip library file is corrupted: there is no clips list.', 500, 'bad_library');
  return index;
}

/**
 * Resolve retarget clip sources against the library.
 *   'library:<clipId>'           -> that clip
 *   'category:<cat>[:<tag>]'     -> first clip (sorted by id) of the category (and tag)
 *   anything else                -> passed through unchanged
 * Resolved entries get source='library:<id>', clip_id, file (absolute) and default name/loop.
 */
export function resolveClipSources(clips, index, animationsDir, { checkFiles = true } = {}) {
  if (!Array.isArray(clips)) throw forgeError('The clips parameter must be a list.', 400, 'bad_params');
  const all = Array.isArray(index?.clips) ? index.clips : [];
  const byId = new Map(all.filter((c) => c && typeof c.id === 'string').map((c) => [c.id, c]));
  return clips.map((raw, i) => {
    const entry = typeof raw === 'string' ? { source: raw } : { ...raw };
    const source = String(entry.source || '');
    let clip = null;
    let category = null;
    if (source.startsWith('library:')) {
      const id = source.slice('library:'.length);
      clip = byId.get(id);
      if (!clip) throw forgeError(`Clip "${id}" not found in the clip library.`, 400, 'clip_missing');
    } else if (source.startsWith('category:')) {
      const [cat, tag] = source.slice('category:'.length).split(':');
      category = cat;
      if (!CLIP_CATEGORIES.includes(cat)) {
        throw forgeError(`Clip #${i + 1}: unknown category "${cat}". Allowed: ${CLIP_CATEGORIES.join(', ')}.`, 400, 'bad_category');
      }
      const candidates = all
        .filter((c) => c && typeof c.id === 'string' && c.category === cat && (!tag || (Array.isArray(c.tags) && c.tags.includes(tag))))
        .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
      clip = candidates[0];
      if (!clip) {
        throw forgeError(
          `The clip library has no category "${cat}"${tag ? ` with tag "${tag}"` : ''}. ` +
          'Import FBX files from Mixamo: node scripts/forge.mjs clips import <folder> or the Clips tab.',
          400, 'clips_missing');
      }
    } else {
      return entry;
    }
    const file = clip.file ? path.resolve(animationsDir, clip.file) : null;
    if (file && !isInside(animationsDir, file)) throw forgeError(`Clip "${clip.id}": the file path is outside the library.`, 400, 'bad_library');
    if (checkFiles && (!file || !existsSync(file))) {
      throw forgeError(`The file of clip "${clip.id}" is missing from the library (${clip.file || 'not specified'}).`, 400, 'clip_file_missing');
    }
    const cat = clip.category || category;
    return {
      ...entry,
      requested: source,
      source: `library:${clip.id}`,
      clip_id: clip.id,
      file,
      name: entry.name || category || clip.id,
      loop: typeof entry.loop === 'boolean' ? entry.loop : LOOP_CATEGORIES.includes(cat),
      category: cat || null,
      skeleton: clip.skeleton || null,
    };
  });
}

// ------------------------------------------------------------------ sockets of a GLB (fx masks)

const GLB_JSON_CHUNK = 0x4e4f534a; // 'JSON'
const MAX_GLB_JSON_BYTES = 128 * 1024 * 1024;

/**
 * Socket names of a .glb/.gltf, read from its JSON only (no Blender): nodes with
 * extras.game_socket, or whose name starts with 'socket_' — the same rule godot_package uses.
 */
export async function readGlbSockets(file) {
  let json;
  try {
    if (fileExt(file) === 'gltf') {
      json = JSON.parse(await fs.readFile(file, 'utf8'));
    } else {
      const fd = await fs.open(file, 'r');
      try {
        const head = Buffer.alloc(20);
        const { bytesRead } = await fd.read(head, 0, 20, 0);
        if (bytesRead < 20 || head.toString('latin1', 0, 4) !== 'glTF') throw new Error('no glTF header');
        const length = head.readUInt32LE(12);
        if (head.readUInt32LE(16) !== GLB_JSON_CHUNK) throw new Error('the first chunk is not JSON');
        if (length > MAX_GLB_JSON_BYTES) throw new Error('the JSON chunk is too large');
        const body = Buffer.alloc(length);
        const read = await fd.read(body, 0, length, 20);
        if (read.bytesRead < length) throw new Error('the file is truncated');
        json = JSON.parse(body.toString('utf8'));
      } finally {
        await fd.close();
      }
    }
  } catch (err) {
    throw forgeError(`Failed to read the model sockets of ${path.basename(String(file))}: ${err.message}`, 400, 'bad_glb');
  }
  const names = [];
  for (const node of Array.isArray(json?.nodes) ? json.nodes : []) {
    const socket = node?.extras?.game_socket;
    const name = typeof node?.name === 'string' ? node.name : '';
    if (typeof socket === 'string' && socket) names.push(socket);
    else if (name.startsWith('socket_')) names.push(name);
  }
  return [...new Set(names)];
}

export const hasSocketMask = (entry) => typeof entry?.socket === 'string' && /[*?]/.test(entry.socket);

/**
 * Expand fx entries whose socket is a mask ('*' any run, '?' one character) into one entry per
 * matching socket (sorted by name). A mask that matches nothing is an error in Russian.
 */
export function expandSocketMasks(fx, sockets) {
  if (!Array.isArray(fx)) return fx;
  const out = [];
  fx.forEach((entry, i) => {
    if (!hasSocketMask(entry)) { out.push(entry); return; }
    const mask = entry.socket;
    const re = new RegExp('^' + mask.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
    const matched = sockets.filter((s) => re.test(s)).sort();
    if (!matched.length) {
      const known = sockets.length
        ? ` Model sockets: ${sockets.slice(0, 12).join(', ')}${sockets.length > 12 ? '…' : ''}.`
        : ' The model has no sockets at all.';
      throw forgeError(`Effect #${i + 1} "${entry.preset || '?'}": the socket mask "${mask}" matched no socket of the model.${known}`, 400, 'fx_socket_missing');
    }
    for (const socket of matched) out.push({ ...entry, socket });
  });
  return out;
}

/** First string inside params that looks like a local path ({key, value}) or null. */
/** place_parts: every point brings its own part (asset UUID or, on the CLI, a part path). */
export function everyPointHasPart(points) {
  return Array.isArray(points) && points.length > 0
    && points.every((pt) => pt && typeof pt === 'object' && !Array.isArray(pt) && (pt.asset || pt.part));
}

export function findLocalPath(value, key = '') {
  if (typeof value === 'string') {
    const v = value.trim();
    const looksLikePath = /^[A-Za-z]:[\\/]/.test(v) || /^[\\/]{2}/.test(v) || (v.startsWith('/') && v.length > 1)
      || /^file:/i.test(v) || /(^|[\\/])\.\.([\\/]|$)/.test(v);
    return looksLikePath ? { key: key || 'value', value: v } : null;
  }
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const hit = findLocalPath(value[i], `${key}[${i}]`);
      if (hit) return hit;
    }
    return null;
  }
  if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) {
      const hit = findLocalPath(v, key ? `${key}.${k}` : k);
      if (hit) return hit;
    }
  }
  return null;
}

// ------------------------------------------------------------------ zip

async function listFiles(dir, base = dir, skip = () => false) {
  const out = [];
  for (const ent of await fs.readdir(dir, { withFileTypes: true })) {
    const abs = path.join(dir, ent.name);
    const rel = path.relative(base, abs).split(path.sep).join('/');
    if (skip(rel, ent)) continue;
    if (ent.isDirectory()) out.push(...await listFiles(abs, base, skip));
    else if (ent.isFile()) out.push({ abs, rel });
  }
  return out;
}

/** Zip a folder into `dest` (streamed; temp file + rename). */
export async function zipFolder(dir, dest, { skip, prefix = '' } = {}) {
  const files = await listFiles(dir, dir, skip);
  const zip = new JSZip();
  for (const f of files) zip.file(prefix + f.rel, createReadStream(f.abs), { binary: true });
  await fs.mkdir(path.dirname(dest), { recursive: true });
  const temp = `${dest}.${randomUUID()}.tmp`;
  await new Promise((resolve, reject) => {
    const out = createWriteStream(temp);
    out.on('error', reject);
    out.on('finish', resolve);
    zip.generateNodeStream({ type: 'nodebuffer', streamFiles: true, compression: 'DEFLATE', compressionOptions: { level: 6 } })
      .on('error', reject)
      .pipe(out);
  });
  await fs.rename(temp, dest);
  return { path: dest, files: files.length };
}

// ------------------------------------------------------------------ patch schemas

const clipEntry = z.object({
  id: z.string().min(1).max(120),
  source: z.string().min(1).max(300),
  loop: z.boolean().optional(),
  in_place: z.boolean().optional(),
  trim: z.array(z.number()).length(2).optional(),
}).passthrough();
const propEntry = z.object({ asset: z.string().refine(isUuid, 'props[].asset must be an asset UUID'), socket: z.string().regex(/^socket_[a-z0-9_]+$/, 'Socket name: socket_<kind>_<place>') }).passthrough();
const fxEntry = z.object({ preset: z.string().min(1).max(80), socket: z.string().min(1).max(120), params: z.record(z.string(), z.unknown()).optional() }).passthrough();
const patchSchema = z.object({
  name: z.string().trim().min(1).max(150).optional(),
  profile: z.record(z.string(), z.unknown()).optional(),
  clips: z.array(clipEntry).max(200).optional(),
  props: z.array(propEntry).max(50).optional(),
  fx: z.array(fxEntry).max(100).optional(),
  // Review view (docs/forge/REVIEW.md): the owner's decision on a built character. `decision: null`
  // clears it; `at` is always set by the server (a client value is accepted and ignored).
  qa: z.object({
    reviewed: z.boolean().optional(),
    notes: z.string().max(5000).optional(),
    decision: z.enum(['approved', 'rework', 'rejected']).nullable().optional(),
    comment: z.string().max(2000).optional(),
    game_unit: z.string().trim().max(80).optional(),
    at: z.string().max(40).optional(),
  }).strict().optional(),
}).strict();

const KIND_TITLES = { humanoid: 'Humanoid', creature: 'Creature', item: 'Item', ship: 'Ship', prop: 'Prop' };
const KIND_PROFILES = { humanoid: { height_m: 1.8 }, creature: { template: 'auto' }, item: {}, ship: {}, prop: {} };

// ------------------------------------------------------------------ engine

/** Default forge library: C:\AstraForge\library on Windows (short paths), data/library elsewhere. */
export function defaultLibraryDir(dataDir) {
  return process.platform === 'win32' ? 'C:\\AstraForge\\library' : path.join(dataDir, 'library');
}

/** Default forge runtime (Blender logs, forge work files): C:\AstraForge\runtime on Windows. */
export function defaultRuntimeDir(dataDir) {
  return process.platform === 'win32' ? 'C:\\AstraForge\\runtime' : path.join(dataDir, 'runtime');
}

export class ForgeEngine {
  constructor(opts = {}) {
    this.root = opts.root || PROJECT_ROOT;
    this.dataDir = opts.dataDir || path.join(this.root, 'data');
    // Short default paths on Windows: Blender and Godot break on paths over MAX_PATH (260), and step
    // folders nest deep (assets/<uuid>/v001/03_godot_package/<asset>/_godot_check/...).
    this.libraryDir = path.resolve(opts.libraryDir || process.env.ASTRA_FORGE_LIBRARY || defaultLibraryDir(this.dataDir));
    this.runtimeDir = path.resolve(opts.runtimeDir || process.env.ASTRA_FORGE_RUNTIME || defaultRuntimeDir(this.dataDir));
    // Blender steps read the clip library default from the environment (steps/_anim_lib.py).
    if (!opts.libraryDir && !process.env.ASTRA_FORGE_LIBRARY) process.env.ASTRA_FORGE_LIBRARY = this.libraryDir;
    this.studioAssetsDir = opts.studioAssetsDir || path.join(this.dataDir, 'assets');
    this.studioJobsDir = opts.studioJobsDir || path.join(this.dataDir, 'jobs');
    this.assetsDir = path.join(this.libraryDir, 'assets');
    this.jobsDir = path.join(this.libraryDir, 'jobs');
    this.batchesDir = path.join(this.libraryDir, 'batches');
    this.trashDir = path.join(this.libraryDir, 'trash');
    this.animationsDir = path.join(this.libraryDir, 'animations');
    this.blenderLogDir = path.join(this.runtimeDir, 'blender');
    this.workDir = path.join(this.runtimeDir, 'forge');
    this.runStep = opts.runStep || runBlenderStep;
    this.blenderQueue = opts.blenderQueue || blenderQueue;
    this.jobQueue = new StepQueue(opts.jobConcurrency || Number(process.env.ASTRA_FORGE_JOB_CONCURRENCY) || this.blenderQueue.concurrency || 1);
    this.getGenerationJob = opts.getGenerationJob || null;
    this.blenderInfo = opts.blenderInfo || (() => blenderInfo());
    this.godotInfo = opts.godotInfo || (() => godotInfo());
    this.host = opts.host || 'server';
    // Only the CLI (--direct) may reference files by local path; HTTP clients upload bytes.
    this.allowLocalPaths = opts.allowLocalPaths ?? false;
    this.urlBase = opts.urlBase ?? '/api/forge';
    this.persistIntervalMs = opts.persistIntervalMs ?? 1000;
    this.heartbeatMs = opts.heartbeatMs ?? HEARTBEAT_MS;
    this.staleMs = opts.staleMs ?? STALE_MS;
    this.orphanGraceMs = opts.orphanGraceMs ?? ORPHAN_GRACE_MS;
    this.libraryLockPollMs = opts.libraryLockPollMs ?? 500;
    this.libraryMutex = new Mutex();
    this.jobs = new Map();
    this.owned = new Set(); // jobs created (and executed) by this engine
    this.controllers = new Map();
    this.waiters = new Map();
    this.locks = new Map();
    this.jobWrites = new Map();
    this.persistTimers = new Map();
    this.writeFailures = new Map();
    this.retryTimers = new Map();
    this.heartbeatTimer = null;
    this.ready = null;
  }

  init() {
    if (!this.ready) this.ready = this._init();
    return this.ready;
  }

  async _init() {
    for (const dir of [this.assetsDir, this.jobsDir, this.batchesDir, this.trashDir, this.animationsDir, this.blenderLogDir, this.workDir]) {
      await fs.mkdir(dir, { recursive: true });
    }
    let files = [];
    try { files = await fs.readdir(this.jobsDir); } catch { files = []; }
    for (const file of files) {
      if (!file.endsWith('.json') || !isUuid(file.slice(0, -5))) continue;
      let job;
      try { job = await readJsonFile(path.join(this.jobsDir, file), null); } catch { continue; }
      if (!job || !isUuid(job.id)) continue;
      // a live owner with a fresh heartbeat -> external (e.g. CLI --direct); otherwise interrupted
      if (!TERMINAL.includes(job.status)) await this._adopt(job);
      this.jobs.set(job.id, job);
    }
    await this._repairOrphanVersions().catch((err) => console.warn('[forge] check for versions without jobs:', err.message));
    await this._cleanupStaleUploads().catch(() => {});
  }

  // ---------------------------------------------------------------- job ownership

  /** 'own' | 'terminal' | 'external' (live owner, fresh heartbeat) | 'dead'. */
  _ownerState(job) {
    if (this.owned.has(job.id)) return 'own';
    if (TERMINAL.includes(job.status)) return 'terminal';
    // Our own pid on a job we did not create = an earlier process that had the same pid.
    if (!Number.isInteger(job.pid) || job.pid <= 0 || job.pid === process.pid) return 'dead';
    if (!isPidAlive(job.pid)) return 'dead';
    const beat = Date.parse(job.heartbeatAt || job.updatedAt || '');
    if (!Number.isFinite(beat) || Date.now() - beat > this.staleMs) return 'dead';
    return 'external';
  }

  /** Reconcile a job record that this engine does not own (read from disk). */
  async _adopt(job) {
    const state = this._ownerState(job);
    if (state === 'dead') await this._markInterrupted(job);
    else if (state === 'external') job.external = true;
    else delete job.external;
    return job;
  }

  async _markInterrupted(job) {
    const message = INTERRUPTED_MESSAGE;
    job.status = 'failed';
    job.error = message;
    job.message = message;
    job.finishedAt = now();
    delete job.external;
    for (const s of job.steps || []) {
      if (s.status === 'running') { s.status = 'failed'; s.message = message; s.finishedAt = job.finishedAt; }
      else if (s.status === 'queued') s.status = 'cancelled';
    }
    await this._writeJob(job, true);
    if (job.cleanupDir) await this._cleanupStaged(job.cleanupDir);
    if (job.assetId && isUuid(job.assetId) && job.version && existsSync(this._manifestPath(job.assetId))) {
      await this._settleVersion(job.assetId, job.version, job.id, message);
    }
  }

  /** Mark a version (and its pending steps in version.json and the manifest) failed. */
  async _settleVersion(assetId, version, jobId, message) {
    const at = now();
    const fix = (entries) => {
      for (const e of entries || []) {
        if (e.jobId === jobId && e.status === 'pending') {
          e.status = 'failed';
          e.result = { ok: false, error: message };
          e.at = at;
        }
      }
    };
    if (parseVersion(version) !== null) {
      const file = path.join(this.assetDir(assetId), version, 'version.json');
      await this._serial('versionfile:' + file, async () => {
        const vj = await readJsonFile(file, null);
        if (!vj) return;
        fix(vj.pipeline);
        if (!TERMINAL.includes(vj.status)) { vj.status = 'failed'; vj.finishedAt = at; vj.final = null; }
        await writeJsonAtomic(file, vj);
      }).catch(() => {});
    }
    await this._updateManifest(assetId, async (m) => {
      fix(m.pipeline);
      const v = (m.versions || []).find((x) => x.version === version);
      if (v && !TERMINAL.includes(v.status)) { v.status = 'failed'; v.finishedAt = at; }
      await this._rebuildPipeline(m);
    }).catch(() => {});
  }

  /**
   * Versions left 'queued'/'running' without any job record (the process died before writing
   * it) are marked failed, so the manifest does not show "running" forever.
   */
  async _repairOrphanVersions() {
    let names = [];
    try { names = await fs.readdir(this.assetsDir); } catch { return; }
    for (const name of names) {
      if (!isUuid(name)) continue;
      const m = await readJsonFile(path.join(this.assetsDir, name, 'asset.json'), null).catch(() => null);
      if (!m?.id || !isUuid(m.id) || !Array.isArray(m.versions)) continue;
      for (const v of m.versions) {
        if (!v || TERMINAL.includes(v.status)) continue;
        if (isUuid(v.jobId) && (this.jobs.has(v.jobId) || existsSync(this._jobFile(v.jobId)))) continue;
        const age = Date.now() - Date.parse(v.createdAt || '');
        if (Number.isFinite(age) && age < this.orphanGraceMs) continue;
        await this._settleVersion(m.id, v.version, v.jobId, 'The job record of this version was not saved (the process ended earlier). Run the pipeline again.');
      }
    }
  }

  /** Staged uploads are removed by their request/job; leftovers of crashes go after a day. */
  async _cleanupStaleUploads() {
    const dir = path.join(this.workDir, 'uploads');
    let names = [];
    try { names = await fs.readdir(dir); } catch { return; }
    for (const name of names) {
      const abs = path.join(dir, name);
      const st = await fs.stat(abs).catch(() => null);
      if (st?.isDirectory() && Date.now() - st.mtimeMs > STALE_UPLOAD_MS) await fs.rm(abs, { recursive: true, force: true }).catch(() => {});
    }
  }

  async _cleanupStaged(dir) {
    const uploads = path.join(this.workDir, 'uploads');
    const abs = path.resolve(String(dir || ''));
    if (dir && isInside(uploads, abs)) await fs.rm(abs, { recursive: true, force: true }).catch(() => {});
  }

  /** While this engine has unfinished jobs, rewrite their heartbeat (see _ownerState). */
  _startHeartbeat() {
    if (this.heartbeatTimer || !(this.heartbeatMs > 0)) return;
    this.heartbeatTimer = setInterval(() => {
      let active = 0;
      for (const id of this.owned) {
        const job = this.jobs.get(id);
        if (!job || TERMINAL.includes(job.status)) continue;
        active += 1;
        void this._writeJob(job, true);
      }
      if (!active) {
        clearInterval(this.heartbeatTimer);
        this.heartbeatTimer = null;
      }
    }, this.heartbeatMs);
    this.heartbeatTimer.unref?.();
  }

  // ---------------------------------------------------------------- paths & locks

  assetDir(id) {
    if (!isUuid(id)) throw forgeError('Invalid asset ID.', 400, 'bad_id');
    return path.join(this.assetsDir, id.toLowerCase());
  }

  _manifestPath(id) {
    return path.join(this.assetDir(id), 'asset.json');
  }

  _serial(key, fn) {
    const prev = this.locks.get(key) || Promise.resolve();
    const next = prev.catch(() => {}).then(fn);
    const tail = next.catch(() => {});
    this.locks.set(key, tail);
    tail.then(() => { if (this.locks.get(key) === tail) this.locks.delete(key); });
    return next;
  }

  fileUrl(id, abs) {
    if (!abs || typeof abs !== 'string') return null;
    const dir = this.assetDir(id);
    const full = path.resolve(abs);
    if (!isInside(dir, full)) return null;
    const rel = path.relative(dir, full).split(path.sep).map(encodeURIComponent).join('/');
    return `${this.urlBase}/assets/${id}/files/${rel}`;
  }

  // ---------------------------------------------------------------- manifests

  async getAsset(id) {
    const m = await readJsonFile(this._manifestPath(id), null);
    if (!m) throw forgeError('Asset not found.', 404, 'not_found');
    return m;
  }

  async _updateManifest(id, mutate) {
    return this._serial('asset:' + id, async () => {
      const m = await this.getAsset(id);
      const out = (await mutate(m)) || m;
      out.updated = now();
      await writeJsonAtomic(this._manifestPath(id), out);
      return out;
    });
  }

  _summary(m) {
    const pipeline = m.pipeline || [];
    let lastStatus = 'new';
    if (pipeline.some((e) => e.status === 'pending')) lastStatus = 'running';
    else if (pipeline.some((e) => e.status === 'failed')) lastStatus = 'failed';
    else if (pipeline.length) lastStatus = 'done';
    let thumbnail = null;
    for (let i = pipeline.length - 1; i >= 0 && !thumbnail; i -= 1) {
      const preview = pipeline[i].result?.outputs?.preview;
      if (pipeline[i].status === 'done' && typeof preview === 'string' && preview.toLowerCase().endsWith('.png')) {
        thumbnail = this.fileUrl(m.id, preview);
      }
    }
    return { id: m.id, name: m.name, kind: m.kind, updated: m.updated, thumbnail, lastStatus };
  }

  async listAssets() {
    let names = [];
    try { names = await fs.readdir(this.assetsDir); } catch { return []; }
    const out = [];
    for (const name of names) {
      if (!isUuid(name)) continue;
      const m = await readJsonFile(path.join(this.assetsDir, name, 'asset.json'), null).catch(() => null);
      if (m && m.id) out.push(this._summary(m));
    }
    return out.sort((a, b) => String(b.updated).localeCompare(String(a.updated)));
  }

  /**
   * Create an asset manifest.
   * @param {{name?:string, kind:string, source:{type:string, jobId?:string, assetId?:string, params?:object}, profile?:object}} input
   * @param {{buffer?:Buffer, path?:string, originalName?:string}} [file] for source.type 'upload'
   */
  async createAsset(input, file = null) {
    if (!input || typeof input !== 'object') throw forgeError('A request body with kind and source is required.', 400, 'bad_request');
    const kind = input.kind;
    if (!KINDS.includes(kind)) throw forgeError(`Asset kind: ${KINDS.join(', ')}.`, 400, 'bad_kind');
    const source = input.source || { type: 'params' };
    if (!['job', 'asset', 'upload', 'params'].includes(source.type)) {
      throw forgeError('Source: job, asset, upload or params.', 400, 'bad_source');
    }
    if (input.profile !== undefined && (typeof input.profile !== 'object' || input.profile === null || Array.isArray(input.profile))) {
      throw forgeError('profile must be an object.', 400, 'bad_profile');
    }
    let name = typeof input.name === 'string' ? input.name.trim().slice(0, 150) : '';

    // Gather the source file (all validation happens before anything is written).
    let copyFrom = null;
    let buffer = null;
    let ext = null;
    const src = { type: source.type };
    if (source.type === 'job') {
      if (!isUuid(source.jobId)) throw forgeError('Invalid generation job ID.', 400, 'bad_id');
      const job = await this._generationJob(source.jobId);
      if (!job) throw forgeError('Generation job not found.', 404, 'not_found');
      if (job.status !== 'completed' || !job.asset || job.asset.extension !== '.glb' || !isUuid(job.asset.id)) {
        throw forgeError('The job has no completed GLB model.', 409, 'job_not_ready');
      }
      copyFrom = path.join(this.studioAssetsDir, job.asset.id + '.glb');
      if (!existsSync(copyFrom)) throw forgeError('The job\'s model file is missing from data/assets.', 404, 'file_missing');
      ext = '.glb';
      Object.assign(src, { jobId: source.jobId, assetId: job.asset.id, prompt: job.prompt || null, image: job.imageId || job.concept?.id || null });
      if (!name) name = (job.prompt || '').trim().slice(0, 80);
    } else if (source.type === 'asset') {
      if (!isUuid(source.assetId)) throw forgeError('Invalid source asset ID.', 400, 'bad_id');
      const forge = await readJsonFile(this._manifestPath(source.assetId), null);
      if (forge) {
        const latest = await this._latestFile(forge, ['glb']).catch(() => null);
        copyFrom = latest?.file || (forge.source?.file ? path.join(this.assetDir(forge.id), forge.source.file) : null);
        if (!copyFrom || !existsSync(copyFrom)) throw forgeError('The source asset has no finished model.', 409, 'no_model');
        ext = path.extname(copyFrom).toLowerCase();
        Object.assign(src, { assetId: forge.id, forgeAsset: true, version: latest?.version || null });
        if (!name) name = `${forge.name} (copy)`.slice(0, 150);
      } else {
        const info = await readJsonFile(path.join(this.studioAssetsDir, source.assetId + '.json'), null);
        if (!info || !['.glb', '.obj', '.stl', '.fbx'].includes(info.extension)) {
          throw forgeError('The source asset was not found or is not a model.', 404, 'not_found');
        }
        copyFrom = path.join(this.studioAssetsDir, source.assetId + info.extension);
        if (!existsSync(copyFrom)) throw forgeError('The source asset\'s file is missing.', 404, 'file_missing');
        ext = info.extension;
        Object.assign(src, { assetId: source.assetId, originalName: info.name || null });
        if (!name && info.name) name = String(info.name).slice(0, 150);
      }
    } else if (source.type === 'upload') {
      if (!file || (!file.buffer && !file.path)) throw forgeError('No file was provided.', 400, 'no_file');
      if (file.path && !this.allowLocalPaths && !file.staged) throw forgeError('Local paths are only available in --direct mode.', 400, 'no_local_paths');
      const originalName = file.originalName || (file.path ? path.basename(file.path) : 'upload');
      if (file.path) {
        const st = await fs.stat(file.path).catch(() => null);
        if (!st || !st.isFile()) throw forgeError(`File not found: ${file.path}`, 404, 'file_missing');
        if (st.size > MAX_UPLOAD_BYTES) throw forgeError('The file is larger than 300 MB.', 413, 'too_large');
        const head = Buffer.alloc(Math.min(st.size, 65536));
        const fd = await fs.open(file.path, 'r');
        try { await fd.read(head, 0, head.length, 0); } finally { await fd.close(); }
        ext = sniffMeshUpload(head, originalName);
        copyFrom = file.path;
      } else {
        buffer = file.buffer;
        ext = sniffMeshUpload(buffer, originalName);
      }
      src.originalName = originalName.replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(-150);
      if (!name) name = path.basename(src.originalName, ext).slice(0, 150);
    } else {
      const params = source.params ?? {};
      if (typeof params !== 'object' || Array.isArray(params) || params === null) throw forgeError('source.params must be an object.', 400, 'bad_params');
      if (JSON.stringify(params).length > 65536) throw forgeError('source.params is too large.', 400, 'bad_params');
      src.params = params;
    }

    const id = randomUUID();
    const dir = this.assetDir(id);
    await fs.mkdir(dir, { recursive: false });
    if (copyFrom || buffer) {
      await fs.mkdir(path.join(dir, 'source'));
      const target = path.join(dir, 'source', 'source' + ext);
      if (buffer) await fs.writeFile(target, buffer);
      else await fs.copyFile(copyFrom, target);
      src.file = 'source/source' + ext;
    }
    const at = now();
    const manifest = {
      id,
      name: name || KIND_TITLES[kind],
      kind,
      created: at,
      updated: at,
      source: src,
      profile: { ...KIND_PROFILES[kind], ...(input.profile || {}) },
      pipeline: [],
      clips: [],
      props: [],
      fx: [],
      exports: [],
      qa: { checks: [], reviewed: false },
      versions: [],
    };
    await writeJsonAtomic(path.join(dir, 'asset.json'), manifest);
    return manifest;
  }

  async _generationJob(jobId) {
    if (this.getGenerationJob) {
      const j = await this.getGenerationJob(jobId);
      if (j) return j;
    }
    return readJsonFile(path.join(this.studioJobsDir, jobId + '.json'), null);
  }

  async patchAsset(id, patch) {
    const parsed = patchSchema.safeParse(patch || {});
    if (!parsed.success) throw forgeError(parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; '), 400, 'validation');
    const p = parsed.data;
    return this._updateManifest(id, (m) => {
      if (p.name !== undefined) m.name = p.name;
      if (p.profile !== undefined) m.profile = { ...m.profile, ...p.profile };
      if (p.clips !== undefined) m.clips = p.clips;
      if (p.props !== undefined) m.props = p.props;
      if (p.fx !== undefined) m.fx = p.fx;
      if (p.qa !== undefined) {
        m.qa = { checks: [], reviewed: false, ...(m.qa || {}) };
        if (Object.prototype.hasOwnProperty.call(p.qa, 'reviewed')) {
          m.qa.reviewed = p.qa.reviewed;
          m.qa.reviewedAt = now();
        }
        if (p.qa.notes !== undefined) m.qa.notes = p.qa.notes;
        if (p.qa.decision === null) {
          for (const key of ['decision', 'comment', 'game_unit', 'at']) delete m.qa[key];
          if (!Object.prototype.hasOwnProperty.call(p.qa, 'reviewed')) { m.qa.reviewed = false; m.qa.reviewedAt = now(); }
        } else if (p.qa.decision !== undefined) {
          m.qa.decision = p.qa.decision;
          m.qa.comment = p.qa.comment ?? '';
          m.qa.game_unit = p.qa.game_unit ?? '';
          m.qa.at = now();
          if (!Object.prototype.hasOwnProperty.call(p.qa, 'reviewed')) { m.qa.reviewed = true; m.qa.reviewedAt = m.qa.at; }
        } else if (m.qa.decision) { // comment / unit of an existing decision edited on their own
          if (p.qa.comment !== undefined) m.qa.comment = p.qa.comment;
          if (p.qa.game_unit !== undefined) m.qa.game_unit = p.qa.game_unit;
        }
      }
    });
  }

  /** DELETE: move the asset folder into data/library/trash/assets/ (never hard-delete). */
  async trashAsset(id) {
    const dir = this.assetDir(id);
    await this.getAsset(id);
    await this._syncJobs(); // jobs of dead processes become failed here instead of blocking forever
    const active = [...this.jobs.values()].find((j) => j.assetId === id && !TERMINAL.includes(j.status));
    if (active) throw forgeError('A job is running for this asset. Cancel it or wait for it to finish.', 409, 'busy');
    const target = path.join(this.trashDir, 'assets', `${id}-${now().replace(/[:.]/g, '-')}`);
    await moveEntry(dir, target);
    return { ok: true, trash: target };
  }

  // ---------------------------------------------------------------- versions & state

  _sourceState(id, m) {
    if (!m.source?.file) return {};
    const abs = path.join(this.assetDir(id), m.source.file);
    if (!isInside(this.assetDir(id), abs)) return {};
    const ext = fileExt(abs);
    if (ext === 'blend') return { blend: abs };
    if (ext === 'glb' || ext === 'gltf') return { glb: abs, mesh: abs };
    return { mesh: abs };
  }

  _latestCompleted(m) {
    return [...(m.versions || [])]
      .filter((v) => v.status === 'completed')
      .sort((a, b) => parseVersion(b.version) - parseVersion(a.version))[0] || null;
  }

  /** Latest file of a given kind for an asset: final state of the newest completed version, else source. */
  async _latestFile(m, kinds = ['glb']) {
    const v = this._latestCompleted(m);
    if (v) {
      const vj = await readJsonFile(path.join(this.assetDir(m.id), v.version, 'version.json'), null);
      const fin = vj?.final || v.final || {};
      for (const k of kinds) if (fin[k] && existsSync(fin[k])) return { file: fin[k], version: v.version };
    }
    const st = this._sourceState(m.id, m);
    for (const k of kinds) if (st[k] && existsSync(st[k])) return { file: st[k], version: null };
    return null;
  }

  async _resolveBase(id, m, from) {
    const warnings = [];
    const f = from || 'source';
    if (f === 'source') return { from: 'source', base: null, state: this._sourceState(id, m), recipe: [], warnings };
    let v = null;
    if (f === 'latest') {
      v = this._latestCompleted(m);
      if (!v) {
        warnings.push('No completed versions - starting from the asset source.');
        return { from: 'source', base: null, state: this._sourceState(id, m), recipe: [], warnings };
      }
    } else if (parseVersion(f) !== null) {
      v = (m.versions || []).find((x) => x.version === f);
      if (!v) throw forgeError(`Version ${f} not found.`, 404, 'version_missing');
      if (v.status !== 'completed') throw forgeError(`Version ${f} is not completed (${v.status}).`, 409, 'version_incomplete');
    } else {
      throw forgeError('from: allowed values are source, latest or vNNN.', 400, 'bad_from');
    }
    const vj = await readJsonFile(path.join(this.assetDir(id), v.version, 'version.json'), null);
    return { from: f, base: v.version, state: { ...(vj?.final || v.final || {}) }, recipe: structuredClone(vj?.pipeline || []), warnings };
  }

  async _allocateVersion(id) {
    return this._serial('version:' + id, async () => {
      const dir = this.assetDir(id);
      let max = 0;
      for (const name of await fs.readdir(dir)) {
        const n = parseVersion(name);
        if (n !== null && n > max) max = n;
      }
      for (let n = max + 1; ; n += 1) {
        const v = versionName(n);
        try {
          await fs.mkdir(path.join(dir, v));
          return v;
        } catch (err) {
          if (err.code !== 'EEXIST') throw err;
        }
      }
    });
  }

  // ---------------------------------------------------------------- input resolution

  /**
   * Inputs for one step from the current chain state (API.md "Versions and input resolution").
   * state = {blend?, glb?, mesh?}; ctx = {manifest, manifestSnapshot, params}
   */
  async resolveInputs(stepName, state, ctx = {}) {
    const def = getStep(stepName);
    if (!def) throw forgeError(`Unknown step "${stepName}".`, 400, 'unknown_step');
    const slots = def.inputs?.slots || [];
    const formats = def.inputs?.formats || [];
    if (!slots.length) return {};
    const title = `Step "${def.title}" (${stepName})`;
    if (stepName === 'attach_prop') {
      // a rigged GLB / .blend of the chain, or a rigged FBX (source upload or convert output); the items cut out
      // by extract_props come back onto the .blend (their placement sockets live on the rig's hand bones)
      const extracted = !!ctx.params?.extracted;
      const character = (extracted ? state.blend || state.glb : state.glb || state.blend) ||
        [state.mesh, state.fbx].find((p) => p && fileExt(p) === 'fbx');
      if (!character) throw forgeError(`${title} needs a character: a GLB, .blend or FBX from the previous step.`, 400, 'no_input');
      const inputs = { character };
      const attachments = Array.isArray(ctx.params?.attachments) ? ctx.params.attachments : [];
      if (extracted) {
        if (!state.extracted || !existsSync(state.extracted)) {
          throw forgeError(`${title} with extracted needs the props.json of the extract_props step earlier in this run.`, 400, 'no_extracted');
        }
        inputs.extracted = state.extracted;
      } else if (!attachments.length) {
        // params.attachments carry their own items (resolved in _prepareParams); otherwise props[0]
        const props = ctx.manifest?.props || [];
        const propId = ctx.params?.prop_asset || props[0]?.asset;
        if (!propId) throw forgeError(`${title} needs a prop: add an asset to the passport's props.`, 400, 'no_prop');
        inputs.item = await this._propItem(propId);
      }
      if (Array.isArray(ctx.params?.clips) && ctx.params.clips.length) inputs.clips = await this._clipInputs(ctx.params.clips);
      return inputs;
    }
    if (stepName === 'replace_part' || stepName === 'place_parts') {
      // the body / target from the chain (a model file preferred, a .blend accepted) + inputs.part: the
      // part from another asset (part_asset, also how an upload gets in) or a local file (part_file,
      // CLI only). place_parts may instead give every point its own part (points[].asset, resolved in
      // _prepareParams, or points[].part on the CLI).
      const mesh = [state.glb, state.mesh, state.fbx].filter(Boolean).find((p) => fileExt(p) !== 'blend' && formats.includes(fileExt(p)));
      const inputs = mesh ? { mesh } : state.blend ? { blend: state.blend } : null;
      const what = stepName === 'replace_part' ? 'body model' : 'target model';
      if (!inputs) throw forgeError(`${title} needs the ${what}: a GLB, FBX, OBJ or .blend from the previous step or the source.`, 400, 'no_input');
      if (ctx.params?.part_file) inputs.part = ctx.params.part_file;
      else if (ctx.params?.part_asset) inputs.part = await this._assetMesh(ctx.params.part_asset, 'part');
      else if (stepName === 'place_parts' && everyPointHasPart(ctx.params?.points)) return inputs;
      else if (stepName === 'place_parts') throw forgeError(`${title} needs a part: part_asset (the UUID of an asset with the part; make an upload into an asset first), asset on every point, or part_file / points[].part (CLI --direct only).`, 400, 'no_part');
      else throw forgeError(`${title} needs a new part: part_asset (the UUID of an asset with the part; make an upload into an asset first) or part_file (CLI --direct only).`, 400, 'no_part');
      return inputs;
    }
    if (stepName === 'paint_from_concept') {
      // the model of the chain (.blend first: sockets and every object stay) + inputs.concept: params.concept_file
      // (CLI) > params.concept_asset (studio image) > the prepared RGBA reference of the generation job the asset
      // was made from (with the original image + framing as inputs.concept_original, for colour under faint alpha)
      const mesh = [state.glb, state.mesh, state.fbx].filter(Boolean).find((p) => fileExt(p) !== 'blend' && formats.includes(fileExt(p)));
      const inputs = state.blend ? { blend: state.blend } : mesh ? { mesh } : null;
      if (!inputs) throw forgeError(`${title} needs a model: a .blend, GLB, FBX or OBJ from the previous step or the source.`, 400, 'no_input');
      return Object.assign(inputs, await this._conceptInputs(ctx.manifest, ctx.params, title), await this._conceptBackInputs(ctx.params));
    }
    if (stepName === 'assemble_parts') {
      // the whole-body base of the chain (.blend first, like paint_from_concept) + inputs.concept (the FRONT reference the parts
      // were cut from: concept_file > concept_asset > the generation job's reference) + inputs.parts: parts.json of
      // scripts/parts (a local path: params.parts_file, CLI --direct only)
      const mesh = [state.glb, state.mesh, state.fbx].filter(Boolean).find((p) => fileExt(p) !== 'blend' && formats.includes(fileExt(p)));
      const inputs = state.blend ? { blend: state.blend } : mesh ? { mesh } : null;
      if (!inputs) throw forgeError(`${title} needs a whole mesh: a .blend, GLB, FBX or OBJ from the previous step or the source.`, 400, 'no_input');
      if (!ctx.params?.parts_file) throw forgeError(`${title} needs parts_file - the parts.json from scripts/parts (part frames and their meshes; CLI --direct only).`, 400, 'no_parts');
      inputs.parts = ctx.params.parts_file;
      return Object.assign(inputs, await this._conceptInputs(ctx.manifest, ctx.params, title));
    }
    const withExtras = (inputs) => {
      if (def.inputs.manifest && ctx.manifestSnapshot) inputs.manifest = ctx.manifestSnapshot;
      // sprites rendered earlier for this model go into the Godot package (render_sprites output)
      const sprites = state.exports?.render_sprites?.sprites_json;
      if (stepName === 'godot_package' && typeof sprites === 'string' && existsSync(sprites)) inputs.sprites = sprites;
      return inputs;
    };
    if (slots.includes('glb')) {
      if (!state.glb) throw forgeError(`${title} needs a GLB from the previous step or the source.`, 400, 'no_input');
      return withExtras({ glb: state.glb });
    }
    for (const slot of slots) {
      if (slot === 'blend' && state.blend) return withExtras({ blend: state.blend });
      if (slot === 'mesh') {
        const candidates = [state.glb, state.mesh, state.fbx].filter(Boolean);
        const mesh = candidates.find((p) => !formats.length || formats.includes(fileExt(p)));
        if (mesh) return withExtras({ mesh });
        if (formats.includes('blend') && state.blend) return withExtras({ mesh: state.blend });
      }
    }
    if (!def.inputs.required) return {};
    const need = slots.map((s) => (s === 'blend' ? '.blend' : s === 'mesh' ? `model (${formats.join(', ')})` : s)).join(' or');
    throw forgeError(`${title} needs an input: ${need}. Neither the previous step nor the asset source provided it.`, 400, 'no_input');
  }

  /**
   * Chain state after an export step (godot_package, render_sprites): the model (.blend/.glb)
   * stays what it was, the package outputs are recorded under state.exports[step], so later
   * steps and version.final keep using the rig/animation .blend, not the packaged GLB.
   */
  static recordExport(state, step, outputs = {}) {
    const kept = {};
    for (const [k, v] of Object.entries(outputs || {})) {
      if (typeof v === 'string' || (Array.isArray(v) && v.every((x) => typeof x === 'string'))) kept[k] = v;
    }
    return { ...state, exports: { ...(state.exports || {}), [step]: kept } };
  }

  /** Next chain state after a step: a scene-changing step replaces both blend and glb. */
  static advanceState(state, outputs = {}) {
    const first = (v) => (Array.isArray(v) ? v.find((x) => typeof x === 'string') : typeof v === 'string' ? v : null);
    const blend = first(outputs.blend);
    let glb = first(outputs.glb);
    if (!glb) {
      const mesh = first(outputs.mesh);
      if (mesh && fileExt(mesh) === 'glb') glb = mesh;
    }
    const next = (blend || glb) ? { blend: blend || null, glb: glb || null, mesh: glb || null } : { ...state };
    const fbx = first(outputs.fbx);
    if (fbx) next.fbx = fbx;
    // the items extract_props cut out (props.json) stay known to the later steps of the run (attach_prop extracted)
    const props = first(outputs.props_json);
    if (props) next.extracted = props;
    else if (state.extracted && !next.extracted) next.extracted = state.extracted;
    for (const k of Object.keys(next)) if (next[k] === null) delete next[k];
    return next;
  }

  async _clipIndex() {
    return readClipIndex(this.animationsDir);
  }

  /** Latest model of another asset (replace_part / place_parts inputs.part, place_parts points[].asset): newest completed version's GLB, else its source. */
  async _assetMesh(assetId, what = 'model') {
    if (!isUuid(assetId)) throw forgeError('Invalid asset ID.', 400, 'bad_id');
    const other = await readJsonFile(this._manifestPath(assetId), null);
    if (!other) throw forgeError(`Asset "${what}" not found.`, 404, 'asset_missing');
    const hit = await this._latestFile(other, ['glb', 'mesh']);
    if (!hit) throw forgeError(`Asset "${other.name}" has no finished model (a GLB or an uploaded file).`, 409, 'asset_not_ready');
    return hit.file;
  }

  /** Image file of a studio asset (data/assets/<id>.png|jpg|jpeg|webp) — ship_from_mesh paint.asset, paint_from_concept concept_asset. */
  async _studioImage(assetId, what = 'paint.asset', purpose = 'concept markup') {
    if (!isUuid(assetId)) throw forgeError(`Invalid studio image ID (${what}).`, 400, 'bad_id');
    for (const ext of ['.png', '.jpg', '.jpeg', '.webp']) {
      const file = path.join(this.studioAssetsDir, assetId.toLowerCase() + ext);
      if (existsSync(file)) return file;
    }
    throw forgeError(`The studio image for ${purpose} (${what}) was not found.`, 404, 'asset_missing');
  }

  /** The generation job an asset comes from: source.type 'job', or the job of the forge asset it was copied from. */
  async _sourceGenerationJob(manifest, depth = 0) {
    const src = manifest?.source;
    if (!src || depth > 4) return null;
    if (src.type === 'job' && isUuid(src.jobId)) return this._generationJob(src.jobId);
    if (src.type === 'asset' && src.forgeAsset && isUuid(src.assetId)) {
      const other = await readJsonFile(this._manifestPath(src.assetId), null);
      return other ? this._sourceGenerationJob(other, depth + 1) : null;
    }
    return null;
  }

  /**
   * inputs.concept (+ concept_original) of paint_from_concept: params.concept_file (CLI) > params.concept_asset >
   * the generation job's prepared reference (original RGB + rembg alpha, data/assets/<reference.id>.png), with the
   * original upload and the reference framing so the step can restore colour where rembg left a faint alpha;
   * a job without a reference gives its original image (border-keyed by the step).
   */
  async _conceptInputs(manifest, params, title) {
    if (params?.concept_file) return { concept: params.concept_file };
    if (params?.concept_asset) return { concept: await this._studioImage(params.concept_asset, 'concept_asset', 'concept painting') };
    const job = await this._sourceGenerationJob(manifest);
    const ref = job?.preparedConcept?.reference;
    if (ref && isUuid(ref.id)) {
      const file = path.join(this.studioAssetsDir, ref.id.toLowerCase() + (ref.extension || '.png'));
      if (existsSync(file)) {
        const out = { concept: file };
        const fr = ref.framing;
        const origId = ref.originalId || job.imageId;
        if (fr && Array.isArray(fr.crop) && fr.crop.length === 4 && Array.isArray(fr.contentRect) && fr.contentRect.length === 4 && isUuid(origId)) {
          const image = await this._studioImage(origId, 'originalId').catch(() => null);
          if (image) out.concept_original = { image, crop: fr.crop, content_rect: fr.contentRect };
        }
        return out;
      }
    }
    const imageId = job?.imageId || job?.concept?.id;
    if (isUuid(imageId)) {
      const image = await this._studioImage(imageId).catch(() => null);
      if (image) return { concept: image };
    }
    throw forgeError(`${title} needs a concept: an asset from a studio generation (its reference image is used), concept_asset (the UUID of a studio image) or concept_file (a path, CLI --direct only).`, 400, 'no_concept');
  }

  /** inputs.concept_back of paint_from_concept: params.concept_back_file (CLI) > params.concept_back_asset (studio image). */
  async _conceptBackInputs(params) {
    if (params?.concept_back_file) return { concept_back: params.concept_back_file };
    if (params?.concept_back_asset) return { concept_back: await this._studioImage(params.concept_back_asset, 'concept_back_asset', 'back-view painting') };
    return {};
  }

  /** Latest GLB of a prop asset (attach_prop item). */
  async _propItem(propId) {
    if (!isUuid(propId)) throw forgeError('Invalid prop asset ID.', 400, 'bad_id');
    const propManifest = await readJsonFile(this._manifestPath(propId), null);
    if (!propManifest) throw forgeError('Prop asset not found.', 404, 'prop_missing');
    const item = await this._latestFile(propManifest, ['glb']);
    if (!item) throw forgeError(`Prop "${propManifest.name}" has no finished GLB.`, 409, 'prop_not_ready');
    return item.file;
  }

  /**
   * attach_prop params.clips -> inputs.clips ({file, name, loop}): library:<id> / category:… are
   * resolved against the clip library like retarget clips; other entries (CLI --direct paths)
   * pass through as {file, name, loop}.
   */
  async _clipInputs(clips) {
    const resolved = resolveClipSources(clips, await this._clipIndex(), this.animationsDir);
    return resolved.map((c) => {
      const entry = typeof c === 'string' ? { source: c } : c;
      const out = { file: entry.file || entry.path || entry.source };
      if (entry.name) out.name = entry.name;
      if (typeof entry.loop === 'boolean') out.loop = entry.loop;
      return out;
    });
  }

  /**
   * Fill server-side params (asset_id, library), resolve retarget clip sources and expand fx
   * socket masks of godot_package against the sockets of its input GLB.
   */
  async _prepareParams(stepName, params, asset, inputs = {}) {
    const def = getStep(stepName);
    const out = { ...params };
    for (const [k, spec] of Object.entries(def?.params || {})) {
      const library = spec.auto === 'animations' || spec.auto === 'library';
      // HTTP clients never choose the library folder (startRun already rejects it; this is the backstop)
      if (library && !this.allowLocalPaths) { out[k] = this.animationsDir; continue; }
      if (out[k] !== null && out[k] !== undefined) continue;
      if (spec.auto === 'asset_id' && asset) out[k] = asset.id;
      if (spec.auto === 'asset_slug' && asset) out[k] = assetSlug(asset);
      if (library) out[k] = this.animationsDir;
    }
    if (stepName === 'retarget' && Array.isArray(out.clips)) {
      out.clips = resolveClipSources(out.clips, await this._clipIndex(), this.animationsDir);
    }
    if (stepName === 'attach_prop' && Array.isArray(out.attachments)) {
      // {asset: <prop UUID>} -> {item: <its latest GLB>}; the recipe keeps the UUID
      const attachments = [];
      for (const a of out.attachments) {
        if (a && typeof a === 'object' && a.asset) {
          const { asset: propId, ...rest } = a;
          attachments.push({ ...rest, item: await this._propItem(propId), asset: propId });
        } else {
          attachments.push(a);
        }
      }
      out.attachments = attachments;
    }
    if (stepName === 'place_parts' && Array.isArray(out.points)) {
      // {asset: <part UUID>} -> part: <its latest GLB>; the recipe keeps the UUID
      const cache = new Map();
      const points = [];
      for (const [i, pt] of out.points.entries()) {
        if (pt && typeof pt === 'object' && !Array.isArray(pt) && pt.asset) {
          if (!cache.has(pt.asset)) cache.set(pt.asset, await this._assetMesh(pt.asset, `part of point #${i + 1}`));
          points.push({ ...pt, part: cache.get(pt.asset) });
        } else {
          points.push(pt);
        }
      }
      out.points = points;
    }
    if (stepName === 'ship_from_mesh' && out.paint && typeof out.paint === 'object' && out.paint.asset) {
      // concept painting from a studio image asset (HTTP clients cannot pass paths): {asset: UUID} -> image path
      out.paint = { ...out.paint, image: await this._studioImage(out.paint.asset) };
    }
    if (stepName === 'godot_package') {
      // fx given in params, else the passport's fx (the step falls back to manifest.fx itself,
      // but it cannot expand masks, so masked passport fx are expanded and sent explicitly)
      const fx = Array.isArray(out.fx) ? out.fx : (out.fx === undefined && Array.isArray(asset?.fx) && asset.fx.some(hasSocketMask) ? asset.fx : null);
      if (fx && fx.some(hasSocketMask)) {
        if (!inputs?.glb) throw forgeError('The socket mask in fx cannot be expanded: the godot_package step has no input GLB.', 400, 'no_input');
        out.fx = expandSocketMasks(fx, await readGlbSockets(inputs.glb));
      }
    }
    return blenderParams(stepName, out);
  }

  /** HTTP clients upload bytes: no local paths in step params (CLI --direct may use them). */
  _assertNoLocalPaths(steps) {
    for (const s of steps) {
      const params = s.params || {};
      for (const k of pathParams(s.step)) {
        if (params[k] === undefined || params[k] === null) continue;
        const auto = getStep(s.step)?.params?.[k]?.auto;
        throw forgeError(`Step "${s.step}": parameter "${k}" is a path on disk; it is not accepted over HTTP${auto ? ' (the server fills it in)' : ' (upload the file through the studio or use CLI --direct)'}.`, 400, 'no_local_paths');
      }
      if (s.step === 'attach_prop' && Array.isArray(params.attachments)) {
        params.attachments.forEach((a, i) => {
          if (a && typeof a === 'object' && ('item' in a || 'path' in a)) {
            throw forgeError(`Item #${i + 1}: over HTTP an item is given as an asset (asset: UUID), not as a file path.`, 400, 'no_local_paths');
          }
        });
      }
      if (s.step === 'place_parts') {
        // a part path relative to nothing would still reach Blender: the part of a point is an asset over HTTP
        if (params.part !== undefined) throw forgeError('Step "place_parts": over HTTP a part is given as an asset (part_asset: UUID), not as a path (part).', 400, 'no_local_paths');
        (Array.isArray(params.points) ? params.points : []).forEach((pt, i) => {
          if (pt && typeof pt === 'object' && !Array.isArray(pt) && 'part' in pt) {
            throw forgeError(`Point #${i + 1}: over HTTP a point's part is given as an asset (asset: UUID), not as a file path (part).`, 400, 'no_local_paths');
          }
        });
      }
      if ((s.step === 'retarget' || s.step === 'attach_prop') && Array.isArray(params.clips)) {
        params.clips.forEach((c, i) => {
          const entry = typeof c === 'string' ? { source: c } : (c && typeof c === 'object' ? c : {});
          const src = String(entry.source || '');
          const allowed = src.startsWith('library:') || src.startsWith('category:') || isClipId(src);
          if (!allowed || 'file' in entry || 'path' in entry) {
            throw forgeError(`Clip #${i + 1}: over HTTP only the sources library:<id>, category:<category>[:<tag>] or a library clip id are allowed - no file paths.`, 400, 'no_local_paths');
          }
        });
      }
      const hit = findLocalPath(params);
      if (hit) {
        throw forgeError(`Step "${s.step}": "${hit.key}" looks like a file path (${hit.value.slice(0, 80)}); paths are not accepted over HTTP - upload the file through the studio or use CLI --direct.`, 400, 'no_local_paths');
      }
    }
  }

  /**
   * manifest.pipeline = recipe of the latest completed version (all 'done'), followed by the own
   * steps of runs that are still queued/running, and of the most recent failed/cancelled run
   * after that version (pending/failed attempts, so the UI can show them).
   * Replaying the 'done' entries in order rebuilds the latest completed version.
   */
  async _rebuildPipeline(m) {
    const versions = (m.versions || []).filter((v) => v && parseVersion(v.version) !== null)
      .sort((a, b) => parseVersion(a.version) - parseVersion(b.version));
    if (!versions.length) return;
    const dir = this.assetDir(m.id);
    const current = Array.isArray(m.pipeline) ? m.pipeline : [];
    const readVersion = (v) => readJsonFile(path.join(dir, v.version, 'version.json'), null).catch(() => null);
    const latest = [...versions].reverse().find((v) => v.status === 'completed') || null;
    const latestN = latest ? parseVersion(latest.version) : 0;
    let pipeline = [];
    if (latest) {
      const vj = await readVersion(latest);
      pipeline = Array.isArray(vj?.pipeline) ? vj.pipeline : current.filter((e) => e.status === 'done');
    }
    const lastFailed = [...versions].reverse()
      .find((v) => TERMINAL.includes(v.status) && v.status !== 'completed' && parseVersion(v.version) > latestN) || null;
    for (const v of versions) {
      const active = !TERMINAL.includes(v.status);
      if (!active && v !== lastFailed) continue;
      const vj = await readVersion(v);
      const source = Array.isArray(vj?.pipeline) ? vj.pipeline : current;
      pipeline.push(...source.filter((e) => e && e.jobId === v.jobId && e.version === v.version));
    }
    m.pipeline = pipeline;
  }

  // ---------------------------------------------------------------- runs

  /**
   * Start a pipeline run on an asset.
   * @param {string} id
   * @param {{steps?:Array, preset?:string, from?:string}} body
   * @returns {Promise<{jobId:string, version:string, warnings:string[]}>}
   */
  async startRun(id, body = {}, meta = {}) {
    const m = await this.getAsset(id);
    if (body.preset && body.steps) throw forgeError('Specify either preset or steps.', 400, 'bad_request');
    let rawSteps = body.steps;
    const from = body.from || 'source';
    if (body.preset) {
      const preset = getPreset(body.preset);
      if (!preset) throw forgeError(`Unknown preset "${body.preset}".`, 400, 'unknown_preset');
      rawSteps = preset.steps;
    }
    // Asset source params feed the first generator step (item_kit, ship_gen, turret_gen).
    if (from === 'source' && m.source?.type === 'params' && m.source.params && Array.isArray(rawSteps) && rawSteps[0]) {
      const first = getStep(rawSteps[0].step);
      if (first && !(first.inputs?.slots || []).length && first.scope !== 'library') {
        rawSteps = rawSteps.map((s, i) => (i === 0
          ? { ...s, params: body.preset ? { ...(s.params || {}), ...m.source.params } : { ...m.source.params, ...(s.params || {}) } }
          : s));
      }
    }
    const { steps, warnings } = normalizeSteps(rawSteps);
    // passport size -> rig_creature / creature cleanup (recorded in the recipe like any explicit param)
    warnings.push(...applyProfileSize(steps, m));
    for (const s of steps) {
      const def = getStep(s.step);
      if (def.scope === 'library') throw forgeError(`Step "${s.step}" works with the clip library, not with an asset.`, 400, 'bad_scope');
      if (def.kinds.length && !def.kinds.includes(m.kind)) warnings.push(`Step "${s.step}" is meant for kinds ${def.kinds.join(', ')}, but the asset is ${m.kind}.`);
    }
    if (!this.allowLocalPaths) this._assertNoLocalPaths(steps);
    const base = await this._resolveBase(id, m, from);
    warnings.push(...base.warnings);

    // Preflight: fail fast on problems that would only show up after minutes of Blender work.
    const firstDef = getStep(steps[0].step);
    if (firstDef.inputs?.required && firstDef.inputs.slots.length && steps[0].step !== 'attach_prop') {
      await this.resolveInputs(steps[0].step, base.state, { manifest: m, manifestSnapshot: 'preflight', params: steps[0].params });
    }
    for (const s of steps) {
      if (s.step === 'replace_part') {
        if (!s.params.region) throw forgeError('Step replace_part needs the region: {min: [x, y, z], max: [x, y, z]} in meters.', 400, 'bad_params');
        if (!s.params.part_file && !s.params.part_asset) {
          throw forgeError('Step replace_part needs a part: part_asset (the UUID of an asset with the part) or part_file (CLI --direct only).', 400, 'no_part');
        }
        if (!s.params.part_file) await this._assetMesh(s.params.part_asset, 'part');
      }
      if (s.step === 'place_parts') {
        const points = Array.isArray(s.params.points) ? s.params.points : [];
        if (!s.params.part_file && !s.params.part_asset && !everyPointHasPart(points)) {
          throw forgeError('Step place_parts needs a part: part_asset (the UUID of an asset with the part), asset on every point, or part_file / points[].part (CLI --direct only).', 400, 'no_part');
        }
        if (!s.params.part_file && s.params.part_asset) await this._assetMesh(s.params.part_asset, 'part');
        for (const [i, pt] of points.entries()) if (pt && typeof pt === 'object' && pt.asset) await this._assetMesh(pt.asset, `part of point #${i + 1}`);
      }
      if (s.step === 'retarget') resolveClipSources(s.params.clips || [], await this._clipIndex(), this.animationsDir);
      if (s.step === 'attach_prop' && s.params.extracted) {
        // the items come from an extract_props step earlier in the same run
        if (!steps.slice(0, steps.indexOf(s)).some((x) => x.step === 'extract_props')) {
          throw forgeError('Step attach_prop with extracted needs the extract_props step earlier in this run.', 400, 'no_extracted');
        }
      } else if (s.step === 'attach_prop') {
        const attachments = Array.isArray(s.params.attachments) ? s.params.attachments : [];
        const propId = s.params.prop_asset || m.props?.[0]?.asset;
        if (!propId && !attachments.length) throw forgeError('For the attach_prop step, add a prop to the passport\'s props.', 400, 'no_prop');
        for (const a of attachments) if (a?.asset) await this._propItem(a.asset);
        if (Array.isArray(s.params.clips)) resolveClipSources(s.params.clips, await this._clipIndex(), this.animationsDir);
      }
    }

    const version = await this._allocateVersion(id);
    const jobId = randomUUID();
    const at = now();
    const versionDir = path.join(this.assetDir(id), version);
    const names = steps.map((s) => s.step);
    const job = {
      id: jobId,
      type: meta.type || 'run',
      assetId: id,
      version,
      batchId: meta.batchId || null,
      exportProfile: meta.exportProfile || null,
      status: 'queued',
      progress: 0,
      message: 'Waiting in the queue',
      from: base.from,
      base: base.base,
      baseState: base.state,
      createdAt: at,
      updatedAt: at,
      startedAt: null,
      finishedAt: null,
      host: this.host,
      pid: process.pid,
      warnings,
      error: null,
      steps: steps.map((s, i) => {
        const dir = stepDirName(i + 1, s.step);
        const dup = names.filter((n) => n === s.step).length > 1;
        return {
          index: i + 1, step: s.step, params: s.params, dir, outDir: path.join(versionDir, dir),
          logName: dup ? `${dir}.log` : `${s.step}.log`,
          status: 'queued', progress: 0, message: '', startedAt: null, finishedAt: null,
          result: null, logTail: [], logFile: null, warnings: [],
        };
      }),
    };
    const entries = job.steps.map((s) => ({
      step: s.step, params: s.params, status: 'pending', result: null, version, at, jobId, index: s.index, dir: s.dir,
    }));
    const recipe = [...base.recipe, ...entries];
    // The job record is written first (forced, not throttled): a version that mentions a job
    // always has a record, so recovery after a crash can settle it.
    this.jobs.set(jobId, job);
    this.owned.add(jobId);
    await this._writeJob(job, true);
    try {
      await writeJsonAtomic(path.join(versionDir, 'version.json'), {
        version, assetId: id, jobId, type: job.type, from: base.from, base: base.base, createdAt: at, status: 'queued', pipeline: recipe, final: null,
      });
      await this._updateManifest(id, async (mm) => {
        mm.versions = [...(mm.versions || []), {
          version, from: base.from, base: base.base, type: job.type, jobId, status: 'queued', createdAt: at, steps: names,
        }];
        await this._rebuildPipeline(mm);
      });
    } catch (err) {
      await this._finishJob(job, 'failed', `Failed to write the asset passport: ${err.message}`);
      throw err;
    }
    this._enqueue(job);
    return { jobId, version, warnings };
  }

  /** POST /assets/:id/export -> new version with the export step, from the latest completed version. */
  async startExport(id, { profile, params } = {}) {
    const def = EXPORT_PROFILES[profile];
    if (!def) throw forgeError(`Export profile: ${Object.keys(EXPORT_PROFILES).join(', ')}.`, 400, 'bad_profile');
    if (params !== undefined && (typeof params !== 'object' || params === null || Array.isArray(params))) {
      throw forgeError('params must be an object.', 400, 'bad_params');
    }
    return this.startRun(id, { steps: [{ step: def.step, params: { ...def.params, ...(params || {}) } }], from: 'latest' }, { type: 'export', exportProfile: profile });
  }

  _enqueue(job) {
    const controller = new AbortController();
    this.controllers.set(job.id, controller);
    this.owned.add(job.id);
    this._startHeartbeat();
    this.jobQueue.run((slot) => this._execute(job, controller.signal, slot), { signal: controller.signal })
      .catch(async (err) => {
        // nothing here may reject: an unhandled rejection would take the whole studio down
        try {
          if (err?.name === 'AbortError' && job.status === 'queued') await this._finishJob(job, 'cancelled', 'Cancelled before start');
          else if (!TERMINAL.includes(job.status)) await this._finishJob(job, 'failed', err?.message || String(err));
        } catch (finishErr) {
          console.warn(`[forge] failed to finish job ${job.id}:`, finishErr?.message || finishErr);
        }
      })
      .finally(() => this.controllers.delete(job.id));
  }

  /**
   * Run the steps of a job. `slot` is its place in the job queue: a GPU step gives it back while
   * it waits for the GPU mutex and renders, so other jobs are not blocked behind that wait.
   */
  async _execute(job, signal, slot = null) {
    job.status = 'running';
    job.startedAt = now();
    job.message = 'Starting';
    await this._writeJob(job);
    let state = { ...(job.baseState || {}) };
    const manifest = job.assetId ? await this.getAsset(job.assetId) : null;
    const versionDir = job.assetId ? path.join(this.assetDir(job.assetId), job.version) : null;
    let snapshot = null;
    if (versionDir) {
      snapshot = path.join(versionDir, 'asset.snapshot.json');
      await writeJsonAtomic(snapshot, manifest);
    }
    let failure = null;
    let cancelled = false;
    for (const s of job.steps) {
      if (signal.aborted) { cancelled = true; break; }
      if (slot && !slot.held) {
        // back from a GPU step: take a job-queue place again (ahead of jobs not started yet)
        const back = await slot.reacquire(signal);
        if (!back || signal.aborted) { cancelled = true; break; }
      }
      const def = getStep(s.step);
      job.message = `${def?.title || s.step}: preparing`;
      let inputs;
      let params;
      try {
        inputs = await this.resolveInputs(s.step, state, { manifest, manifestSnapshot: snapshot, params: s.params });
        params = await this._prepareParams(s.step, s.params, manifest, inputs);
      } catch (err) {
        failure = err.message;
        await this._stepDone(job, s, 'failed', { ok: false, error: err.message }, err.message);
        break;
      }
      const gpu = isGpuStep(s.step, params);
      s.status = 'queued';
      s.message = gpu ? 'Waiting for the GPU and the Blender queue' : 'Waiting for the Blender queue';
      s.request = { inputs, params, out_dir: s.outDir };
      await this._writeJob(job, true);
      if (gpu && slot) slot.release();
      let releaseLibrary = null;
      try {
        if (def?.scope === 'library') {
          // one clip_library job at a time, in this process and across processes (index.json)
          releaseLibrary = await this._acquireLibraryLock({
            signal,
            owner: job.id,
            onWait: () => {
              s.message = 'Waiting for the clip library: another import or clip removal is running';
              job.message = `${def?.title || s.step}: waiting for the clip library`;
              void this._writeJob(job, true);
            },
          });
        }
        const run = await this.runStep({
          step: s.step,
          request: {
            step: s.step, inputs, params, out_dir: s.outDir,
            asset: manifest ? { id: manifest.id, name: manifest.name, kind: manifest.kind, profile: manifest.profile } : null,
            job: { id: job.id, version: job.version, index: s.index },
          },
          jobId: job.id,
          logDir: path.join(this.blenderLogDir, job.id),
          logName: s.logName,
          timeoutSec: timeoutFor(s.step, params),
          gpu,
          signal,
          onStart: () => {
            s.status = 'running';
            s.startedAt = now();
            s.message = 'Running';
            job.message = `${def?.title || s.step}: running`;
            void this._writeJob(job, true);
          },
          onLog: (entry) => {
            if (entry?.msg) {
              s.message = String(entry.msg).slice(0, 300);
              s.logTail = [...(s.logTail || []), s.message].slice(-30);
            }
            void this._writeJob(job);
          },
          onProgress: (pct) => {
            s.progress = pct;
            job.progress = this._jobProgress(job);
            void this._writeJob(job);
          },
        });
        const result = run.result || {};
        s.logTail = run.logTail;
        s.logFile = run.logFile;
        s.warnings = [...(run.warnings || []), ...(result.warnings || [])];
        if (result.ok === false) {
          const failedChecks = (result.checks || []).filter((c) => c && c.ok === false && (c.severity || 'error') === 'error').map((c) => c.name);
          failure = `Step "${s.step}" failed: ${result.error || (failedChecks.length ? 'checks ' + failedChecks.join(', ') : 'ok=false')}`;
          await this._stepDone(job, s, 'failed', result, failure);
          break;
        }
        for (const [k, v] of Object.entries(result.outputs || {})) {
          if (typeof v === 'string' && path.isAbsolute(v) && !existsSync(v)) s.warnings.push(`Output ${k} is missing on disk: ${v}`);
        }
        if (s.step === 'rig_creature') {
          // native size kept (no height_m / length_m) and implausible for the template
          const sizeWarn = creatureSizeWarning(result.stats?.template, result.stats?.size, params);
          if (sizeWarn) {
            s.warnings.push(sizeWarn);
            job.warnings = [...(job.warnings || []), sizeWarn];
          }
        }
        // export steps record their package but keep the model of the chain (.blend/.glb)
        state = def?.export
          ? ForgeEngine.recordExport(state, s.step, result.outputs || {})
          : ForgeEngine.advanceState(state, result.outputs || {});
        let exported = null;
        if (def?.export && job.assetId) {
          try {
            exported = await this._packageExport(job, s, result);
          } catch (err) {
            s.warnings.push(`Failed to pack the export: ${err.message}`);
          }
        }
        await this._stepDone(job, s, 'completed', result, null, exported);
      } catch (err) {
        s.logTail = err.logTail || s.logTail;
        s.logFile = err.logFile || s.logFile;
        if (err.name === 'AbortError' || signal.aborted) {
          cancelled = true;
          await this._stepDone(job, s, 'cancelled', { ok: false, error: 'Cancelled by the user', cancelled: true }, 'Cancelled by the user');
        } else {
          failure = err.message;
          // af_error: the step's own AFERROR text, unchanged (e.g. render_sprites' units hint)
          const payload = err.payload ? { trace: err.payload.trace, af_error: err.payload.error ?? null } : {};
          await this._stepDone(job, s, 'failed', { ok: false, error: err.message, code: err.code || null, ...payload }, err.message);
        }
        break;
      } finally {
        if (releaseLibrary) await releaseLibrary();
      }
    }
    const status = cancelled ? 'cancelled' : failure ? 'failed' : 'completed';
    await this._finishJob(job, status, cancelled ? 'Cancelled by the user' : failure, state);
  }

  _jobProgress(job) {
    const n = job.steps.length || 1;
    let sum = 0;
    for (const s of job.steps) sum += s.status === 'completed' ? 100 : (s.status === 'running' ? (s.progress || 0) : 0);
    return Math.round((sum / n) * 10) / 10;
  }

  async _stepDone(job, s, status, result, message, exported = null) {
    s.status = status;
    s.finishedAt = now();
    s.result = result;
    s.message = message || (status === 'completed' ? 'Done' : s.message);
    if (status === 'completed') s.progress = 100;
    job.progress = this._jobProgress(job);
    await this._writeJob(job, true);
    if (job.assetId) {
      await this._manifestEntry(job, s, status === 'completed' ? 'done' : 'failed', result, exported);
    }
  }

  async _manifestEntry(job, s, status, result, exported = null) {
    if (!job.assetId) return;
    const urls = {};
    for (const [k, v] of Object.entries(result?.outputs || {})) {
      const u = typeof v === 'string' ? this.fileUrl(job.assetId, v) : null;
      if (u) urls[k] = u;
    }
    const resolvedClips = Array.isArray(s.request?.params?.clips) && s.step === 'retarget'
      ? s.request.params.clips.map((c) => ({ requested: c.requested || c.source, source: c.source, clip_id: c.clip_id || null, name: c.name || null }))
      : null;
    const apply = (e) => {
      e.status = status;
      e.result = result;
      e.urls = urls;
      e.version = job.version;
      e.at = now();
      if (resolvedClips) e.resolvedClips = resolvedClips;
    };
    await this._updateVersionFile(job, (vj) => {
      const e = (vj.pipeline || []).find((x) => x.jobId === job.id && x.index === s.index);
      if (e) apply(e);
    });
    await this._updateManifest(job.assetId, (m) => {
      const e = (m.pipeline || []).find((x) => x.jobId === job.id && x.index === s.index);
      if (e) apply(e);
      if (status === 'done' && Array.isArray(result?.checks) && result.checks.length) {
        m.qa = { checks: [], reviewed: false, ...(m.qa || {}) };
        m.qa.checks = [...(m.qa.checks || []).filter((c) => c.step !== s.step), ...result.checks.map((c) => ({ ...c, step: s.step, version: job.version }))];
      }
      if (exported) m.exports = [...(m.exports || []), exported];
    }).catch(() => {});
  }

  async _packageExport(job, s, result) {
    const outputs = result.outputs || {};
    const exportsDir = path.join(this.assetDir(job.assetId), 'exports');
    const profile = String(s.params?.profile || job.exportProfile || defaultParams(s.step).profile || s.step).replace(/[^A-Za-z0-9_-]/g, '_');
    let dest = path.join(exportsDir, `${profile}-${job.version}.zip`);
    if (existsSync(dest)) dest = path.join(exportsDir, `${profile}-${job.version}-${s.dir}.zip`);
    const zipOut = typeof outputs.zip === 'string' && existsSync(outputs.zip) ? outputs.zip : null;
    let files = null;
    if (zipOut) {
      await fs.mkdir(exportsDir, { recursive: true });
      await fs.copyFile(zipOut, dest);
    } else {
      let folder = s.outDir;
      for (const key of ['package', 'package_dir', 'folder', 'dir', 'sprites_dir', 'output_dir']) {
        const v = outputs[key];
        if (typeof v === 'string' && (v === s.outDir || isInside(s.outDir, path.resolve(v)))) {
          const st = await fs.stat(v).catch(() => null);
          if (st?.isDirectory()) { folder = path.resolve(v); break; }
        }
      }
      const zipped = await zipFolder(folder, dest, { skip: (rel) => rel === 'request.json' || rel === '_views' || rel.startsWith('_views/') });
      files = zipped.files;
    }
    const st = await fs.stat(dest);
    return {
      profile, step: s.step, version: job.version, jobId: job.id, path: dest,
      url: this.fileUrl(job.assetId, dest), bytes: st.size, files, at: now(),
    };
  }

  async _updateVersionFile(job, mutate) {
    if (!job.assetId || !job.version) return;
    const file = path.join(this.assetDir(job.assetId), job.version, 'version.json');
    await this._serial('versionfile:' + file, async () => {
      const vj = await readJsonFile(file, null);
      if (!vj) return;
      await mutate(vj);
      await writeJsonAtomic(file, vj);
    }).catch(() => {});
  }

  async _finishJob(job, status, error = null, finalState = null) {
    // Manifest and version.json are finalised first; the job turns terminal last, so a client
    // that sees "completed" can rely on every file of the run being written.
    const finishedAt = now();
    try {
      for (const s of job.steps || []) {
        if (s.status !== 'queued' && s.status !== 'running') continue;
        const wasRunning = s.status === 'running';
        s.status = status === 'cancelled' ? 'cancelled' : (wasRunning ? 'failed' : 'skipped');
        s.finishedAt = finishedAt;
        const reason = status === 'cancelled' ? 'Cancelled by the user' : (wasRunning ? (error || 'Error') : 'Skipped after an error');
        s.message = reason;
        await this._manifestEntry(job, s, 'failed', { ok: false, error: reason, skipped: !wasRunning, cancelled: status === 'cancelled' });
      }
      if (job.assetId) {
        await this._updateVersionFile(job, (vj) => {
          vj.status = status;
          vj.finishedAt = finishedAt;
          vj.final = status === 'completed' ? finalState : null;
        });
        if (existsSync(this._manifestPath(job.assetId))) {
          await this._updateManifest(job.assetId, async (mm) => {
            const v = (mm.versions || []).find((x) => x.version === job.version);
            if (v) {
              v.status = status;
              v.finishedAt = finishedAt;
              if (status === 'completed') v.final = finalState;
            }
            await this._rebuildPipeline(mm);
          }).catch((err) => console.warn(`[forge] passport ${job.assetId} was not updated after job ${job.id}:`, err.message));
        }
      }
    } catch (err) {
      console.warn(`[forge] failed to update the passport after job ${job.id}:`, err?.message || err);
    }
    job.progress = status === 'completed' ? 100 : this._jobProgress(job);
    if (finalState) job.finalState = finalState;
    job.error = status === 'completed' ? null : (error || null);
    job.message = status === 'completed' ? 'Done' : status === 'cancelled' ? 'Cancelled' : (error || 'Error');
    job.finishedAt = finishedAt;
    job.status = status;
    await this._writeJob(job, true); // never rejects (a failed write is logged and retried)
    if (job.cleanupDir) await this._cleanupStaged(job.cleanupDir);
    const waiters = this.waiters.get(job.id) || [];
    this.waiters.delete(job.id);
    for (const w of waiters) {
      try { w(job); } catch { /* a waiter must not break the others */ }
    }
  }

  // ---------------------------------------------------------------- job records

  _jobFile(id) {
    return path.join(this.jobsDir, id + '.json');
  }

  /**
   * Persist a job (serialised per job; throttled unless force). Never rejects: the in-memory job
   * stays authoritative, a failed write (antivirus / indexer / OneDrive holding the file, a
   * read-only folder) is logged and retried with back-off, and the next write retries anyway.
   */
  _writeJob(job, force = false) {
    job.updatedAt = now();
    if (!force && this.persistIntervalMs > 0) {
      if (this.persistTimers.has(job.id)) return Promise.resolve();
      const timer = setTimeout(() => {
        this.persistTimers.delete(job.id);
        void this._writeJob(job, true);
      }, this.persistIntervalMs);
      timer.unref?.();
      this.persistTimers.set(job.id, timer);
      return Promise.resolve();
    }
    const pending = this.persistTimers.get(job.id);
    if (pending) { clearTimeout(pending); this.persistTimers.delete(job.id); }
    if (this.owned.has(job.id)) job.heartbeatAt = job.updatedAt;
    let snapshot;
    try {
      snapshot = JSON.parse(JSON.stringify(job));
    } catch (err) {
      this._jobWriteFailed(job, err);
      return Promise.resolve();
    }
    delete snapshot.external;
    const file = this._jobFile(job.id);
    const next = (this.jobWrites.get(job.id) || Promise.resolve())
      .then(() => this._writeJobFile(file, snapshot))
      .then(() => this._jobWriteOk(job), (err) => this._jobWriteFailed(job, err));
    this.jobWrites.set(job.id, next);
    return next;
  }

  /** The actual file write (a separate method so tests can make it fail). */
  _writeJobFile(file, value) {
    return writeJsonAtomic(file, value);
  }

  _jobWriteOk(job) {
    const retry = this.retryTimers.get(job.id);
    if (retry) { clearTimeout(retry); this.retryTimers.delete(job.id); }
    if (this.writeFailures.has(job.id)) {
      this.writeFailures.delete(job.id);
      console.warn(`[forge] writing job ${job.id} works again.`);
    }
  }

  _jobWriteFailed(job, err) {
    const count = (this.writeFailures.get(job.id) || 0) + 1;
    this.writeFailures.set(job.id, count);
    if (count === 1 || count % 10 === 0) {
      console.warn(`[forge] failed to write job ${job.id} (${err?.code || err?.message || err}); the state is kept in memory, the write will be retried.`);
    }
    if (count <= 30 && !this.retryTimers.has(job.id)) {
      const timer = setTimeout(() => {
        this.retryTimers.delete(job.id);
        void this._writeJob(job, true);
      }, Math.min(30000, 500 * 2 ** Math.min(count, 6)));
      timer.unref?.();
      this.retryTimers.set(job.id, timer);
    }
  }

  async getJob(id) {
    if (!isUuid(id)) throw forgeError('Invalid job ID.', 400, 'bad_id');
    const mem = this.jobs.get(id);
    if (mem && (this.owned.has(id) || TERMINAL.includes(mem.status))) return mem;
    // another process's job (or one we have not seen): re-read it and check its owner
    const disk = await readJsonFile(this._jobFile(id), null).catch(() => null);
    if (!disk || !isUuid(disk.id)) {
      if (mem) return mem;
      throw forgeError('Job not found.', 404, 'not_found');
    }
    if (!TERMINAL.includes(disk.status)) await this._adopt(disk);
    this.jobs.set(id, disk);
    return disk;
  }

  /** Load job records written by other processes and settle the ones whose owner died. */
  async _syncJobs() {
    let files = [];
    try { files = (await fs.readdir(this.jobsDir)).filter((f) => f.endsWith('.json') && isUuid(f.slice(0, -5))); } catch { files = []; }
    for (const f of files) {
      const id = f.slice(0, -5);
      const mem = this.jobs.get(id);
      if (mem && (this.owned.has(id) || TERMINAL.includes(mem.status))) continue;
      const j = await readJsonFile(path.join(this.jobsDir, f), null).catch(() => null);
      if (!j || !isUuid(j.id)) continue;
      if (!TERMINAL.includes(j.status)) await this._adopt(j);
      this.jobs.set(j.id, j);
    }
  }

  async listJobs({ limit = 50, assetId = null } = {}) {
    await this._syncJobs();
    return [...this.jobs.values()]
      .filter((j) => !assetId || j.assetId === assetId)
      .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)))
      .slice(0, Math.max(1, Math.min(500, limit)))
      .map((j) => ({
        id: j.id, type: j.type, assetId: j.assetId, version: j.version, batchId: j.batchId || null, status: j.status,
        progress: j.progress ?? 0, message: j.message || '', error: j.error || null,
        createdAt: j.createdAt, startedAt: j.startedAt, finishedAt: j.finishedAt,
        steps: (j.steps || []).map((s) => ({ step: s.step, status: s.status, progress: s.progress })),
      }));
  }

  async cancelJob(id) {
    const job = await this.getJob(id);
    if (TERMINAL.includes(job.status)) return { ok: true, status: job.status };
    if (job.external) throw forgeError('The job is running in another process (CLI --direct); cancel it there.', 409, 'external_job');
    const controller = this.controllers.get(id);
    if (controller) controller.abort();
    else await this._finishJob(job, 'cancelled', 'Cancelled by the user');
    return { ok: true, status: 'cancelling' };
  }

  /** Resolve when the job reaches completed/failed/cancelled. */
  async waitJob(id) {
    const job = await this.getJob(id);
    if (TERMINAL.includes(job.status)) return job;
    if (job.external) {
      // getJob re-reads the record and marks it failed if the owner dies while we wait
      for (;;) {
        await sleep(1000);
        const j = await this.getJob(id).catch(() => null);
        if (!j || TERMINAL.includes(j.status)) return j;
      }
    }
    return new Promise((resolve) => {
      const list = this.waiters.get(id) || [];
      list.push(resolve);
      this.waiters.set(id, list);
    });
  }

  async jobLog(id, index) {
    const job = await this.getJob(id);
    const s = (job.steps || []).find((x) => x.index === Number(index));
    if (!s) throw forgeError('Job step not found.', 404, 'not_found');
    const file = s.logFile || path.join(this.blenderLogDir, job.id, s.logName || `${s.step}.log`);
    if (!isInside(this.blenderLogDir, path.resolve(file))) throw forgeError('The log is outside the log folder.', 400, 'bad_path');
    if (!existsSync(file)) throw forgeError('The step log has not been created yet.', 404, 'not_found');
    return file;
  }

  // ---------------------------------------------------------------- clip library

  async clipIndex() {
    return readClipIndex(this.animationsDir);
  }

  /**
   * Take the clip library lock (see LIBRARY_LOCK_FILE). wait:false -> 409 busy when it is held.
   * Resolves to an async release(); rejects with AbortError when the signal aborts while waiting.
   */
  async _acquireLibraryLock({ signal = null, wait = true, owner = null, onWait = null } = {}) {
    const busy = () => forgeError('The clip library is busy: a clip import or removal is running. Try again when it finishes.', 409, 'busy');
    if (!wait) {
      if (!this.libraryMutex.tryLock()) throw busy();
    } else {
      if (this.libraryMutex.locked) onWait?.(null);
      await this.libraryMutex.lock(signal);
    }
    const token = randomUUID();
    const file = path.join(this.animationsDir, LIBRARY_LOCK_FILE);
    LIVE_LIBRARY_LOCKS.add(token);
    try {
      let told = false;
      for (;;) {
        if (signal?.aborted) throw abortError();
        try {
          await fs.mkdir(this.animationsDir, { recursive: true });
          const fh = await fs.open(file, 'wx');
          try {
            await fh.writeFile(JSON.stringify({ pid: process.pid, host: this.host, token, owner, at: now() }));
          } finally {
            await fh.close();
          }
          break;
        } catch (err) {
          if (err.code !== 'EEXIST') throw err;
        }
        if (!(await this._libraryLockHeld(file))) continue; // a stale lock was removed: try again at once
        if (!wait) throw busy();
        if (!told) { told = true; onWait?.(await readJsonFile(file, null).catch(() => null)); }
        await sleepAbortable(this.libraryLockPollMs, signal);
      }
    } catch (err) {
      LIVE_LIBRARY_LOCKS.delete(token);
      this.libraryMutex.unlock();
      throw err;
    }
    const beat = setInterval(() => { const t = new Date(); fs.utimes(file, t, t).catch(() => {}); }, this.heartbeatMs);
    beat.unref?.();
    let released = false;
    return async () => {
      if (released) return;
      released = true;
      clearInterval(beat);
      try {
        const info = JSON.parse(await fs.readFile(file, 'utf8'));
        if (info?.token === token) await fs.unlink(file);
      } catch { /* already gone */ }
      LIVE_LIBRARY_LOCKS.delete(token);
      this.libraryMutex.unlock();
    };
  }

  /** Is the existing lock file held by a live owner? A stale one is removed (returns false). */
  async _libraryLockHeld(file) {
    let st;
    let info = null;
    try {
      st = await fs.stat(file);
      info = JSON.parse(await fs.readFile(file, 'utf8'));
    } catch (err) {
      if (err.code === 'ENOENT') return false;
      // empty or half-written: its creator is writing it right now unless it is old
    }
    const age = st ? Date.now() - st.mtimeMs : Infinity;
    let alive;
    if (!info || !Number.isInteger(info.pid)) alive = age < this.staleMs;
    else if (info.pid === process.pid) alive = LIVE_LIBRARY_LOCKS.has(info.token); // another engine of this process
    else alive = isPidAlive(info.pid) && age < this.staleMs; // a reused pid stops refreshing the file
    if (alive) return true;
    // take over a stale lock only if it is still the same file (a tiny race with another taker
    // remains: both would have to find it stale within the same few milliseconds)
    const again = await readJsonFile(file, null).catch(() => null);
    if (again && info && again.token !== info.token) return true;
    await fs.unlink(file).catch(() => {});
    return false;
  }

  /** Is a clip-library job of this or another live process unfinished? */
  async _clipJobRunning() {
    await this._syncJobs();
    return [...this.jobs.values()].some((j) => j.type === 'clips' && !TERMINAL.includes(j.status));
  }

  /**
   * Import FBX clips via the clip_library step. One import at a time (they all rewrite index.json).
   * @param {{files?:string[], folder?:string, stagedDir?:string}} src absolute paths (validated
   *   here); stagedDir = a runtime/forge/uploads/<id> folder of HTTP uploads, removed when the job ends
   */
  async importClips({ files = [], folder = null, stagedDir = null } = {}) {
    return this._serial('clips-import', async () => {
      if (await this._clipJobRunning()) throw forgeError('A clip import is already running. Wait for it to finish and try again.', 409, 'busy');
      const list = [...files];
      if (folder) {
        const st = await fs.stat(folder).catch(() => null);
        if (!st?.isDirectory()) throw forgeError(`Folder not found: ${folder}`, 404, 'not_found');
        for (const name of (await fs.readdir(folder)).sort()) {
          if (name.toLowerCase().endsWith('.fbx')) list.push(path.join(folder, name));
        }
      }
      if (!list.length) throw forgeError('No FBX files to import.', 400, 'no_files');
      if (list.length > MAX_CLIP_FILES) throw forgeError(`At most ${MAX_CLIP_FILES} clips can be imported at a time.`, 400, 'too_many_files');
      for (const f of list) {
        if (!path.isAbsolute(f)) throw forgeError(`An absolute path is required: ${f}`, 400, 'bad_path');
        const fd = await fs.open(f, 'r').catch(() => null);
        if (!fd) throw forgeError(`File not found: ${f}`, 404, 'not_found');
        const head = Buffer.alloc(2048);
        try { await fd.read(head, 0, 2048, 0); } finally { await fd.close(); }
        if (!isFbxBuffer(head)) throw forgeError(`The file does not look like an FBX: ${path.basename(f)}`, 400, 'bad_signature');
      }
      const uploads = path.join(this.workDir, 'uploads');
      const cleanupDir = stagedDir && isInside(uploads, path.resolve(stagedDir)) ? path.resolve(stagedDir) : null;
      return this._startLibraryJob({ action: 'import', files: list, library: this.animationsDir }, { cleanupDir });
    });
  }

  async _startLibraryJob(params, { cleanupDir = null } = {}) {
    const { steps } = normalizeSteps([{ step: 'clip_library', params }]);
    const jobId = randomUUID();
    const at = now();
    const dir = stepDirName(1, 'clip_library');
    const job = {
      id: jobId, type: 'clips', assetId: null, version: null, batchId: null, status: 'queued', progress: 0, message: 'Waiting in the queue',
      from: null, base: null, baseState: {}, createdAt: at, updatedAt: at, startedAt: null, finishedAt: null,
      host: this.host, pid: process.pid, warnings: [], error: null, cleanupDir,
      steps: [{
        index: 1, step: 'clip_library', params: steps[0].params, dir, outDir: path.join(this.workDir, 'jobs', jobId, dir), logName: 'clip_library.log',
        status: 'queued', progress: 0, message: '', startedAt: null, finishedAt: null, result: null, logTail: [], logFile: null, warnings: [],
      }],
    };
    this.jobs.set(jobId, job);
    this.owned.add(jobId);
    await this._writeJob(job, true);
    this._enqueue(job);
    return { jobId };
  }

  /** DELETE /clips/:id — drop from index.json and move its files into trash. */
  async removeClip(id) {
    if (!isClipId(id)) throw forgeError('Invalid clip ID.', 400, 'bad_id');
    if (await this._clipJobRunning()) throw forgeError('A clip import is running. Try again when it finishes.', 409, 'busy');
    // the library lock closes the gap between that check and the index rewrite (and covers jobs
    // of other processes whose record we have not seen yet)
    const release = await this._acquireLibraryLock({ wait: false, owner: `remove:${id}` });
    try {
      return await this._removeClipLocked(id);
    } finally {
      await release();
    }
  }

  async _removeClipLocked(id) {
    return this._serial('clips', async () => {
      const index = await this.clipIndex();
      const clip = index.clips.find((c) => c?.id === id);
      if (!clip) throw forgeError('Clip not found in the library.', 404, 'not_found');
      const trash = path.join(this.trashDir, 'clips', `${id}-${now().replace(/[:.]/g, '-')}`);
      const moved = [];
      for (const rel of [clip.file, clip.poses, clip.preview].filter((x) => typeof x === 'string')) {
        const abs = path.resolve(this.animationsDir, rel);
        if (!isInside(this.animationsDir, abs) || !existsSync(abs)) continue;
        await moveEntry(abs, path.join(trash, path.basename(abs)));
        moved.push(rel);
      }
      const poses = await this.clipPosesPath(id, index).catch(() => null);
      if (poses && existsSync(poses)) {
        await moveEntry(poses, path.join(trash, path.basename(poses)));
        moved.push(path.relative(this.animationsDir, poses));
      }
      await writeJsonAtomic(path.join(trash, 'clip.json'), clip);
      index.clips = index.clips.filter((c) => c?.id !== id);
      await writeJsonAtomic(path.join(this.animationsDir, 'index.json'), index);
      return { ok: true, id, moved, trash };
    });
  }

  /** Absolute path of <id>.poses.json for the stick-figure preview. */
  async clipPosesPath(id, index = null) {
    if (!isClipId(id)) throw forgeError('Invalid clip ID.', 400, 'bad_id');
    const idx = index || await this.clipIndex();
    const clip = idx.clips.find((c) => c?.id === id);
    const candidates = [];
    if (clip?.poses) candidates.push(path.resolve(this.animationsDir, clip.poses));
    if (clip?.file) candidates.push(path.join(path.dirname(path.resolve(this.animationsDir, clip.file)), `${id}.poses.json`));
    candidates.push(path.join(this.animationsDir, `${id}.poses.json`), path.join(this.animationsDir, 'poses', `${id}.poses.json`));
    for (const c of candidates) {
      if (isInside(this.animationsDir, c) && existsSync(c)) return c;
    }
    throw forgeError('The clip poses have not been built yet.', 404, 'not_found');
  }

  // ---------------------------------------------------------------- batches

  /**
   * POST /batch: create assets and start their pipelines (one job each, run one after another).
   * @param {{items:Array<{name?, kind, source, preset?, steps?, profile?, from?}>}} body
   * @param {Array<{buffer?:Buffer, path?:string, originalName?:string}>} files for source {type:'upload', fileIndex}
   */
  async startBatch(body, files = []) {
    const items = body?.items;
    if (!Array.isArray(items) || !items.length) throw forgeError('A non-empty items list is required.', 400, 'no_items');
    if (items.length > 200) throw forgeError('A batch can have at most 200 items.', 400, 'too_many_items');
    const batchId = randomUUID();
    const record = { id: batchId, createdAt: now(), items: [] };
    for (const [i, item] of items.entries()) {
      const out = { index: i, name: item?.name || null, kind: item?.kind || null, assetId: null, jobId: null, version: null, error: null };
      try {
        if (!item || typeof item !== 'object') throw forgeError('A batch item must be an object.', 400, 'bad_item');
        let file = null;
        if (item.source?.type === 'upload') {
          const fi = Number(item.source.fileIndex ?? 0);
          file = files[fi] || (item.source.path && this.allowLocalPaths ? { path: item.source.path } : null);
          if (!file) throw forgeError(`Item #${i + 1}: no file to upload was provided.`, 400, 'no_file');
        }
        const m = await this.createAsset({ name: item.name, kind: item.kind, source: item.source || { type: 'params' }, profile: item.profile }, file);
        out.assetId = m.id;
        out.name = m.name;
        if (item.preset || item.steps) {
          const run = await this.startRun(m.id, { preset: item.preset, steps: item.steps, from: 'source' }, { batchId });
          out.jobId = run.jobId;
          out.version = run.version;
        }
      } catch (err) {
        out.error = err.message;
      }
      record.items.push(out);
    }
    await writeJsonAtomic(path.join(this.batchesDir, batchId + '.json'), record);
    return { batchId, items: record.items.map(({ assetId, jobId, version, error, name }) => ({ assetId, jobId, version, name, error })) };
  }

  async getBatch(id) {
    if (!isUuid(id)) throw forgeError('Invalid batch ID.', 400, 'bad_id');
    const record = await readJsonFile(path.join(this.batchesDir, id + '.json'), null);
    if (!record) throw forgeError('Batch not found.', 404, 'not_found');
    const counts = { total: record.items.length, queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0, created: 0 };
    const items = [];
    for (const it of record.items) {
      let status = it.error ? 'failed' : it.jobId ? 'queued' : 'created';
      let progress = 0;
      let error = it.error;
      if (it.jobId) {
        const job = await this.getJob(it.jobId).catch(() => null);
        if (job) { status = job.status; progress = job.progress ?? 0; error = job.error || error; }
      }
      counts[status] = (counts[status] || 0) + 1;
      items.push({ ...it, status, progress, error });
    }
    const status = counts.running || counts.queued ? 'running' : counts.failed || counts.cancelled ? (counts.completed ? 'partial' : 'failed') : 'completed';
    return { id: record.id, createdAt: record.createdAt, status, counts, items };
  }

  // ---------------------------------------------------------------- files

  /**
   * Absolute path for GET /assets/:id/files/<version>/<stepDir>/<file> with traversal guards.
   * @param {string} id
   * @param {string[]} segments  e.g. ['v001', '01_cleanup', 'clean.glb'] | ['exports', 'godot-v002.zip'] | ['source', 'source.glb']
   */
  async resolveAssetFile(id, segments) {
    const dir = this.assetDir(id);
    if (!Array.isArray(segments) || segments.length < 2 || segments.length > 12) throw forgeError('Invalid file path.', 400, 'bad_path');
    for (const seg of segments) {
      if (typeof seg !== 'string' || seg === '.' || seg === '..' || seg.includes('..') || !SAFE_SEGMENT.test(seg)) {
        throw forgeError('Invalid file path.', 400, 'bad_path');
      }
    }
    const [first] = segments;
    if (!(parseVersion(first) !== null || first === 'source' || first === 'exports')) throw forgeError('Invalid file path.', 400, 'bad_path');
    const ext = fileExt(segments[segments.length - 1]);
    if (!FILE_EXTENSIONS.includes(ext)) throw forgeError('This file type is not served.', 403, 'bad_extension');
    const abs = path.resolve(dir, ...segments);
    if (!isInside(dir, abs)) throw forgeError('Invalid file path.', 400, 'bad_path');
    let real;
    try {
      real = await fs.realpath(abs);
    } catch {
      throw forgeError('File not found.', 404, 'not_found');
    }
    const realDir = await fs.realpath(dir).catch(() => dir);
    if (!isInside(realDir, real)) throw forgeError('Invalid file path.', 400, 'bad_path');
    const st = await fs.stat(real);
    if (!st.isFile()) throw forgeError('File not found.', 404, 'not_found');
    return { path: real, ext, name: path.basename(real), size: st.size, download: DOWNLOAD_EXTENSIONS.includes(ext) };
  }

  // ---------------------------------------------------------------- status

  async status() {
    const [blender, godot] = await Promise.all([
      Promise.resolve(this.blenderInfo()).catch((e) => ({ ok: false, error: e.message })),
      Promise.resolve(this.godotInfo()).catch((e) => ({ ok: false, error: e.message })),
    ]);
    let clips = 0;
    try { clips = (await this.clipIndex()).clips.length; } catch { clips = 0; }
    let assets = 0;
    try { assets = (await fs.readdir(this.assetsDir)).filter(isUuid).length; } catch { assets = 0; }
    return {
      blender,
      godot,
      library: { clips, assets, path: this.libraryDir },
      queue: {
        running: this.blenderQueue.running,
        queued: this.blenderQueue.queued + this.jobQueue.queued,
        blender: this.blenderQueue.stats(),
        jobs: this.jobQueue.stats(),
      },
      gpuLock: gpuLockState(),
    };
  }
}
