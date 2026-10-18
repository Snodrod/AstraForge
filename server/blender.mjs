// Headless Blender runner for Forge steps (docs/FORGE-CONTRACT.md §3, §9).
//
//   blender -b --factory-startup --python scripts/blender/af_run.py -- <step> <request.json>
//
// Streams stdout, parses AFLOG / AFRESULT / AFERROR lines, keeps a per-step log file,
// enforces timeouts, kills the whole process tree on cancel and serialises runs through a
// FIFO queue (ASTRA_BLENDER_CONCURRENCY, default 1). GPU steps also take the shared GPU mutex.
import { spawn } from 'node:child_process';
import { createWriteStream, existsSync, promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { acquireGpuLock } from './gpu-lock.mjs';

export const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const DEFAULT_BLENDER = 'C:/Program Files/Blender Foundation/Blender 5.0/blender.exe';
export const AF_RUN = path.join(PROJECT_ROOT, 'scripts', 'blender', 'af_run.py');
export const DEFAULT_TIMEOUT_SEC = 15 * 60;
export const RENDER_TIMEOUT_SEC = 60 * 60;
const TAIL_LINES = 40;

export function blenderPath(env = process.env) {
  return env.ASTRA_BLENDER || DEFAULT_BLENDER;
}

// ------------------------------------------------------------------ errors

export class BlenderStepError extends Error {
  constructor(message, { code = 'step_failed', payload = null, logTail = [], exitCode = null, logFile = null } = {}) {
    super(message);
    this.name = 'BlenderStepError';
    this.code = code;
    this.payload = payload;
    this.logTail = logTail;
    this.exitCode = exitCode;
    this.logFile = logFile;
  }
}

function abortError(message = 'Step cancelled') {
  const err = new Error(message);
  err.name = 'AbortError';
  err.code = 'cancelled';
  return err;
}

// ------------------------------------------------------------------ process helpers

/** Kill a process and all its children (taskkill /T /F on Windows). */
export function killTree(child) {
  return new Promise((resolve) => {
    if (!child || !child.pid || child.exitCode !== null || child.signalCode) return resolve();
    if (process.platform === 'win32') {
      const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      killer.on('error', () => { try { child.kill(); } catch { /* gone */ } resolve(); });
      killer.on('exit', () => resolve());
    } else {
      try { process.kill(-child.pid, 'SIGKILL'); } catch { try { child.kill('SIGKILL'); } catch { /* gone */ } }
      resolve();
    }
  });
}

function runCapture(command, args, { timeoutMs = 60000, cwd } = {}) {
  return new Promise((resolve, reject) => {
    let out = '';
    let child;
    try {
      child = spawn(command, args, { windowsHide: true, cwd, stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err) {
      return reject(err);
    }
    const timer = setTimeout(() => { void killTree(child); reject(new Error('timeout')); }, timeoutMs);
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { out += d; });
    child.on('error', (err) => { clearTimeout(timer); reject(err); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, out }); });
  });
}

// ------------------------------------------------------------------ discovery

const infoCache = new Map();

/** {path, ok, version, error?}; the version probe is cached per path. */
export function blenderInfo({ path: exe = blenderPath(), refresh = false } = {}) {
  if (!refresh && infoCache.has(exe)) return infoCache.get(exe);
  const probe = (async () => {
    if (!existsSync(exe)) {
      return { path: exe, ok: false, version: null, error: `Blender not found: ${exe}. Set the path in the ASTRA_BLENDER variable.` };
    }
    try {
      const { out } = await runCapture(exe, ['-b', '--factory-startup', '--version'], { timeoutMs: 60000 });
      const m = /Blender\s+(\d+\.\d+(?:\.\d+)?)/.exec(out);
      if (!m) return { path: exe, ok: false, version: null, error: 'Blender did not report its version.' };
      return { path: exe, ok: true, version: m[1] };
    } catch (err) {
      return { path: exe, ok: false, version: null, error: `Failed to start Blender: ${err.message}` };
    }
  })();
  infoCache.set(exe, probe);
  probe.then((info) => { if (!info.ok) infoCache.delete(exe); }, () => infoCache.delete(exe));
  return probe;
}

const GODOT_CANDIDATE_DIRS = [
  'C:/Program Files/Godot',
  'C:/Godot',
  process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Programs', 'Godot') : null,
].filter(Boolean);

