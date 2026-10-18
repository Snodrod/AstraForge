// GPU hand-over between the two 3D engines (Hunyuan ComfyUI :8190 and Pixal3D ComfyUI :8191, docs/forge/pixal3d.md).
//
// Pixal3D peaks at ~9.5 GB of the 12 GB card, Hunyuan keeps its models cached, so they can not both sit in VRAM.
// Before a generation on engine X the studio (under the shared GPU mutex, see gpu-lock.mjs):
//   1. looks at the OTHER engine's queue: while something runs or waits there it WAITS (a running job is never interrupted);
//   2. when that queue is empty it asks the other engine to unload its models (POST /free) and waits until the memory is back.
// An engine that does not answer (not started, not installed) is simply skipped.
import { acquireGpuLock } from './gpu-lock.mjs';
import { pixal3dConfig } from './pixal3d.mjs';

export const ENGINE_LABELS = { hunyuan: 'Hunyuan3D', pixal3d: 'Pixal3D' };
export const DEFAULT_HUNYUAN_URL = 'http://127.0.0.1:8190';

const sleepMs = (ms, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(new DOMException('Cancelled', 'AbortError'));
  const onAbort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
  const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
  signal?.addEventListener('abort', onAbort, { once: true });
});

/** URLs of both engines. hunyuan: explicit option, else ASTRA_COMFY_URL, else 8190; pixal3d: from pixal3dConfig(). */
export function engineUrls(urls = {}, env = process.env) {
  let pixal3d;
  try { pixal3d = pixal3dConfig({ env }).url; } catch { pixal3d = 'http://127.0.0.1:8191'; }
  return { hunyuan: env.ASTRA_COMFY_URL || DEFAULT_HUNYUAN_URL, pixal3d, ...urls };
}

async function json(fetchFn, url, options = {}) {
  const res = await fetchFn(url, { ...options, signal: options.signal || AbortSignal.timeout(4000) });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

/** ComfyUI queue of an engine: {running, pending} or null when it does not answer like a ComfyUI. */
export async function engineQueue(url, fetchFn = fetch) {
  try {
    const q = await json(fetchFn, url + '/queue', { signal: AbortSignal.timeout(2500) });
    if (!Array.isArray(q?.queue_running) || !Array.isArray(q?.queue_pending)) return null;
    return { running: q.queue_running.length, pending: q.queue_pending.length };
  } catch { return null; }
}

/** Torch VRAM currently reserved by an engine's own process (bytes), null when unknown. */
async function torchVram(url, fetchFn) {
  try {
    const stats = await json(fetchFn, url + '/system_stats', { signal: AbortSignal.timeout(2500) });
    const d = stats?.devices?.[0];
    return Number.isFinite(d?.torch_vram_total) ? d.torch_vram_total : null;
  } catch { return null; }
}

/**
 * Make room for `target` by unloading the other engine. Resolves with a report, throws only on abort or after `maxWaitMs`.
 * @param {'hunyuan'|'pixal3d'} target engine that is about to run
 * @param {object} [o] urls, signal, onWait(text) (called once when waiting starts), onStep(text), fetchFn, sleep, pollMs, maxWaitMs, settleMs
 * @returns {Promise<{waited:boolean, freed:string[], skipped:string[]}>}
 */
export async function handoverTo(target, o = {}) {
  const { signal, onWait, onStep, fetchFn = fetch, sleep = sleepMs, pollMs = 2000, maxWaitMs = 60 * 60 * 1000, settleMs = 8000 } = o;
  const urls = engineUrls(o.urls);
  const report = { waited: false, freed: [], skipped: [] };
  for (const other of Object.keys(ENGINE_LABELS).filter((name) => name !== target)) {
    const url = urls[other];
    if (!url || url === urls[target]) { report.skipped.push(other); continue; }
    let queue = await engineQueue(url, fetchFn);
    if (!queue) { report.skipped.push(other); continue; }
    const started = Date.now();
    while (queue && (queue.running || queue.pending)) {
      if (!report.waited) {
        report.waited = true;
        onWait?.(`Waiting for ${ENGINE_LABELS[other]} to finish its job (not interrupting it)`);
      }
      if (Date.now() - started > maxWaitMs) throw new Error(`${ENGINE_LABELS[other]} has been busy too long (over ${Math.round(maxWaitMs / 60000)} min). Wait for its job to finish and try again.`);
      await sleep(pollMs, signal);
      signal?.throwIfAborted();
      queue = await engineQueue(url, fetchFn);
    }
    if (!queue) { report.skipped.push(other); continue; }   // it went away while we waited
    onStep?.(`Freeing VRAM: unloading ${ENGINE_LABELS[other]}`);
    const before = await torchVram(url, fetchFn);
    try {
      await fetchFn(url + '/free', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ unload_models: true, free_memory: true }), signal: AbortSignal.timeout(5000) });
    } catch { report.skipped.push(other); continue; }
    report.freed.push(other);
    // ComfyUI runs /free on its worker loop: wait until its torch memory drops (bounded, best effort)
    if (before && before > 512 * 1024 * 1024) {
      const t0 = Date.now();
      for (;;) {
        await sleep(500, signal);
        const now = await torchVram(url, fetchFn);
        if (now === null || now < before * 0.5 || Date.now() - t0 > settleMs) break;
      }
    }
  }
  return report;
}

/**
 * The studio's GPU mutex (cross-process, gpu-lock.mjs) plus the hand-over to `engine`. Same result as acquireGpuLock: call release().
 * @param {'hunyuan'|'pixal3d'} engine
 * @param {object} [opts] everything acquireGpuLock takes, plus urls, onStep, fetchFn, sleep, acquire (tests), pollMs, maxWaitMs, settleMs
 */
export async function acquireEngineGpu(engine, opts = {}) {
  const { acquire = acquireGpuLock, urls, onStep, fetchFn, sleep, pollMs, maxWaitMs, settleMs, ...lockOpts } = opts;
  const lock = await acquire(lockOpts);
  try {
    lock.handover = await handoverTo(engine, { urls, signal: lockOpts.signal, onWait: onStep, onStep, fetchFn, sleep, pollMs, maxWaitMs, settleMs });
    return lock;
  } catch (error) {
    await lock.release();
    throw error;
  }
}

/** Run fn under the GPU mutex after the hand-over. */
export async function withEngineGpu(engine, fn, opts = {}) {
  const lock = await acquireEngineGpu(engine, opts);
  try { return await fn(lock); } finally { await lock.release(); }
}

/** Best effort: unload one engine's models when it is idle (used after a Pixal3D job so other GPU work gets the memory). */
export async function freeIfIdle(url, fetchFn = fetch) {
  const queue = await engineQueue(url, fetchFn);
  if (!queue || queue.running || queue.pending) return false;
  try {
    await fetchFn(url + '/free', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ unload_models: true, free_memory: true }), signal: AbortSignal.timeout(5000) });
    return true;
  } catch { return false; }
}
