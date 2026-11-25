import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import {
  cpuPercentBetween, percentile, summarizeNativeSamples, createNativeMonitor,
} from '../scripts/native/native_monitor.mjs';

test('native monitor computes CPU deltas and nearest-rank percentiles', () => {
  assert.equal(cpuPercentBetween({ idle: 100, total: 200 }, { idle: 150, total: 300 }), 50);
  assert.equal(cpuPercentBetween(null, { idle: 150, total: 300 }), null);
  assert.equal(cpuPercentBetween({ idle: 100, total: 200 }, { idle: 100, total: 200 }), null);
  assert.equal(percentile([], 0.95), null);
  assert.equal(percentile([100, 300, 1200], 0.5), 300);
  assert.equal(percentile([100, 300, 1200], 0.95), 1200);
});

test('native monitor summary excludes startup errors and aggregates stalls and phases', () => {
  const sample = (postReady, phase, heartbeat, cpu, ram, vram, error = null) => ({
    at: '2026-11-04T00:00:00.000Z', postReady, phase,
    system: { cpuPercent: cpu, ramUsedMb: ram },
    studio: { ready: !error, latencyMs: heartbeat, error },
    engine: { deviceUsedMb: vram, error: null },
  });
  const summary = summarizeNativeSamples([
    sample(false, 'starting', 2500, 5, 1000, null, 'connecting'),
    sample(true, 'Building', 100, 40, 5000, 8000),
    sample(true, 'Building', 300, 90, 7000, 12000),
    sample(true, 'Painting', 1200, 60, 6500, 11000, 'timeout'),
  ]);
  assert.equal(summary.samples, 4);
  assert.equal(summary.postReadySamples, 3);
  assert.deepEqual(summary.heartbeatMs, { p50: 300, p95: 1200, max: 1200 });
  assert.equal(summary.heartbeatStalls250Ms, 2);
  assert.equal(summary.heartbeatStalls1000Ms, 1);
  assert.equal(summary.postReadyErrors, 1);
  assert.equal(summary.maxCpuPercent, 90);
  assert.equal(summary.maxRamUsedMb, 7000);
  assert.equal(summary.maxDeviceUsedMb, 12000);
  assert.deepEqual(summary.phases.Building, {
    samples: 2, maxCpuPercent: 90, maxRamUsedMb: 7000, maxDeviceUsedMb: 12000, maxHeartbeatMs: 300,
  });
});

test('native monitor separates engine startup failures from post-ready failures', () => {
  const sample = (status, error) => ({
    postReady: true, phase: 'test', system: {}, studio: { latencyMs: 1, error: null },
    engine: { status, error }, jobError: null,
  });
  const summary = summarizeNativeSamples([
    sample(null, 'engine starting'), sample(200, null), sample(null, 'timeout'),
  ]);
  assert.equal(summary.engineStartupErrors, 1);
  assert.equal(summary.engineReadyErrors, 1);
  assert.deepEqual(summary.errors, { studio: 0, jobs: 0, engine: 2 });
});

test('native monitor never overlaps samples and preserves JSONL evidence', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'astra-native-monitor-'));
  const jsonlFile = path.join(dir, 'samples.jsonl');
  const summaryFile = path.join(dir, 'summary.json');
  let active = 0;
  let maxActive = 0;
  let calls = 0;
  const monitor = createNativeMonitor({
    jsonlFile, summaryFile, intervalMs: 1,
    sampleFn: async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 15));
      active -= 1;
      calls += 1;
      return {
        at: new Date().toISOString(), postReady: true, phase: 'test',
        system: { cpuPercent: 1, ramUsedMb: 2 },
        studio: { ready: true, latencyMs: 3, error: null },
        engine: { deviceUsedMb: 4, error: null },
      };
    },
  });
  await monitor.start();
  await new Promise((resolve) => setTimeout(resolve, 55));
  await monitor.stop();
  assert.equal(maxActive, 1);
  assert.ok(calls >= 2);
  const lines = readFileSync(jsonlFile, 'utf8').trim().split('\n');
  assert.equal(lines.length, calls);
  assert.equal(JSON.parse(lines[0]).phase, 'test');
  assert.equal(JSON.parse(readFileSync(summaryFile, 'utf8')).samples, calls);
});

test('native monitor supports standalone ComfyUI without fabricated studio health', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'astra-engine-monitor-'));
  const jsonlFile = path.join(dir, 'samples.jsonl');
  const summaryFile = path.join(dir, 'summary.json');
  const urls = [];
  const monitor = createNativeMonitor({
    engineUrl: 'http://127.0.0.1:8195', jsonlFile, summaryFile, intervalMs: 1,
    fetchFn: async (url) => {
      urls.push(url);
      return {
        ok: true, status: 200,
        json: async () => ({
          devices: [{ vram_total: 16 * 1024 ** 3, vram_free: 8 * 1024 ** 3 }],
          queue_running: [{}], queue_pending: [],
        }),
      };
    },
  });
  await monitor.start();
  await new Promise((resolve) => setTimeout(resolve, 10));
  const summary = await monitor.stop();
  assert.ok(urls.length > 0);
  assert.ok(urls.every((url) => url === 'http://127.0.0.1:8195/system_stats'));
  const sample = JSON.parse(readFileSync(jsonlFile, 'utf8').trim().split('\n')[0]);
  assert.equal(sample.postReady, true);
  assert.equal(sample.studio, null);
  assert.equal(sample.jobError, null);
  assert.equal(sample.engine.deviceUsedMb, 8192);
  assert.ok(summary.postReadySamples > 0);
  assert.ok(summary.engineLatencyMs.max >= 0);
  assert.equal(summary.maxDeviceUsedMb, 8192);
});
