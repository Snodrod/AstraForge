// Minimal ComfyUI HTTP client shared by the views scripts (the studio server has its own websocket-based one in
// server/jobs.mjs; scripts must also work with no studio running).
import { promises as fs, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

export const APP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const DATA = path.join(APP_ROOT, 'data');
export const DEFAULT_COMFY_URL = 'http://127.0.0.1:8190';

// ComfyUI install: env ASTRA_COMFY_DIR, else <app>/runtime/ComfyUI
export function comfyDir() { return process.env.ASTRA_COMFY_DIR || path.join(APP_ROOT, 'runtime', 'ComfyUI'); }
export function pythonExe() { return path.join(comfyDir(), '.venv', 'Scripts', 'python.exe'); }

async function jsonFetch(url, options = {}) {
  const res = await fetch(url, { ...options, signal: options.signal || AbortSignal.timeout(20000) });
  let value;
  try { value = await res.json(); } catch { throw new Error('ComfyUI returned an invalid response.'); }
  if (!res.ok) throw new Error(value?.error?.message || JSON.stringify(value?.node_errors || value?.error || value).slice(0, 700));
  return value;
}

export async function comfyQueue(url) {
  try { return await jsonFetch(url + '/queue', { signal: AbortSignal.timeout(4000) }); } catch { return null; }
}

/** Throws if ComfyUI is down; returns object_info for the given node classes. */
export async function requireNodes(url, nodes) {
  let info;
  try { info = await jsonFetch(url + '/object_info', { signal: AbortSignal.timeout(15000) }); } catch {
    throw new Error('The local 3D engine is not running. Start AstraForge (Start.cmd) or click "Start 3D engine" in Connections.');
  }
  for (const n of nodes) if (!info[n]) throw new Error('ComfyUI has no node ' + n + '. Restart the 3D engine (Stop.cmd, then Start.cmd): nodes are loaded at startup.');
  return info;
}

export async function uploadImage(url, file, name) {
  const form = new FormData();
  form.append('image', new Blob([await fs.readFile(file)]), name || path.basename(file));
  form.append('overwrite', 'true');
  const res = await jsonFetch(url + '/upload/image', { method: 'POST', body: form });
  return res.name;
}

/**
 * Queue a workflow and wait for it. Polls /history (no websocket needed).
 * onProgress(text) gets coarse status lines. Returns the history `outputs` object.
 */
export async function runWorkflow(url, workflow, { signal, onProgress, timeoutMs = 60 * 60 * 1000 } = {}) {
  const queued = await jsonFetch(url + '/prompt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: workflow }), signal,
  });
  const id = queued.prompt_id;
  const deadline = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    const history = await jsonFetch(url + '/history/' + id, { signal });
    const h = history[id];
    if (h) {
      const err = h.status?.messages?.find((m) => m[0] === 'execution_error');
      if (err) throw new Error(String(err[1]?.exception_message || 'Engine error').slice(0, 900));
      if (h.status?.status_str === 'error') throw new Error('ComfyUI could not run the job. See data/runtime/comfy.log.');
      if (h.status?.completed) return { outputs: h.outputs, promptId: id };
    } else {
      const q = await comfyQueue(url);
      const pos = q?.queue_pending?.findIndex((x) => x[1] === id);
      const text = pos >= 0 ? `engine queue position: ${pos + 1}` : 'running';
      if (text !== last) { last = text; onProgress?.(text); }
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error('ComfyUI timed out.');
}

export async function downloadOutput(url, item, dest) {
  const params = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || '', type: item.type || 'output' });
  const res = await fetch(url + '/view?' + params, { signal: AbortSignal.timeout(120000) });
  if (!res.ok) throw new Error('Could not fetch the result from ComfyUI.');
  await fs.mkdir(path.dirname(dest), { recursive: true });
  await fs.writeFile(dest, Buffer.from(await res.arrayBuffer()));
  return dest;
}

/** Run a script in the ComfyUI venv; resolves with the parsed `RESULT {json}` stdout line. */
export function runPython(script, args, { signal, timeoutMs = 600000, onLine } = {}) {
  const python = pythonExe();
  if (!existsSync(python)) return Promise.reject(new Error('ComfyUI Python is required (' + python + '). Set ASTRA_COMFY_DIR.'));
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-I', script, ...args], {
      windowsHide: true,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8', U2NET_HOME: path.join(DATA, 'models', 'background'), NUMBA_CACHE_DIR: path.join(DATA, 'runtime', 'temp', 'numba') },
    });
    let out = '', err = '';
    child.stdout.on('data', (b) => { out += b; onLine?.(String(b)); });
    child.stderr.on('data', (b) => { err = (err + b).slice(-2000); });
    const kill = () => { if (process.platform === 'win32') spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }); else child.kill(); };
    const timer = setTimeout(kill, timeoutMs);
    signal?.addEventListener('abort', kill, { once: true });
    child.on('error', (e) => { clearTimeout(timer); reject(e); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (signal?.aborted) return reject(new DOMException('Cancelled', 'AbortError'));
      const line = out.split(/\r?\n/).reverse().find((l) => l.startsWith('RESULT '));
      if (code !== 0 || !line) return reject(new Error(('Image processing failed. ' + (err || out)).trim().slice(-900)));
      try { resolve(JSON.parse(line.slice(7))); } catch { reject(new Error('The script returned an invalid response.')); }
    });
  });
}
