import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

const MIB = 1024 * 1024;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const finite = (values) => values.filter((value) => Number.isFinite(value));
const maximum = (values) => finite(values).length ? Math.max(...finite(values)) : null;

export function percentile(values, fraction) {
  const sorted = finite(values).sort((a, b) => a - b);
  if (!sorted.length) return null;
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
}

function cpuTimes() {
  return os.cpus().reduce((total, cpu) => ({
    idle: total.idle + cpu.times.idle,
    total: total.total + Object.values(cpu.times).reduce((sum, value) => sum + value, 0),
  }), { idle: 0, total: 0 });
}

export function cpuPercentBetween(previous, current) {
  if (!previous || !current) return null;
  const total = current.total - previous.total;
  const idle = current.idle - previous.idle;
  if (!(total > 0)) return null;
  return Math.max(0, Math.min(100, (1 - idle / total) * 100));
}

export function summarizeNativeSamples(samples) {
  const postReady = samples.filter((sample) => sample.postReady);
  const heartbeats = postReady.map((sample) => sample.studio?.latencyMs);
  const engineReadyIndex = samples.findIndex((sample) => sample.engine?.status === 200);
  const phases = {};
  for (const sample of samples) {
    const name = sample.phase || 'unknown';
    const phase = phases[name] ||= { samples: 0 };
    phase.samples += 1;
    phase.maxCpuPercent = maximum([phase.maxCpuPercent, sample.system?.cpuPercent]);
    phase.maxRamUsedMb = maximum([phase.maxRamUsedMb, sample.system?.ramUsedMb]);
    phase.maxDeviceUsedMb = maximum([phase.maxDeviceUsedMb, sample.engine?.deviceUsedMb]);
    phase.maxHeartbeatMs = maximum([phase.maxHeartbeatMs, sample.studio?.latencyMs]);
  }
  return {
    samples: samples.length,
    postReadySamples: postReady.length,
    heartbeatMs: { p50: percentile(heartbeats, 0.5), p95: percentile(heartbeats, 0.95), max: maximum(heartbeats) },
    heartbeatStalls250Ms: finite(heartbeats).filter((value) => value > 250).length,
    heartbeatStalls1000Ms: finite(heartbeats).filter((value) => value > 1000).length,
    postReadyErrors: postReady.filter((sample) => sample.studio?.error || sample.engine?.error || sample.jobError).length,
    errors: {
      studio: postReady.filter((sample) => sample.studio?.error).length,
      jobs: postReady.filter((sample) => sample.jobError).length,
      engine: postReady.filter((sample) => sample.engine?.error).length,
    },
    engineStartupErrors: samples.filter((sample, index) => (engineReadyIndex < 0 || index < engineReadyIndex) && sample.engine?.error).length,
    engineReadyErrors: engineReadyIndex < 0 ? 0 : samples.filter((sample, index) => index > engineReadyIndex && sample.postReady && sample.engine?.error).length,
    maxCpuPercent: maximum(samples.map((sample) => sample.system?.cpuPercent)),
    maxRamUsedMb: maximum(samples.map((sample) => sample.system?.ramUsedMb)),
    maxDeviceUsedMb: maximum(samples.map((sample) => sample.engine?.deviceUsedMb)),
    phases,
  };
}

async function fetchJson(fetchFn, url, headers = {}) {
  const started = performance.now();
  try {
    const response = await fetchFn(url, { headers, signal: AbortSignal.timeout(2500) });
    const data = await response.json();
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return { latencyMs: performance.now() - started, status: response.status, error: null, data };
  } catch (error) {
    return { latencyMs: performance.now() - started, status: null, error: error.message, data: null };
  }
}

const mib = (bytes) => Number.isFinite(bytes) ? bytes / MIB : null;

export function createNativeMonitor(options) {
  const {
    studioUrl, engineUrl, jsonlFile, summaryFile, intervalMs = 1000,
    fetchFn = fetch, sampleFn = null,
  } = options;
  let running = false;
  let readySeen = false;
  let loopPromise = null;
  let previousCpu = null;
  const samples = [];

  async function sampleNative() {
    const [health, jobs, stats] = await Promise.all([
      fetchJson(fetchFn, studioUrl + '/api/health'),
      fetchJson(fetchFn, studioUrl + '/api/jobs', { 'x-astra-client': 'studio' }),
      fetchJson(fetchFn, engineUrl + '/system_stats'),
    ]);
    if (health.data?.app === 'AstraForge') readySeen = true;
    const jobList = Array.isArray(jobs.data) ? jobs.data : [];
    const job = jobList.find((item) => item.engine === 'pixal3d' && item.status === 'running')
      || jobList.find((item) => item.engine === 'pixal3d') || null;
    const device = stats.data?.devices?.[0] || {};
    const currentCpu = cpuTimes();
    const sample = {
      at: new Date().toISOString(),
      postReady: readySeen,
      phase: job?.message?.split(' · ')[0] || (readySeen ? 'idle' : 'starting'),
      system: {
        cpuPercent: cpuPercentBetween(previousCpu, currentCpu),
        ramUsedMb: mib(os.totalmem() - os.freemem()),
        ramTotalMb: mib(os.totalmem()),
      },
      studio: {
        ready: health.data?.app === 'AstraForge', latencyMs: health.latencyMs,
        status: health.status, error: health.error,
      },
      job: job ? { id: job.id, status: job.status, progress: job.progress ?? null, message: job.message || '' } : null,
      jobError: jobs.error,
      engine: {
        latencyMs: stats.latencyMs, status: stats.status, error: stats.error,
        deviceUsedMb: mib(device.vram_total - device.vram_free),
        torchReservedMb: mib(device.torch_vram_total),
        torchAllocatedMb: mib(device.torch_vram_total - device.torch_vram_free),
        running: stats.data?.queue_running?.length ?? null,
        pending: stats.data?.queue_pending?.length ?? null,
      },
    };
    previousCpu = currentCpu;
    return sample;
  }

  async function loop() {
    while (running) {
      let sample;
      try {
        sample = await (sampleFn ? sampleFn() : sampleNative());
      } catch (error) {
        sample = {
          at: new Date().toISOString(), postReady: readySeen, phase: 'monitor-error',
          system: {}, studio: { ready: false, error: error.message }, engine: { error: error.message }, jobError: error.message,
        };
      }
      samples.push(sample);
      appendFileSync(jsonlFile, JSON.stringify(sample) + '\n');
      if (running) await sleep(intervalMs);
    }
  }

  return {
    async start() {
      if (running) return;
      mkdirSync(path.dirname(jsonlFile), { recursive: true });
      writeFileSync(jsonlFile, '');
      running = true;
      loopPromise = loop();
    },
    async stop() {
      running = false;
      if (loopPromise) await loopPromise;
      const summary = summarizeNativeSamples(samples);
      writeFileSync(summaryFile, JSON.stringify(summary, null, 2) + '\n');
      return summary;
    },
  };
}