async function findGodotPath() {
  if (process.env.ASTRA_GODOT) return process.env.ASTRA_GODOT;
  const found = [];
  for (const dir of GODOT_CANDIDATE_DIRS) {
    let names = [];
    try { names = await fs.readdir(dir); } catch { continue; }
    for (const name of names) {
      const m = /^Godot_v(\d+)\.(\d+)(?:\.(\d+))?-stable_win64(_console)?\.exe$/i.exec(name);
      if (m) found.push({ file: path.join(dir, name), v: [Number(m[1]), Number(m[2]), Number(m[3] || 0)], console: !!m[4] });
    }
  }
  found.sort((a, b) => (b.v[0] - a.v[0]) || (b.v[1] - a.v[1]) || (b.v[2] - a.v[2]) || (Number(b.console) - Number(a.console)));
  return found[0]?.file || null;
}

let godotCache = null;

/** {path, ok, version, error?} for Godot (env ASTRA_GODOT or a few known folders). Cached. */
export function godotInfo({ refresh = false } = {}) {
  if (godotCache && !refresh) return godotCache;
  godotCache = (async () => {
    const exe = await findGodotPath();
    if (!exe || !existsSync(exe)) {
      return { path: exe, ok: false, version: null, error: 'Godot not found. Set the path in the ASTRA_GODOT variable.' };
    }
    try {
      const { out } = await runCapture(exe, ['--headless', '--version'], { timeoutMs: 30000 });
      const m = /(\d+\.\d+(?:\.\d+)?)\.(?:stable|beta|rc|dev)/i.exec(out);
      return m ? { path: exe, ok: true, version: m[1] } : { path: exe, ok: false, version: null, error: 'Godot did not report its version.' };
    } catch (err) {
      return { path: exe, ok: false, version: null, error: `Failed to start Godot: ${err.message}` };
    }
  })();
  return godotCache;
}

// ------------------------------------------------------------------ protocol

/** Parse one stdout line of af_run.py. */
export function parseProtocolLine(raw) {
  const line = String(raw).replace(/\r$/, '');
  for (const [prefix, type] of [['AFRESULT ', 'result'], ['AFERROR ', 'error'], ['AFLOG ', 'log']]) {
    if (line.startsWith(prefix)) {
      try {
        return { type, data: JSON.parse(line.slice(prefix.length)), text: line };
      } catch {
        return { type: 'text', text: line, malformed: type };
      }
    }
  }
  return { type: 'text', text: line };
}

/** Progress in percent from an AFLOG payload, or null. */
export function progressOf(data) {
  if (!data || typeof data !== 'object') return null;
  let p = null;
  if (Number.isFinite(data.progress)) p = data.progress <= 1 ? data.progress * 100 : data.progress;
  else if (Number.isFinite(data.pct)) p = data.pct;
  else if (Number.isFinite(data.done) && Number.isFinite(data.total) && data.total > 0) p = (data.done / data.total) * 100;
  else if (Number.isFinite(data.frame) && Number.isFinite(data.frames) && data.frames > 0) p = (data.frame / data.frames) * 100;
  if (p === null) return null;
  return Math.max(0, Math.min(100, Math.round(p * 10) / 10));
}

// ------------------------------------------------------------------ queue

export class StepQueue {
  constructor(concurrency = 1) {
    this.concurrency = Math.max(1, Math.floor(Number(concurrency) || 1));
    this.running = 0;
    this.items = [];
  }

  get queued() {
    return this.items.length;
  }

  stats() {
    return { running: this.running, queued: this.items.length, concurrency: this.concurrency };
  }

