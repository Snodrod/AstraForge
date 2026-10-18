// Forge API client. Every /api/forge endpoint path used by the wizard lives in this file
// (docs/forge/API.md). Requests carry the same x-astra-client: studio header as src/api.js.
import { api, upload } from '../api.js';
import { assetRelPath } from './forge-core.js';
import { t } from '../i18n/index.js';

export const FORGE_BASE = '/api/forge';

/** Message of the "forge module not installed on the server" error (current language). */
export const notReadyMessage = () => t('forge.api.notReady');
const networkMessage = () => t('forge.api.network');

export class ForgeApiError extends Error {
  constructor(message, { status = 0, code = 'error' } = {}) {
    super(message);
    this.name = 'ForgeApiError';
    this.status = status;
    this.code = code;
  }

  get notReady() {
    return this.code === 'not_ready';
  }
}

const enc = encodeURIComponent;

/** All Forge endpoint paths (relative to FORGE_BASE). */
export const paths = {
  status: () => '/status',
  steps: () => '/steps',
  presets: () => '/presets',
  assets: () => '/assets',
  asset: (id) => `/assets/${enc(id)}`,
  assetRun: (id) => `/assets/${enc(id)}/run`,
  assetExport: (id) => `/assets/${enc(id)}/export`,
  assetFile: (id, version, stepDir, file) =>
    `/assets/${enc(id)}/files/${enc(version)}/${enc(stepDir)}/${String(file).split('/').map(enc).join('/')}`,
  // Same route for exports/… and source/… (server: resolveAssetFile).
  assetFileRel: (id, rel) => `/assets/${enc(id)}/files/${String(rel).split('/').map(enc).join('/')}`,
  jobs: () => '/jobs',
  job: (id) => `/jobs/${enc(id)}`,
  jobCancel: (id) => `/jobs/${enc(id)}/cancel`,
  clips: () => '/clips',
  clip: (id) => `/clips/${enc(id)}`,
  clipPoses: (id) => `/clips/${enc(id)}/poses`,
  batch: () => '/batch',
  batchOne: (id) => `/batch/${enc(id)}`,
  review: (batch) => '/review' + (batch ? `?batch=${enc(batch)}` : ''),
  reviewExport: () => '/review/export',
};

