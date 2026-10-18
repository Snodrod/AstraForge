// One Pixal3D shape generation: images -> ComfyUI-Pixal3D (127.0.0.1:8191) -> normalised GLB (docs/forge/pixal3d.md).
// Used by the studio job (server/jobs.mjs) and by the command line (scripts/views/views_3d.mjs --engine pixal3d).
//   1. start the Pixal3D ComfyUI if it is not running, check that its nodes are loaded;
//   2. upload the RGBA cut-outs, take the GPU mutex and hand the card over from Hunyuan (gpu-handover.mjs);
//   3. queue pixal3dWorkflow (shape only), follow the progress over the websocket, poll /history for the end;
//   4. download the GLB, check it, centre it and scale it to height 2 like the Hunyuan seeds (glb-normalize.mjs).
import WebSocket from 'ws';
import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { pixal3dWorkflow } from './schema.mjs';
import { pixal3dConfig, startPixal3dRuntime } from './pixal3d.mjs';
import { acquireEngineGpu, freeIfIdle, engineQueue } from './gpu-handover.mjs';
import { validateGeneratedMesh, maxMeshBytes } from './mesh-validation.mjs';
import { normalizeGlb } from './glb-normalize.mjs';

/**
 * node class -> [percent from, percent to, label]. The ranges follow the measured time shares of a four-view run (243 s on the militia:
 * model load + DINOv3 8 s, coarse shape 68 s, 512 shape 46 s, 1024 shape 86 s, decode 6 s, quad remesh 15 s, holes + simplify 8 s).
 */
export const PIXAL3D_PHASES = {
  Trellis2FovMoGeCameraConfig: [0, 1, 'Setting up the view cameras'],
  Trellis2Pixal3DMultiViewConfig: [0, 1, 'Setting up the view cameras'],
  Trellis2LoadModel: [1, 3, 'Loading the Pixal3D model'],
  Trellis2PreProcessImage: [3, 4, 'Preparing the image'],
  Trellis2ImageCondGenerator: [4, 6, 'Reading the images · DINOv3'],
  Trellis2SparseGenerator: [6, 33, 'Building the coarse shape'],
  Trellis2ShapeGenerator: [33, 52, 'Building the shape · 512'],
  Trellis2ShapeCascadeGenerator: [52, 86, 'Refining the shape · 1024'],
  Trellis2DecodeLatents: [86, 89, 'Decoding the mesh'],
  Trellis2FillHolesWithCuMesh: [89, 90, 'Closing holes'],
  Trellis2ReconstructMeshWithQuad: [90, 94, 'Rebuilding the surface'],
  Trellis2SimplifyMesh: [94, 95, 'Simplifying the mesh'],
  Trellis2FillHolesNicelyWithMeshlib: [95, 97, 'Closing holes · meshlib'],
  Trellis2MeshWithVoxelToTrimesh: [97, 98, 'Assembling the mesh'],
  Trellis2ExportMesh: [98, 98, 'Saving GLB'],
};

/** Known failures of the Pixal3D ComfyUI in plain Russian; anything else is passed on (shortened). */
export function explainPixal3dError(text) {
  const t = String(text || '');
  if (/out of memory|CUDA error: out of memory|OutOfMemory/i.test(t)) return 'Pixal3D ran out of VRAM (about 10 GB of 12 are needed). Close programs that hold the GPU and try again.';
  if (/dinov3/i.test(t) && /(not found|No such file|\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d|Exception)/i.test(t)) return 'The DINOv3 weights for Pixal3D were not found. See ASTRAFORGE_README.md in the ComfyUI-Pixal3D folder.';
  if (/Interrupted|InterruptProcessingException|processing interrupted/i.test(t)) return 'Generation aborted';
  return t.slice(0, 900) || 'Pixal3D could not build the geometry. See data/runtime/pixal3d.log.';
}

