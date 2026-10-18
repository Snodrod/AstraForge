// Blender self-tests of the forge steps (docs/FORGE-CONTRACT.md §10), wrapped for node:test.
// Lives in tests/integration/ so the fast `npm test` glob (tests/*.test.mjs) does not pick it up.
//
//   node --test tests/integration/blender.test.mjs
//   FORGE_TEST_MODULE=core node --test tests/integration/blender.test.mjs     (one module)
//   FORGE_TEST_MODULE=core,items FORGE_TEST_FILTER=real node --test ...       (several, -k filter)
//
// Blender: env ASTRA_BLENDER, else the default install path; the test is skipped when missing.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const RUNNER = join(ROOT, 'scripts', 'blender', 'tests', 'run_tests.py');
const DEFAULT_BLENDER = 'C:/Program Files/Blender Foundation/Blender 5.0/blender.exe';
const BLENDER = process.env.ASTRA_BLENDER || DEFAULT_BLENDER;
const TIMEOUT_MS = 40 * 60 * 1000;

export function parseTestResult(stdout) {
  const lines = String(stdout).split(/\r?\n/).filter((l) => l.startsWith('TESTRESULT '));
  if (!lines.length) return null;
  return JSON.parse(lines[lines.length - 1].slice('TESTRESULT '.length));
}

function runBlender(args) {
  return new Promise((resolvePromise) => {
    const child = spawn(BLENDER, args, {cwd: ROOT, windowsHide: true});
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill();
    }, TIMEOUT_MS);
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('error', (err) => {
      clearTimeout(timer);
      resolvePromise({code: -1, stdout, stderr: stderr + String(err), timedOut});
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolvePromise({code, stdout, stderr, timedOut});
    });
  });
}

const missing = !existsSync(BLENDER) ? `Blender not found at ${BLENDER} (set ASTRA_BLENDER)` : false;

test('Blender forge step self-tests pass', {skip: missing, timeout: TIMEOUT_MS + 60_000}, async (t) => {
  const args = ['-b', '--factory-startup', '--python', RUNNER, '--'];
  const modules = (process.env.FORGE_TEST_MODULE || '').split(',').map((s) => s.trim()).filter(Boolean);
  args.push(...modules);
  if (process.env.FORGE_TEST_FILTER) args.push('-k', process.env.FORGE_TEST_FILTER);
  const run = await runBlender(args);
  assert.equal(run.timedOut, false, 'Blender tests exceeded 40 minutes');
  const result = parseTestResult(run.stdout);
  assert.ok(result, `no TESTRESULT line; exit ${run.code}\n${run.stdout.slice(-4000)}\n${run.stderr.slice(-2000)}`);
  for (const item of result.tests) {
    t.diagnostic(`${item.status} ${item.module}.${item.test} ${item.seconds}s${item.error ? ' ' + item.error : ''}`);
  }
  t.diagnostic(`passed ${result.passed}, failed ${result.failed}, skipped ${result.skipped}, ${result.seconds}s`);
  const failed = result.tests.filter((x) => x.status === 'FAIL');
  assert.equal(result.failed, 0, failed.map((x) => `${x.module}.${x.test}: ${x.error}`).join('\n'));
  assert.equal(run.code, 0, `Blender exited with ${run.code}`);
  assert.ok(result.passed + result.skipped > 0, 'no tests were discovered');
});
