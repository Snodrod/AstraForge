// Real cross-process GPU mutex (Windows named mutex via a PowerShell holder).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { GPU_MUTEX_NAME, acquireGpuLock, gpuLockName, gpuLockState } from '../server/gpu-lock.mjs';

const isWin = process.platform === 'win32';
const testName = () => `Local\\AF_TEST_${randomUUID()}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('GPU mutex name: default Global\\AF_GPU_RENDER, ASTRA_GPU_LOCK_NAME overrides', () => {
  assert.equal(GPU_MUTEX_NAME, 'Global\\AF_GPU_RENDER');
  assert.equal(gpuLockState().name, process.env.ASTRA_GPU_LOCK_NAME || process.env.ASTRA_GPU_MUTEX || 'Global\\AF_GPU_RENDER');
  assert.equal(gpuLockName({}), 'Global\\AF_GPU_RENDER');
  assert.equal(gpuLockName({ ASTRA_GPU_LOCK_NAME: 'Local\\MyLock' }), 'Local\\MyLock');
  assert.equal(gpuLockName({ ASTRA_GPU_MUTEX: 'Local\\Old', ASTRA_GPU_LOCK_NAME: 'Local\\New' }), 'Local\\New');
});

test('two acquirers of the real mutex are serialised', { skip: !isWin && 'Windows only' }, async () => {
  const name = testName();
  const a = await acquireGpuLock({ name, timeoutMs: 20000 });
  let b = null;
  let bPromise = null;
  try {
    assert.equal(a.noop, false);
    assert.equal(gpuLockState().held, true);
    let bAt = 0;
    let waited = false;
    bPromise = acquireGpuLock({ name, timeoutMs: 20000, onWait: () => { waited = true; } }).then((lock) => { bAt = Date.now(); b = lock; return lock; });
    await sleep(900);
    assert.equal(bAt, 0, 'second acquirer must wait while the first holds the mutex');
    assert.equal(waited, true, 'second acquirer reports that it is waiting');
    const releasedAt = Date.now();
    await a.release();
    await bPromise;
    assert.ok(bAt >= releasedAt, 'second acquirer gets the mutex only after release');
    await b.release();
    await b.release(); // idempotent
    assert.equal(gpuLockState().held, false);
  } finally {
    await a.release();
    if (bPromise) await bPromise.catch(() => {});
    await b?.release();
  }
});

test('an abandoned mutex (holder killed) is acquirable', { skip: !isWin && 'Windows only' }, async () => {
  const name = testName();
  const a = await acquireGpuLock({ name, timeoutMs: 20000 });
  let b = null;
  try {
    let markWaiting;
    const waiting = new Promise((r) => { markWaiting = r; });
    const pending = acquireGpuLock({ name, timeoutMs: 20000, onWait: () => markWaiting() });
    await waiting;
    process.kill(a.pid, 'SIGKILL'); // TerminateProcess: Windows marks the mutex abandoned
    b = await pending;
    assert.equal(b.abandoned, true);
  } finally {
    await b?.release();
    await a.release(); // releasing a dead holder is a no-op
  }
  // With no other handle open the mutex object disappears entirely; a fresh acquire still works.
  const c = await acquireGpuLock({ name, timeoutMs: 20000 });
  spawnSync('taskkill', ['/PID', String(c.pid), '/T', '/F'], { windowsHide: true });
  const d = await acquireGpuLock({ name, timeoutMs: 20000 });
  await d.release();
  await c.release();
});

test('waiting for the mutex honours timeout and abort', { skip: !isWin && 'Windows only' }, async () => {
  const name = testName();
  const a = await acquireGpuLock({ name, timeoutMs: 20000 });
  try {
    await assert.rejects(acquireGpuLock({ name, timeoutMs: 400 }), (err) => err.code === 'gpu_timeout' && /The GPU has been busy/.test(err.message));
    const controller = new AbortController();
    const pending = acquireGpuLock({ name, signal: controller.signal });
    setTimeout(() => controller.abort(), 300);
    await assert.rejects(pending, (err) => err.name === 'AbortError');
  } finally {
    await a.release();
  }
  const c = await acquireGpuLock({ name, timeoutMs: 5000 });
  await c.release();
});