async function request(path, { method = 'GET', body, headers } = {}) {
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData;
  let res;
  try {
    res = await fetch(FORGE_BASE + path, {
      method,
      body: body === undefined ? undefined : isForm || typeof body === 'string' ? body : JSON.stringify(body),
      headers: {
        'x-astra-client': 'studio',
        ...(body !== undefined && !isForm ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
    });
  } catch {
    throw new ForgeApiError(networkMessage(), { code: 'network' });
  }
  const type = res.headers.get('content-type') || '';
  if (!type.includes('json')) {
    // Without the forge module Express answers GET with the SPA index.html and other
    // methods with an HTML 404 page: both mean "server module not installed yet".
    if (res.status === 404 || res.status === 405 || type.includes('text/html') || res.ok) {
      throw new ForgeApiError(notReadyMessage(), { status: res.status, code: 'not_ready' });
    }
    throw new ForgeApiError(t('forge.api.httpError', { status: res.status }), { status: res.status, code: 'http' });
  }
  let value = null;
  try {
    value = await res.json();
  } catch {
    throw new ForgeApiError(t('forge.api.badJson'), { status: res.status, code: 'bad_json' });
  }
  if (!res.ok) {
    throw new ForgeApiError(value?.error || t('forge.api.httpError', { status: res.status }), {
      status: res.status,
      code: value?.code || (res.status === 404 ? 'not_found' : 'http'),
    });
  }
  return value;
}

// ---------------------------------------------------------------- status & registries
export const getStatus = () => request(paths.status());
export const getSteps = () => request(paths.steps());
export const getPresets = () => request(paths.presets());

// ---------------------------------------------------------------- assets (passports)
export const listAssets = () => request(paths.assets());
export const getAsset = (id) => request(paths.asset(id));
export const createAsset = ({ name, kind, source, profile }) =>
  request(paths.assets(), { method: 'POST', body: { name, kind, source, ...(profile ? { profile } : {}) } });
export const patchAsset = (id, patch) => request(paths.asset(id), { method: 'PATCH', body: patch });
export const deleteAsset = (id) => request(paths.asset(id), { method: 'DELETE' });
/** from: 'source' | 'latest' | 'vNNN' (server default: 'source'). */
export const runSteps = (id, steps, from) =>
  request(paths.assetRun(id), { method: 'POST', body: { steps, ...(from ? { from } : {}) } });
export const exportAsset = (id, profile, params = {}) =>
  request(paths.assetExport(id), { method: 'POST', body: { profile, params } });

/**
 * Passport from an uploaded GLB, FBX or OBJ (also a GLB exported from the scene). API.md lists
 * the source type 'upload' without a transport; docs/forge/server.md documents the multipart
 * form (file + name/kind/source JSON), up to 300 MB.
 */
export function createAssetFromFile(file, { name, kind }) {
  const body = new FormData();
  body.append('file', file, file.name);
  body.append('name', name);
  body.append('kind', kind);
  body.append('source', JSON.stringify({ type: 'upload', file: file.name }));
  return request(paths.assets(), { method: 'POST', body });
}

export function assetFileUrl(assetId, version, stepDir, file) {
  return FORGE_BASE + paths.assetFile(assetId, version, stepDir, file);
}

/** URL for an absolute output path of a step (null when it is outside the asset folder). */
export function outputUrl(assetId, absPath) {
  if (typeof absPath === 'string' && absPath.startsWith(FORGE_BASE + '/assets/')) return absPath;
  const rel = assetRelPath(absPath, assetId);
  return rel ? FORGE_BASE + paths.assetFileRel(assetId, rel) : null;
}

/** Result files are read with plain GET (no state change). */
export async function fetchJsonFile(url) {
  let res;
  try {
    res = await fetch(url);
  } catch {
    throw new ForgeApiError(networkMessage(), { code: 'network' });
  }
  if (!res.ok) throw new ForgeApiError(t('forge.api.fileMissing'), { status: res.status, code: 'not_found' });
  const type = res.headers.get('content-type') || '';
  if (type.includes('text/html')) throw new ForgeApiError(notReadyMessage(), { status: res.status, code: 'not_ready' });
  try {
    return await res.json();
  } catch {
    throw new ForgeApiError(t('forge.api.fileBroken'), { status: res.status, code: 'bad_json' });
  }
}

// ---------------------------------------------------------------- jobs
export const listJobs = () => request(paths.jobs());
export const getJob = (id) => request(paths.job(id));
export const cancelJob = (id) => request(paths.jobCancel(id), { method: 'POST' });

// ---------------------------------------------------------------- clip library
export const getClips = () => request(paths.clips());
export const getClipPoses = (id) => request(paths.clipPoses(id));
export const deleteClip = (id) => request(paths.clip(id), { method: 'DELETE' });

export function uploadClips(files) {
  const body = new FormData();
  for (const f of files) body.append('files[]', f, f.name);
  return request(paths.clips(), { method: 'POST', body });
}

// ---------------------------------------------------------------- batches
export const createBatch = (items) => request(paths.batch(), { method: 'POST', body: { items } });
export const getBatch = (id) => request(paths.batchOne(id));

/** The server takes at most this many files per multipart POST /batch (multer limit). */
export const BATCH_FILES_MAX = 50;

/**
 * Batch with uploaded files (docs/forge/server.md: multipart `items` JSON + `files[]`; an item
 * refers to its file by source {type:'upload', fileIndex}). Files go straight into the forge,
 * up to 300 MB each, without a copy in the studio assets.
 */
export function createBatchWithFiles(items, files) {
  const body = new FormData();
  body.append('items', JSON.stringify(items));
  for (const f of files) body.append('files[]', f, f.name);
  return request(paths.batch(), { method: 'POST', body });
}

// ---------------------------------------------------------------- review (docs/forge/REVIEW.md)
/** Rows of the batch: status, previews, tear numbers and the stored decisions. */
export const getReview = (batch) => request(paths.review(batch));
/** Writes review_decisions.json next to the batch state file; returns {path, count}. */
export const exportReview = (batch) => request(paths.reviewExport(), { method: 'POST', body: batch ? { batch } : {} });

// ---------------------------------------------------------------- existing studio endpoints
/** Completed generation jobs (existing GET /api/jobs). */
export async function listGenerationJobs() {
  const jobs = await api('/jobs');
  return (Array.isArray(jobs) ? jobs : []).filter((j) => j.status === 'completed' && j.asset?.url);
}

/**
 * Existing studio upload (POST /api/assets, 100 MB). Only used for STL, which the forge upload
 * does not accept; GLB/FBX/OBJ go to the forge directly (createAssetFromFile).
 */
export const uploadStudioFile = (file) => upload(file);