  /**
   * Run fn(slot) when a slot is free. Aborting while queued removes the task.
   * `front: true` queues ahead of ordinary tasks (used by a GPU step that already holds the GPU
   * mutex, so the GPU is not kept idle behind a long queue).
   * The slot lets a long wait give the place back: slot.release() frees it for other tasks and
   * `await slot.reacquire(signal)` takes one again ahead of the queue (resolves false if aborted).
   */
  run(fn, { signal, onStart, front = false } = {}) {
    if (signal?.aborted) return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const item = { fn, resolve, reject, signal, onStart, onAbort: null, front: !!front };
      if (signal) {
        item.onAbort = () => {
          const idx = this.items.indexOf(item);
          if (idx >= 0) {
            this.items.splice(idx, 1);
            reject(abortError());
          }
        };
        signal.addEventListener('abort', item.onAbort, { once: true });
      }
      if (item.front) this._insertFront(item);
      else this.items.push(item);
      this._pump();
    });
  }

  /** Re-entries first (in order), then front tasks (in order), then everything else. */
  _insertFront(item) {
    const rank = (i) => (i.reenter ? 0 : i.front ? 1 : 2);
    const mine = rank(item);
    const idx = this.items.findIndex((i) => rank(i) > mine);
    if (idx < 0) this.items.push(item);
    else this.items.splice(idx, 0, item);
  }

  _slot() {
    const slot = {
      held: true,
      release: () => {
        if (!slot.held) return;
        slot.held = false;
        this.running -= 1;
        this._pump();
      },
      reacquire: (signal) => {
        if (slot.held) return Promise.resolve(true);
        if (slot.closed || signal?.aborted) return Promise.resolve(false);
        return new Promise((resolve) => {
          const item = { reenter: true, slot, resolve, signal, onAbort: null };
          if (signal) {
            item.onAbort = () => {
              const idx = this.items.indexOf(item);
              if (idx >= 0) {
                this.items.splice(idx, 1);
                resolve(false);
              }
            };
            signal.addEventListener('abort', item.onAbort, { once: true });
          }
          this._insertFront(item);
          this._pump();
        });
      },
    };
    return slot;
  }

  _pump() {
    while (this.running < this.concurrency && this.items.length) {
      const item = this.items.shift();
      if (item.signal && item.onAbort) item.signal.removeEventListener('abort', item.onAbort);
      this.running += 1;
      if (item.reenter) {
        item.slot.held = true;
        item.resolve(true);
        continue;
      }
      const slot = this._slot();
      Promise.resolve()
        .then(() => { item.onStart?.(); return item.fn(slot); })
        .then(item.resolve, item.reject)
        .finally(() => {
          slot.closed = true;
          // a re-entry the task did not wait for must not hold a slot forever
          for (const pending of this.items.filter((i) => i.reenter && i.slot === slot)) {
            this.items.splice(this.items.indexOf(pending), 1);
            if (pending.signal && pending.onAbort) pending.signal.removeEventListener('abort', pending.onAbort);
            pending.resolve(false);
          }
          if (slot.held) {
            slot.held = false;
            this.running -= 1;
          }
          this._pump();
        });
    }
  }
}

export const blenderQueue = new StepQueue(Number(process.env.ASTRA_BLENDER_CONCURRENCY) || 1);

// ------------------------------------------------------------------ running a step

function defaultCommand({ blender, step, requestPath }) {
  return { command: blender, args: ['-b', '--factory-startup', '--python', AF_RUN, '--', step, requestPath] };
}

/**
 * Run one Forge step in Blender.
 * @param {object} o
 * @param {string} o.step
 * @param {object} o.request  {inputs, params, out_dir, ...} (all paths absolute)
 * @param {string} [o.jobId]
 * @param {string} [o.logDir]  default data/runtime/blender/<jobId>
 * @param {string} [o.logName] default <step>.log
 * @param {number} [o.timeoutSec]
 * @param {boolean} [o.gpu]    take the GPU mutex for the duration of the run (waited for before the
 *                             queue slot, so CPU steps keep running meanwhile)
 * @param {AbortSignal} [o.signal]
 * @param {(entry:object) => void} [o.onLog]       AFLOG payloads and plain lines
 * @param {(pct:number, data:object) => void} [o.onProgress]
 * @param {() => void} [o.onStart]                 called when the run leaves the queue
 * @returns {Promise<{result:object, logFile:string, logTail:string[], exitCode:number, seconds:number, warnings:string[]}>}
 */