async function comfyJson(url, options = {}) {
  const res = await fetch(url, { ...options, signal: options.signal || AbortSignal.timeout(15000) });
  let body;
  try { body = await res.json(); } catch { throw new Error('The Pixal3D engine returned an invalid response.'); }
  if (!res.ok) throw new Error(body.error?.message || JSON.stringify(body.node_errors || body.error || body).slice(0, 700));
  return body;
}

const pause = (ms, signal) => new Promise((resolve, reject) => {
  const onAbort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
  const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
  if (signal?.aborted) return onAbort();
  signal?.addEventListener('abort', onAbort, { once: true });
});

async function uploadImage(url, file, name, signal) {
  const form = new FormData();
  form.append('image', new Blob([await fs.readFile(file)]), name);
  form.append('overwrite', 'true');
  return (await comfyJson(url + '/upload/image', { method: 'POST', body: form, signal })).name;
}

async function downloadGlb(url, item, signal) {
  const params = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || '', type: item.type || 'output' });
  const res = await fetch(url + '/view?' + params, { signal: signal || AbortSignal.timeout(120000) });
  if (!res.ok) throw new Error('Failed to get the Pixal3D result.');
  const buffer = Buffer.from(await res.arrayBuffer());
  if (buffer.length > maxMeshBytes) throw new Error('The Pixal3D result exceeds 100 MB.');
  return buffer;
}

/** Stop our prompt: interrupt it if it is the running one, otherwise take it out of the queue. */
async function cancelPrompt(url, promptId) {
  try {
    const queue = await comfyJson(url + '/queue', { signal: AbortSignal.timeout(4000) });
    const body = { 'Content-Type': 'application/json' };
    if (queue.queue_running?.some((item) => item[1] === promptId)) await fetch(url + '/interrupt', { method: 'POST', headers: body, body: '{}', signal: AbortSignal.timeout(4000) });
    else await fetch(url + '/queue', { method: 'POST', headers: body, body: JSON.stringify({ delete: [promptId] }), signal: AbortSignal.timeout(4000) });
  } catch { /* the engine may be gone already */ }
}

/** Progress follower: websocket events -> onProgress({percent, message}). Never throws; progress is optional. */
function followProgress({ url, clientId, workflow, onProgress, getPromptId, WebSocketImpl, tickMs = 2000 }) {
  let phase = null, last = -1, phaseAt = 0, phaseSteps = false;
  const emit = (percent, message) => {
    percent = Math.max(0, Math.min(98, Math.round(percent)));
    if (percent < last) percent = last;
    last = percent;
    onProgress?.({ percent, message });
  };
  let socket;
  try { socket = new WebSocketImpl(url.replace('http:', 'ws:') + '/ws?clientId=' + clientId); } catch { return { close() {}, ready: Promise.resolve() }; }
  socket.on('error', () => {});
  const ready = new Promise((resolve) => { socket.on('open', resolve); socket.on('close', resolve); setTimeout(resolve, 2500).unref?.(); });
  socket.on('message', (raw) => {
    try {
      const v = JSON.parse(raw);
      const promptId = getPromptId();
      if (v.data?.prompt_id && promptId && v.data.prompt_id !== promptId) return;
      if (v.type === 'executing' && v.data?.node) {
        const next = PIXAL3D_PHASES[workflow[v.data.node]?.class_type];
        if (next && next !== phase) { phase = next; phaseAt = Date.now(); phaseSteps = false; }
        if (phase) emit(phase[0], phase[2]);
      } else if (v.type === 'progress' && phase && v.data?.max > 0) {
        phaseSteps = true;
        emit(phase[0] + (phase[1] - phase[0]) * (v.data.value / v.data.max), `${phase[2]} · ${v.data.value}/${v.data.max}`);
      }
    } catch { /* not a progress frame */ }
  });
  // The Trellis2 nodes report no steps over the websocket: show how long the phase has been running and creep towards its end
  // (never past it), so the bar and the text visibly move during the long phases.
  const ticker = setInterval(() => {
    if (!phase || phaseSteps) return;
    const seconds = Math.round((Date.now() - phaseAt) / 1000);
    emit(phase[0] + (phase[1] - phase[0]) * 0.9 * (1 - Math.exp(-seconds / 45)), `${phase[2]} · ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`);
  }, tickMs);
  ticker.unref?.();
  return { close() { clearInterval(ticker); try { socket.close(); } catch { /* closed */ } }, ready };
}

