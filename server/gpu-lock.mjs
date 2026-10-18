// Cross-process GPU mutex: every studio job (and any other tool that opts in) serialises GPU work
// on one Windows named mutex.
//
// Name: env ASTRA_GPU_LOCK_NAME (ASTRA_GPU_MUTEX is accepted as an older alias), default
// 'Global\AF_GPU_RENDER'. Another pipeline that should take turns with the studio uses the same
// name (or the studio is pointed at its name). Node cannot own a Win32 mutex, so a hidden PowerShell "holder"
// process creates/acquires the mutex, prints LOCKED and keeps it until it reads a line on
// stdin (release) or its stdin closes (our process died). If the holder itself is killed,
// Windows marks the mutex abandoned and the next waiter acquires it normally.
import { spawn } from 'node:child_process';
import path from 'node:path';

export const GPU_MUTEX_NAME = 'Global\\AF_GPU_RENDER';

const HOLDER_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$name = $env:AF_GPU_MUTEX
$timeoutMs = [int]$env:AF_GPU_TIMEOUT_MS
$parentPid = [int]$env:AF_GPU_PARENT
$m = New-Object System.Threading.Mutex($false, $name)
$got = $false
$announced = $false
$start = [Environment]::TickCount
while (-not $got) {
  try { $got = $m.WaitOne(250) }
  catch [System.Threading.AbandonedMutexException] { $got = $true; [Console]::Out.WriteLine('ABANDONED') }
  if ($got) { break }
  if (-not $announced) { [Console]::Out.WriteLine('WAITING'); [Console]::Out.Flush(); $announced = $true }
  if ($parentPid -gt 0) {
    try { $null = [System.Diagnostics.Process]::GetProcessById($parentPid) } catch { exit 4 }
  }
  if ($timeoutMs -ge 0 -and ([Environment]::TickCount - $start) -ge $timeoutMs) {
    [Console]::Out.WriteLine('TIMEOUT'); [Console]::Out.Flush(); exit 3
  }
}
[Console]::Out.WriteLine('LOCKED')
[Console]::Out.Flush()
try { $null = [Console]::In.ReadLine() }
finally { $m.ReleaseMutex(); $m.Dispose() }
[Console]::Out.WriteLine('RELEASED')
[Console]::Out.Flush()
`;
const ENCODED_SCRIPT = Buffer.from(HOLDER_SCRIPT, 'utf16le').toString('base64');

const state = { held: 0, waiting: 0, lastAbandoned: null };

export function gpuLockName(env = process.env) {
  return env.ASTRA_GPU_LOCK_NAME || env.ASTRA_GPU_MUTEX || GPU_MUTEX_NAME;
}

/** In-process view of the lock (the mutex may also be held by other processes). */
export function gpuLockState() {
  return {
    name: gpuLockName(),
    held: state.held > 0,
    holders: state.held,
    waiting: state.waiting,
    supported: process.platform === 'win32',
    lastAbandoned: state.lastAbandoned,
  };
}

function powershellPath() {
  const sysRoot = process.env.SystemRoot || 'C:\\Windows';
  return path.join(sysRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
}

function killProcessTree(child) {
  if (!child || child.exitCode !== null || child.signalCode) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
      .on('error', () => {});
  } else {
    try { child.kill('SIGKILL'); } catch { /* already gone */ }
  }
}

function abortError(message = 'Waiting for the GPU was cancelled') {
  const err = new Error(message);
  err.name = 'AbortError';
  err.code = 'aborted';
  return err;
}

function noopLock(name, warning) {
  return { name, noop: true, abandoned: false, warning, held: false, release: async () => {} };
}

/**
 * Acquire the GPU mutex.
 * @param {object} [opts]
 * @param {string} [opts.name] mutex name (default env ASTRA_GPU_LOCK_NAME, else Global\AF_GPU_RENDER)
 * @param {number} [opts.timeoutMs] -1 = wait forever
 * @param {AbortSignal} [opts.signal]
 * @param {boolean} [opts.failOpen] if the holder cannot start, resolve with a no-op lock
 * @param {() => void} [opts.onWait] called once when the mutex is busy and we start waiting
 * @returns {Promise<{name:string, abandoned:boolean, noop?:boolean, release:() => Promise<void>}>}
 */
export function acquireGpuLock(opts = {}) {
  const name = opts.name || gpuLockName();
  const timeoutMs = Number.isFinite(opts.timeoutMs) ? Math.max(-1, Math.round(opts.timeoutMs)) : -1;
  const { signal, failOpen = false, onWait } = opts;
  if (signal?.aborted) return Promise.reject(abortError());
  if (process.platform !== 'win32') {
    return Promise.resolve(noopLock(name, 'The cross-process GPU lock works on Windows only.'));
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    let locked = false;
    let abandoned = false;
    let buffer = '';
    let stderr = '';
    let startupTimer = null;
    state.waiting += 1;

    const child = spawn(powershellPath(), [
      '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', ENCODED_SCRIPT,
    ], {
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, AF_GPU_MUTEX: name, AF_GPU_TIMEOUT_MS: String(timeoutMs), AF_GPU_PARENT: String(process.pid) },
    });

    const finishWaiting = () => {
      if (startupTimer) clearTimeout(startupTimer);
      startupTimer = null;
      signal?.removeEventListener('abort', onAbort);
      state.waiting = Math.max(0, state.waiting - 1);
    };
    const fail = (err) => {
      if (settled) return;
      settled = true;
      finishWaiting();
      killProcessTree(child);
      reject(err);
    };
    const failSoft = (reason) => {
      if (settled) return;
      if (!failOpen) return fail(Object.assign(new Error(reason), { code: 'gpu_lock_failed' }));
      settled = true;
      finishWaiting();
      killProcessTree(child);
      resolve(noopLock(name, reason));
    };
    const onAbort = () => fail(abortError());
    signal?.addEventListener('abort', onAbort, { once: true });

    // Guard against a holder that never starts (PowerShell broken, AV scan, ...).
    if (timeoutMs >= 0) {
      startupTimer = setTimeout(() => fail(Object.assign(
        new Error(`The GPU has been busy with another process for more than ${Math.round(timeoutMs / 1000)} s.`),
        { code: 'gpu_timeout' })), timeoutMs + 20000);
    }

    let released = null;
    const lock = {
      name,
      pid: child.pid,
      abandoned: false,
      noop: false,
      get held() { return locked && child.exitCode === null; },
      release() {
        if (released) return released;
        released = new Promise((done) => {
          if (!locked) return done();
          locked = false;
          state.held = Math.max(0, state.held - 1);
          if (child.exitCode !== null || child.signalCode) return done();
          const killTimer = setTimeout(() => { killProcessTree(child); done(); }, 5000);
          child.once('exit', () => { clearTimeout(killTimer); done(); });
          try {
            child.stdin.write('release\n');
            child.stdin.end();
          } catch {
            clearTimeout(killTimer);
            killProcessTree(child);
            done();
          }
        });
        return released;
      },
    };

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (line === 'ABANDONED') {
          abandoned = true;
        } else if (line === 'WAITING') {
          try { onWait?.(); } catch { /* callback errors are not lock errors */ }
        } else if (line === 'TIMEOUT') {
          fail(Object.assign(new Error(`The GPU has been busy with another process for more than ${Math.round(timeoutMs / 1000)} s.`),
            { code: 'gpu_timeout' }));
        } else if (line === 'LOCKED' && !settled) {
          settled = true;
          locked = true;
          finishWaiting();
          state.held += 1;
          lock.abandoned = abandoned;
          if (abandoned) state.lastAbandoned = new Date().toISOString();
          resolve(lock);
        }
      }
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk) => { stderr = (stderr + chunk).slice(-2000); });
    child.stdin.on('error', () => {});
    child.on('error', (err) => failSoft(`Failed to start PowerShell for the GPU lock: ${err.message}`));
    // 'close' fires after stdout is drained, so LOCKED/TIMEOUT lines are always seen first.
    child.on('close', (code) => {
      if (locked) {
        // Holder died while we thought we owned the mutex: Windows released it as abandoned.
        locked = false;
        state.held = Math.max(0, state.held - 1);
        return;
      }
      if (!settled) {
        failSoft(`The GPU lock process exited before acquiring the lock (code ${code}). ${stderr.trim().slice(0, 300)}`.trim());
      }
    });
  });
}

/** Run fn while holding the GPU lock. */
export async function withGpuLock(fn, opts = {}) {
  const lock = await acquireGpuLock(opts);
  try {
    return await fn(lock);
  } finally {
    await lock.release();
  }
}
