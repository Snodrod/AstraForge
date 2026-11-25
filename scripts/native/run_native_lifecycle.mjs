import { spawn, spawnSync } from 'node:child_process';
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
const mode = process.env.AMD_PIXAL3D_LIFECYCLE_MODE || 'cancel';
if (!['cancel', 'complete'].includes(mode)) throw new Error('AMD_PIXAL3D_LIFECYCLE_MODE must be cancel or complete.');
const port = 3212;
const studioUrl = `http://127.0.0.1:${port}`;
const engineUrl = 'http://127.0.0.1:8192';
const reportFile = path.join(diagnostics, mode === 'cancel' ? '512-cancel.json' : '512-complete-cleanup.json');
const report = { mode, backend: 'windows-rocm', job: null, promptId: null, samples: [], cleanup: null };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let studio;
let failure = null;

mkdirSync(diagnostics, { recursive: true });

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, signal: options.signal || AbortSignal.timeout(15000) });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${url}: ${response.status}`);
  return body;
}

function studioRequest(route, options = {}) {
  return request(studioUrl + route, { ...options, headers: { 'x-astra-client': 'studio', ...options.headers } });
}

async function engineSample(label) {
  const [stats, queue] = await Promise.all([
    request(engineUrl + '/system_stats', { signal: AbortSignal.timeout(15000) }),
    request(engineUrl + '/queue', { signal: AbortSignal.timeout(15000) }),
  ]);
  const device = stats.devices?.[0] || {};
  const mib = (bytes) => Number.isFinite(bytes) ? Math.round(bytes / 1048576) : null;
  const sample = {
    at: new Date().toISOString(), label,
    deviceUsedMb: mib(device.vram_total - device.vram_free),
    torchReservedMb: mib(device.torch_vram_total),
    torchAllocatedMb: mib(device.torch_vram_total - device.torch_vram_free),
    running: queue.queue_running?.map((item) => item[1]) || [],
    pending: queue.queue_pending?.map((item) => item[1]) || [],
  };
  report.samples.push(sample);
  return sample;
}

async function stopStudio() {
  if (!studio || studio.exitCode !== null) return;
  try { await studioRequest('/api/shutdown', { method: 'POST', signal: AbortSignal.timeout(3000) }); } catch { /* already stopped */ }
  for (let attempt = 0; attempt < 100 && studio.exitCode === null; attempt += 1) await sleep(100);
  if (studio.exitCode === null && process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(studio.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
  }
}

function cleanupVerdict(baseline, final) {
  if (!baseline || !final || baseline.torchReservedMb === null || final.torchReservedMb === null) {
    return { verified: false, reason: 'Torch VRAM telemetry is unavailable.' };
  }
  const toleranceMb = 768;
  const deltaMb = final.torchReservedMb - baseline.torchReservedMb;
  return {
    verified: deltaMb <= toleranceMb,
    baselineTorchReservedMb: baseline.torchReservedMb,
    finalTorchReservedMb: final.torchReservedMb,
    deltaMb,
    toleranceMb,
  };
}

try {
  const logFile = path.join(diagnostics, `512-${mode}-server.log`);
  const fd = openSync(logFile, 'a');
  try {
    studio = spawn(process.execPath, ['server/index.mjs'], {
      cwd: root,
      env: {
        ...process.env,
        PORT: String(port),
        ASTRA_PIXAL3D_BACKEND: 'windows-rocm',
        ASTRA_PIXAL3D_URL: engineUrl,
        ASTRA_COMFY_URL: 'http://127.0.0.1:8192',
      },
      windowsHide: true,
      stdio: ['ignore', fd, fd],
    });
  } finally { closeSync(fd); }

  let healthy = false;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await studioRequest('/api/health')).app === 'AstraForge') { healthy = true; break; }
    } catch { /* starting */ }
    await sleep(250);
  }
  if (!healthy) throw new Error('The isolated AstraForge instance did not start.');
  await studioRequest('/api/runtime/pixal3d/start', { method: 'POST', signal: AbortSignal.timeout(240000) });
  const baseline = await engineSample('baseline');

  const inputPath = path.join(root, 'data', 'runtime', 'amd-pixal3d-native-single-input.png');
  const form = new FormData();
  form.append('file', new Blob([readFileSync(inputPath)], { type: 'image/png' }), 'amd-pixal3d-native-lifecycle.png');
  const input = await studioRequest('/api/assets', { method: 'POST', body: form });
  const job = await studioRequest('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      imageId: input.id, engine: 'pixal3d', quality: 'draft', seed: 42,
      backgroundMode: 'alpha', hazeFilter: 'auto', vertexColors: false, uvTexture: false,
    }),
  });
  report.job = job.id;
  console.error(`Pixal3D ${mode} job: ${job.id}`);

  const deadline = Date.now() + (mode === 'cancel' ? 120000 : 300000);
  let cancelled = false;
  let finalJob = job;
  while (Date.now() < deadline) {
    finalJob = (await studioRequest('/api/jobs')).find((item) => item.id === job.id);
    if (!finalJob) throw new Error('The lifecycle job disappeared.');
    report.promptId = finalJob.promptId || report.promptId;
    let sample = null;
    try { sample = await engineSample(mode === 'cancel' && cancelled ? 'cancelling' : 'running'); } catch (error) {
      (report.telemetryErrors ||= []).push({ at: new Date().toISOString(), message: error.message });
    }
    if (sample && mode === 'cancel' && !cancelled && report.promptId && sample.running.includes(report.promptId)) {
      await studioRequest(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
      cancelled = true;
      report.cancelledAt = new Date().toISOString();
      console.error(`Cancellation requested for prompt ${report.promptId}`);
    }
    if (mode === 'complete' && finalJob.status === 'completed') break;
    if (['failed', 'cancelled'].includes(finalJob.status) && (mode === 'complete' || cancelled)) break;
    await sleep(1500);
  }
  if (mode === 'cancel' && !cancelled) throw new Error('The prompt did not reach the running state before the cancellation deadline.');
  if (mode === 'complete' && finalJob.status !== 'completed') throw new Error(finalJob.message || `Job ${finalJob.status}.`);

  const queueDeadline = Date.now() + 90000;
  for (;;) {
    let sample = null;
    try { sample = await engineSample('draining'); } catch (error) {
      (report.telemetryErrors ||= []).push({ at: new Date().toISOString(), message: error.message });
    }
    if (sample && !sample.running.length && !sample.pending.length) break;
    if (Date.now() > queueDeadline) throw new Error('The engine queue did not become idle within 90 seconds.');
    await sleep(1500);
  }
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await sleep(1000);
    try { await engineSample('settled'); } catch (error) {
      (report.telemetryErrors ||= []).push({ at: new Date().toISOString(), message: error.message });
    }
  }
  const final = [...report.samples].reverse().find((sample) => sample.label === 'settled');
  report.cleanup = cleanupVerdict(baseline, final);
  report.finalJobStatus = finalJob.status;
  if (!report.cleanup.verified) throw new Error(report.cleanup.reason || `VRAM was not released: ${JSON.stringify(report.cleanup)}`);
} catch (error) {
  failure = error;
} finally {
  writeFileSync(reportFile, JSON.stringify(report, null, 2) + '\n');
  await stopStudio();
  if (process.platform === 'win32') {
    try {
      const pid = Number(readFileSync(path.join(root, 'data', 'runtime', 'pixal3d.pid'), 'utf8'));
      if (Number.isInteger(pid) && pid > 0) spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    } catch { /* no owned runtime */ }
  }
}

console.log(JSON.stringify(report, null, 2));
if (failure) throw failure;