/**
 * @param {object} o
 * @param {Object<string,string>} o.images slot -> absolute path of an RGBA cut-out PNG (front required; left/back/right optional)
 * @param {number} [o.seed=1000] @param {number} [o.targetFaces=700000] @param {string} [o.sampler='heun']
 * @param {boolean} [o.removeBackground=false] ask Pixal3D to cut the background itself (only for images without alpha)
 * @param {string} [o.tag] part of the uploaded file names and the output prefix
 * @param {AbortSignal} [o.signal] @param {(p:{percent:number,message:string})=>void} [o.onProgress]
 * @param {(promptId:string)=>void} [o.onPrompt] @param {(text:string)=>void} [o.onWait]
 * @param {object} [o.cfg] pixal3dConfig() @param {object} [o.deps] test doubles: start, acquire, WebSocketImpl, free, tickMs
 * @returns {Promise<{handover:object|null, buffer:Buffer, seconds:number, promptId:string, slots:string[], seed:number, targetFaces:number, rawBytes:number, normalization:object, vram:object|null, url:string}>}
 */
export async function generatePixal3d(o) {
  const { signal, onProgress, onPrompt, onWait, deps = {} } = o;
  const cfg = o.cfg || pixal3dConfig();
  const seed = Number.isFinite(Number(o.seed)) ? Number(o.seed) : 1000, targetFaces = o.targetFaces || 700000;
  const files = Object.fromEntries(Object.entries(o.images || {}).filter(([, file]) => file));
  if (!files.front) throw new Error('Pixal3D requires a front view.');
  const slots = ['front', 'left', 'back', 'right'].filter((s) => files[s]);
  const tag = String(o.tag || randomUUID()).replace(/[^0-9a-zA-Z-]/g, '').slice(0, 24);

  onProgress?.({ percent: 0, message: 'Checking the Pixal3D engine' });
  await (deps.start || startPixal3dRuntime)({ cfg, signal, onProgress: (message) => onProgress?.({ percent: 0, message }) });
  signal?.throwIfAborted();

  const workflowFor = (names) => pixal3dWorkflow(names, { seed, prefix: 'astra/px-' + tag, targetFaces, sampler: o.sampler || 'heun', removeBackground: !!o.removeBackground });
  const wanted = [...new Set(Object.values(workflowFor(Object.fromEntries(slots.map((s) => [s, s])))).map((n) => n.class_type))];
  let info;
  try { info = await comfyJson(cfg.url + '/object_info', { signal: AbortSignal.timeout(30000) }); } catch { throw new Error('The Pixal3D engine is not responding. See data/runtime/pixal3d.log.'); }
  const missingNodes = wanted.filter((cls) => !info[cls]);
  if (missingNodes.length) throw new Error('ComfyUI-Pixal3D did not load the nodes: ' + missingNodes.slice(0, 4).join(', ') + '. Restart the engine (Stop.cmd, then Start.cmd).');

  onProgress?.({ percent: 0, message: 'Uploading views to Pixal3D' });
  const names = {};
  for (const slot of slots) names[slot] = await uploadImage(cfg.url, files[slot], `astra-px-${tag}-${slot}.png`, signal);
  const workflow = workflowFor(names);

  const gpu = await (deps.acquire || acquireEngineGpu)('pixal3d', {
    signal, failOpen: true, timeoutMs: 60 * 60 * 1000, urls: { pixal3d: cfg.url },
    onWait: () => onWait?.('Waiting for the GPU to become free (GPU lock)'), onStep: (text) => onWait?.(text),
  });
  const clientId = randomUUID();
  let promptId = null, vramProbe = false;
  const follower = followProgress({ url: cfg.url, clientId, workflow, onProgress, getPromptId: () => promptId, WebSocketImpl: deps.WebSocketImpl || WebSocket, tickMs: deps.tickMs });
  const started = Date.now();
  try {
    await follower.ready;
    // someone else (another program or session) may already be generating on this engine: our prompt simply waits behind theirs in ComfyUI's own queue
    const own = await engineQueue(cfg.url);
    if (own && (own.running || own.pending)) onWait?.('The Pixal3D queue already holds other jobs: waiting for my turn');
    onProgress?.({ percent: 0, message: 'Pixal3D: queueing the job' });
    vramProbe = await fetch(cfg.url + '/af/vram/start', { method: 'POST', signal: AbortSignal.timeout(3000) }).then((r) => r.ok).catch(() => false);
    const queued = await comfyJson(cfg.url + '/prompt', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: workflow, client_id: clientId }), signal });
    promptId = queued.prompt_id;
    onPrompt?.(promptId);
    let item = null, failures = 0;
    const deadline = Date.now() + 60 * 60 * 1000;
    while (!item) {
      if (Date.now() > deadline) throw new Error('Pixal3D timed out (60 minutes).');
      signal?.throwIfAborted();
      let history;
      try { history = await comfyJson(cfg.url + '/history/' + promptId, { signal }); failures = 0; } catch (e) {
        if (e.name === 'AbortError') throw e;
        if (++failures >= 6) throw new Error('The Pixal3D engine stopped responding during generation (it may have run out of memory). See data/runtime/pixal3d.log.');
        await pause(1500, signal);
        continue;
      }
      const h = history[promptId];
      if (h) {
        const error = h.status?.messages?.find((m) => m[0] === 'execution_error');
        if (error) throw new Error(explainPixal3dError(error[1]?.exception_message));
        if (h.status?.status_str === 'error') throw new Error('Pixal3D could not run the generation. See data/runtime/pixal3d.log.');
        if (h.status?.completed) {
          item = Object.values(h.outputs || {}).flatMap((x) => x['3d'] || [])[0];
          if (!item) throw new Error('Pixal3D did not return a GLB.');
          break;
        }
      }
      await pause(1500, signal);
    }
    onProgress?.({ percent: 98, message: 'Fetching and normalizing the GLB' });
    const raw = await downloadGlb(cfg.url, item, signal);
    validateGeneratedMesh(raw);
    const normalized = normalizeGlb(raw);
    validateGeneratedMesh(normalized.buffer);
    const vram = vramProbe ? await comfyJson(cfg.url + '/af/vram/stop', { method: 'POST', signal: AbortSignal.timeout(3000) }).catch(() => null) : null;
    vramProbe = false;
    return {
      buffer: normalized.buffer, seconds: Math.round((Date.now() - started) / 100) / 10, promptId, slots, seed, targetFaces, rawBytes: raw.length, url: cfg.url, vram, handover: gpu.handover || null,
      normalization: { scale: normalized.scale, center: normalized.center, extentBefore: normalized.before.extent, extentAfter: normalized.after.extent },
    };
  } catch (error) {
    if (promptId && (error.name === 'AbortError' || signal?.aborted)) await cancelPrompt(cfg.url, promptId);
    throw error;
  } finally {
    follower.close();
    if (vramProbe) await fetch(cfg.url + '/af/vram/stop', { method: 'POST', signal: AbortSignal.timeout(3000) }).catch(() => {});
    // models are reloaded by every Pixal3D run anyway: give the memory back to whoever uses the card next
    await (deps.free || freeIfIdle)(cfg.url).catch(() => {});
    await gpu.release();
  }
}

/** Absolute paths of the RGBA cut-outs of a view set folder (views.json normalized.files[slot].cut), for the slots that exist. */
export async function cutFilesOf(dir, views, only = ['front', 'left', 'back', 'right']) {
  const out = {};
  for (const slot of only) {
    const rel = views?.normalized?.files?.[slot]?.cut;
    if (!rel) continue;
    const file = path.join(dir, rel);
    try { await fs.access(file); out[slot] = file; } catch { /* cut-out missing: slot skipped */ }
  }
  return out;
}
