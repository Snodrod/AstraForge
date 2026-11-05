import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createAttempt, writeManifest } from '../scripts/native/attempt_store.mjs';

async function loadRunner() {
  return import('../scripts/native/run_native_belt_repair.mjs').catch(() => ({}));
}

test('belt repair CLI is views-only and supports a mask-only preparation run', async () => {
  const runner = await loadRunner();
  assert.equal(typeof runner.parseBeltRepairOptions, 'function');
  const options = runner.parseBeltRepairOptions([
    '--input', 'C:/source-attempt', '--output', 'C:/repair', '--prepare-only', 'true', '--debug', 'true',
  ]);
  assert.equal(options.input, 'C:/source-attempt');
  assert.equal(options.outputDir, 'C:/repair');
  assert.equal(options.prepareOnly, true);
  assert.equal(options.debug, true);
  assert.equal(options.seed, 42);
  assert.throws(() => runner.parseBeltRepairOptions(['--output', 'C:/repair']), /--input/);
  assert.throws(() => runner.parseBeltRepairOptions(['--input', 'C:/source', '--stage', 'shape']), /Unknown option/);
  assert.throws(() => runner.parseBeltRepairOptions(['--input', 'C:/source', '--stage', 'texture']), /Unknown option/);
  assert.throws(() => runner.parseBeltRepairOptions(['--input', 'C:/source', '--prepare-only', 'maybe']), /true or false/);
});

test('belt repair creates a fresh views attempt without overwriting the source', () => {
  const temporary = mkdtempSync(path.join(tmpdir(), 'astra-belt-attempt-'));
  const first = createAttempt(temporary, 'views');
  writeManifest(first.dir, { stage: 'views', status: 'success', marker: 'keep' });
  const second = createAttempt(temporary, 'views');
  assert.equal(first.id, 'attempt-001');
  assert.equal(second.id, 'attempt-002');
  assert.equal(JSON.parse(readFileSync(path.join(first.dir, 'manifest.json'), 'utf8')).marker, 'keep');
});

test('belt repair source validation accepts pending visual review but rejects failed attempts', async () => {
  const runner = await loadRunner();
  assert.equal(typeof runner.readRepairSource, 'function');
  const temporary = mkdtempSync(path.join(tmpdir(), 'astra-belt-source-'));
  const attempt = createAttempt(temporary, 'views');
  writeManifest(attempt.dir, { id: attempt.id, stage: 'views', status: 'error', artifacts: {} });
  assert.throws(() => runner.readRepairSource(attempt.dir), /did not complete successfully/);
  writeManifest(attempt.dir, { id: attempt.id, stage: 'views', status: 'success', artifacts: {} });
  const source = runner.readRepairSource(attempt.dir);
  assert.equal(source.manifest.id, attempt.id);
  assert.equal(source.review.status, 'pending');
});

test('belt repair refuses to start GPU work without a safe system RAM margin', async () => {
  const runner = await loadRunner();
  assert.equal(typeof runner.assertRepairMemoryBudget, 'function');
  const gib = 1024 ** 3;
  assert.doesNotThrow(() => runner.assertRepairMemoryBudget(13 * gib, 12 * gib));
  assert.throws(() => runner.assertRepairMemoryBudget(8 * gib, 12 * gib), /12 GiB/);
});

test('full belt repair checks RAM before creating an attempt, while mask preparation stays available', async () => {
  const source = readFileSync(new URL('../scripts/native/run_native_belt_repair.mjs', import.meta.url), 'utf8');
  const createAttempt = source.indexOf('const attempt = createAttempt(');
  const prepareReturn = source.indexOf('if (options.prepareOnly)', createAttempt);
  const memoryGuard = source.indexOf('if (!options.prepareOnly) assertRepairMemoryBudget();');
  assert.ok(createAttempt >= 0 && prepareReturn > createAttempt);
  assert.ok(memoryGuard >= 0 && memoryGuard < createAttempt, 'full repair must reject low RAM before creating an attempt, without blocking mask preparation');
});

test('belt repair rejects byte-identical donor views before compositing', async () => {
  const runner = await loadRunner();
  assert.equal(typeof runner.assertDistinctDonorViews, 'function');
  const { mkdirSync } = await import('node:fs');
  const temporary = mkdtempSync(path.join(tmpdir(), 'astra-belt-donor-'));
  mkdirSync(path.join(temporary, 'raw'));
  writeFileSync(path.join(temporary, 'raw', 'front.png'), 'same');
  writeFileSync(path.join(temporary, 'raw', 'back.png'), 'same');
  writeFileSync(path.join(temporary, 'raw', 'left.png'), 'left');
  writeFileSync(path.join(temporary, 'raw', 'right.png'), 'right');
  assert.throws(() => runner.assertDistinctDonorViews(temporary), /Duplicate donor views: back=front/);
  writeFileSync(path.join(temporary, 'raw', 'back.png'), 'back');
  assert.doesNotThrow(() => runner.assertDistinctDonorViews(temporary));
});