export async function runBlenderStep(o) {
  const step = String(o.step || '');
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(step)) throw new BlenderStepError(`Invalid step name: ${step}`, { code: 'bad_step' });
  const request = { ...o.request, step };
  if (!request.out_dir || !path.isAbsolute(request.out_dir)) {
    throw new BlenderStepError('The step request requires an absolute out_dir.', { code: 'bad_request' });
  }
  request.inputs = request.inputs || {};
  request.params = request.params || {};
  const jobId = String(o.jobId || 'adhoc').replace(/[^A-Za-z0-9_-]/g, '_');
  const logDir = o.logDir || path.join(PROJECT_ROOT, 'data', 'runtime', 'blender', jobId);
  const logFile = path.join(logDir, o.logName || `${step}.log`);
  const timeoutSec = Number(o.timeoutSec) > 0 ? Number(o.timeoutSec) : DEFAULT_TIMEOUT_SEC;
  const queue = o.queue || blenderQueue;
  const blender = o.blender || blenderPath();
  const commandFor = o.command || defaultCommand;
  const acquire = o.acquireGpu || acquireGpuLock;

  const requestPath = path.join(request.out_dir, 'request.json');
  const slotTask = (warnings) => async () => {
    await fs.mkdir(request.out_dir, { recursive: true });
    await fs.mkdir(logDir, { recursive: true });
    await fs.writeFile(requestPath, JSON.stringify(request, null, 2), 'utf8');
    const run = await spawnAndParse({
      step, request, requestPath, logFile, timeoutSec, blender, commandFor,
      signal: o.signal, onLog: o.onLog, onProgress: o.onProgress, env: o.env,
    });
    run.warnings = [...warnings, ...run.warnings];
    return run;
  };

  // CPU steps: the queue slot is reserved synchronously, so runs start in call order (FIFO).
  if (!o.gpu) return queue.run(slotTask([]), { signal: o.signal, onStart: o.onStart });

  // GPU steps wait for the shared GPU mutex *before* taking a queue slot, so CPU-only steps keep
  // running meanwhile; once the mutex is held the step queues ahead of ordinary steps, so the
  // GPU is not kept idle longer than the step that is currently running.
  const warnings = [];
  const lock = await acquire({
    signal: o.signal,
    timeoutMs: Number(o.gpuWaitMs) > 0 ? Number(o.gpuWaitMs) : timeoutSec * 1000,
    failOpen: true,
    onWait: () => o.onLog?.({ msg: 'Waiting for the GPU to become free (GPU lock)', level: 'wait' }),
  });
  if (lock.noop && lock.warning) warnings.push(lock.warning);
  if (lock.abandoned) warnings.push('The GPU lock was abandoned by its previous owner and has been taken over.');
  try {
    return await queue.run(slotTask(warnings), { signal: o.signal, onStart: o.onStart, front: true });
  } finally {
    await lock.release();
  }
}

/** Last Python error line of a log tail (Traceback summary), or null. */
export function lastErrorLine(tail = []) {
  for (let i = tail.length - 1; i >= 0; i -= 1) {
    const line = String(tail[i] || '').replace(/^\[stderr\]\s*/, '').trim();
    if (!line || /^AF(LOG|RESULT)\b/.test(line)) continue;
    if (/^Traceback\b/.test(line)) continue;
    if (/^[A-Za-z_][\w.]*(Error|Exception|Interrupt|Exit)\b/.test(line) || /\bError:/.test(line)) return line.slice(0, 500);
  }
  return null;
}

function spawnAndParse({ step, request, requestPath, logFile, timeoutSec, blender, commandFor, signal, onLog, onProgress, env }) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const started = Date.now();
    const { command, args } = commandFor({ blender, step, requestPath, request });
    const log = createWriteStream(logFile, { flags: 'a' });
    log.on('error', () => {});
    log.write(`# ${new Date().toISOString()} ${command} ${args.join(' ')}\n`);
    const tail = [];
    const warnings = [];
    let result = null;
    let errorPayload = null;
    let timedOut = false;
    let cancelled = false;
    let finished = false;
    let exitTimer = null;

    const remember = (line) => {
      tail.push(line.length > 2000 ? line.slice(0, 2000) + '…' : line);
      if (tail.length > TAIL_LINES) tail.shift();
    };

    let child;
    try {
      child = spawn(command, args, {
        cwd: PROJECT_ROOT,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: process.platform !== 'win32',
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', ...(env || {}) },
      });
    } catch (err) {
      log.end();
      return reject(new BlenderStepError(`Failed to start Blender: ${err.message}`, { code: 'spawn_failed', logFile }));
    }

    const handleLine = (raw, stream) => {
      const parsed = parseProtocolLine(raw);
      log.write((stream === 'stderr' ? '[stderr] ' : '') + parsed.text + '\n');
      if (parsed.type === 'result') {
        result = parsed.data;
        remember('AFRESULT ' + (result?.ok === false ? 'ok=false' : 'ok=true'));
        return;
      }
      if (parsed.type === 'error') {
        errorPayload = parsed.data;
        remember('AFERROR ' + (parsed.data?.error || ''));
        return;
      }
      if (parsed.type === 'log') {
        const msg = parsed.data?.msg ?? '';
        remember(String(msg) + (parsed.data && Object.keys(parsed.data).length > 2 ? ' ' + JSON.stringify(parsed.data) : ''));
        try { onLog?.(parsed.data); } catch { /* listener errors must not break the run */ }
        const pct = progressOf(parsed.data);
        if (pct !== null) { try { onProgress?.(pct, parsed.data); } catch { /* ignore */ } }
        return;
      }
      if (parsed.text.trim()) {
        remember((stream === 'stderr' ? '[stderr] ' : '') + parsed.text);
        if (stream === 'stderr' || /error|traceback/i.test(parsed.text)) {
          try { onLog?.({ msg: parsed.text, level: stream === 'stderr' ? 'stderr' : 'text' }); } catch { /* ignore */ }
        }
      }
    };

    const lineReader = (stream) => {
      let buf = '';
      child[stream].setEncoding('utf8');
      child[stream].on('data', (chunk) => {
        buf += chunk;
        let idx;
        while ((idx = buf.indexOf('\n')) >= 0) {
          handleLine(buf.slice(0, idx), stream);
          buf = buf.slice(idx + 1);
        }
        if (buf.length > 4 * 1024 * 1024) { handleLine(buf, stream); buf = ''; }
      });
      child[stream].on('end', () => { if (buf) handleLine(buf, stream); buf = ''; });
    };
    lineReader('stdout');
    lineReader('stderr');

    const timer = setTimeout(() => {
      timedOut = true;
      void killTree(child);
    }, timeoutSec * 1000);
    const onAbort = () => {
      cancelled = true;
      void killTree(child);
    };
    signal?.addEventListener('abort', onAbort, { once: true });

    const finish = async (code) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      if (exitTimer) clearTimeout(exitTimer);
      signal?.removeEventListener('abort', onAbort);
      const seconds = Math.round((Date.now() - started) / 100) / 10;
      log.write(`# exit ${code} after ${seconds}s${timedOut ? ' (timeout)' : ''}${cancelled ? ' (cancelled)' : ''}\n`);
      await new Promise((r) => log.end(r));
      const meta = { logTail: [...tail], exitCode: code, logFile };
      if (cancelled) return reject(Object.assign(abortError(), meta));
      if (timedOut) {
        return reject(new BlenderStepError(
          `Step "${step}" exceeded the time limit (${Math.round(timeoutSec / 60)} min) and was stopped.`,
          { code: 'timeout', ...meta }));
      }
      if (errorPayload) {
        return reject(new BlenderStepError(
          `Step "${step}" failed: ${String(errorPayload.error || 'unknown error').slice(0, 1500)}`,
          { code: 'step_error', payload: errorPayload, ...meta }));
      }
      if (!result) {
        // af_run prints AFRESULT last; fall back to result.json written in this run.
        try {
          const file = path.join(request.out_dir, 'result.json');
          const st = await fs.stat(file);
          if (st.mtimeMs >= started - 1000) result = JSON.parse(await fs.readFile(file, 'utf8'));
        } catch { /* no result file */ }
      }
      if (!result) {
        // A --python script that raises before af_run's own handler exits Blender with code 0;
        // the Python error is only on stderr, so surface its last line.
        const pyError = lastErrorLine(tail);
        return reject(new BlenderStepError(
          `Blender exited without a result for step "${step}" (code ${code})${pyError ? `: ${pyError}` : ''}. See the log: ${logFile}`,
          { code: 'no_result', ...meta }));
      }
      if (code !== 0) warnings.push(`Blender exited with code ${code} after returning the result.`);
      resolve({ result, logFile, logTail: [...tail], exitCode: code, seconds, warnings, requestPath });
    };

    child.on('error', (err) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      log.end();
      reject(new BlenderStepError(`Failed to start Blender (${command}): ${err.message}`, { code: 'spawn_failed', logFile }));
    });
    child.on('exit', (code) => {
      // If a grandchild keeps stdout open, do not wait for it forever.
      exitTimer = setTimeout(() => void finish(code), 3000);
    });
    child.on('close', (code) => void finish(code));
  });
}
