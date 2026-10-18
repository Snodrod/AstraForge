// Blender runner protocol, timeouts, cancellation and queue — with a fake "Blender" (a Node script).
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { StepQueue, parseProtocolLine, progressOf, runBlenderStep, blenderInfo, lastErrorLine } from '../server/blender.mjs';

const tmp = mkdtempSync(path.join(tmpdir(), 'af-blender-'));
after(() => rmSync(tmp, { recursive: true, force: true, maxRetries: 3 }));
const fake = path.join(tmp, 'fake-blender.mjs');
writeFileSync(fake, `
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
const [step, reqPath] = process.argv.slice(2);
const req = JSON.parse(readFileSync(reqPath, 'utf8'));
const mode = req.params.mode || 'ok';
const out = (s) => process.stdout.write(s + '\\n');
mkdirSync(req.out_dir, { recursive: true });
out('Blender 5.0.1 (fake)');
out('Read prefs: nothing');
if (mode === 'ok' || mode === 'slow' || mode === 'checkfail') {
  if (mode === 'slow') writeFileSync(path.join(req.out_dir, 'start.txt'), String(Date.now()));
  out('AFLOG ' + JSON.stringify({ t: 0.1, msg: 'import', progress: 0.25 }));
  process.stderr.write('warning: something on stderr\\n');
  out('AFLOG ' + JSON.stringify({ t: 0.2, msg: 'frames', frame: 3, frames: 4 }));
  out('AFLOG {not json');
  const finish = () => {
    if (mode === 'slow') writeFileSync(path.join(req.out_dir, 'end.txt'), String(Date.now()));
    const res = mode === 'checkfail'
      ? { ok: false, step, outputs: {}, checks: [{ name: 'unweighted_vertices', ok: false, severity: 'error' }], error: 'checks failed: unweighted_vertices' }
      : { ok: true, step, outputs: { glb: path.join(req.out_dir, 'out.glb') }, stats: { tris: 12, echo: req.inputs }, checks: [], warnings: [] };
    writeFileSync(path.join(req.out_dir, 'result.json'), JSON.stringify(res));
    out('AFRESULT ' + JSON.stringify(res));
  };
  if (mode === 'slow') setTimeout(finish, 350); else finish();
} else if (mode === 'error') {
  out('AFERROR ' + JSON.stringify({ step, error: 'RuntimeError: no mesh', trace: 'Traceback...' }));
  process.exit(1);
} else if (mode === 'noresult') {
  out('done without protocol');
} else if (mode === 'pyerror') {
  // like a --python script that raises before af_run's handler: traceback on stderr, exit code 0
  process.stderr.write('Traceback (most recent call last):\\n  File "af_run.py", line 40, in <module>\\n');
  process.stderr.write('FileNotFoundError: [Errno 2] No such file or directory: request.json\\n');
  out('Blender quit');
} else if (mode === 'fileonly') {
  writeFileSync(path.join(req.out_dir, 'result.json'), JSON.stringify({ ok: true, step, outputs: {} }));
} else if (mode === 'hang') {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
  writeFileSync(path.join(req.out_dir, 'child.pid'), String(child.pid));
  out('AFLOG ' + JSON.stringify({ msg: 'started', progress: 0.1 }));
  setInterval(() => {}, 1000);
}
`);

const command = ({ step, requestPath }) => ({ command: process.execPath, args: [fake, step, requestPath] });
let n = 0;
const req = (params = {}, inputs = {}) => ({ inputs, params, out_dir: path.join(tmp, `out-${++n}`) });
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('protocol lines are parsed and progress normalised', () => {
  assert.deepEqual(parseProtocolLine('AFLOG {"msg":"x","progress":0.5}\r').data, { msg: 'x', progress: 0.5 });
  assert.equal(parseProtocolLine('AFRESULT {"ok":true}').type, 'result');
  assert.equal(parseProtocolLine('AFERROR {"error":"e"}').type, 'error');
  assert.equal(parseProtocolLine('AFLOG {broken').type, 'text');
  assert.equal(parseProtocolLine('Blender 5.0.1').type, 'text');
  assert.equal(progressOf({ progress: 0.5 }), 50);
  assert.equal(progressOf({ progress: 40 }), 40);
  assert.equal(progressOf({ frame: 1, frames: 4 }), 25);
  assert.equal(progressOf({ msg: 'no progress' }), null);
});

test('a successful step streams AFLOG, writes request.json and the log file, returns AFRESULT', async () => {
  const logs = [];
  const progress = [];
  const r = req({ mode: 'ok' }, { mesh: 'C:/x.glb' });
  const logDir = path.join(tmp, 'logs');
  const run = await runBlenderStep({
    step: 'inspect', request: r, jobId: 'job-1', logDir, command, queue: new StepQueue(1),
    onLog: (e) => logs.push(e), onProgress: (p) => progress.push(p),
  });
  assert.equal(run.result.ok, true);
  assert.deepEqual(run.result.stats.echo, { mesh: 'C:/x.glb' });
  assert.deepEqual(progress, [25, 75]);
  assert.ok(logs.some((l) => l.msg === 'import'), 'Russian AFLOG text survives UTF-8 decoding');
  const request = JSON.parse(readFileSync(path.join(r.out_dir, 'request.json'), 'utf8'));
  assert.equal(request.step, 'inspect');
  assert.equal(request.inputs.mesh, 'C:/x.glb');
  const log = readFileSync(path.join(logDir, 'inspect.log'), 'utf8');
  assert.match(log, /AFRESULT/);
  assert.match(log, /\[stderr\] warning/);
  assert.ok(run.logTail.length > 0 && run.logTail.length <= 40);
});

test('AFERROR, missing result and ok:false are reported distinctly', async () => {
  const queue = new StepQueue(1);
  await assert.rejects(runBlenderStep({ step: 'cleanup', request: req({ mode: 'error' }), command, queue, logDir: tmp }),
    (e) => e.code === 'step_error' && /no mesh/.test(e.message) && e.payload.trace === 'Traceback...');
  await assert.rejects(runBlenderStep({ step: 'cleanup', request: req({ mode: 'noresult' }), command, queue, logDir: tmp }),
    (e) => e.code === 'no_result' && /exited without a result/.test(e.message));
  await assert.rejects(runBlenderStep({ step: 'inspect', request: req({ mode: 'pyerror' }), command, queue, logDir: tmp }),
    (e) => e.code === 'no_result' && /\(code 0\): FileNotFoundError: \[Errno 2\]/.test(e.message), 'the Python error is in the message');
  assert.equal(lastErrorLine(['[stderr] Traceback (most recent call last):', '[stderr] RuntimeError: no mesh', 'Blender quit']), 'RuntimeError: no mesh');
  assert.equal(lastErrorLine(['Blender 5.0.1', 'AFLOG {}']), null);
  const fileOnly = await runBlenderStep({ step: 'cleanup', request: req({ mode: 'fileonly' }), command, queue, logDir: tmp });
  assert.equal(fileOnly.result.ok, true, 'falls back to result.json written during this run');
  const bad = await runBlenderStep({ step: 'rig_humanoid', request: req({ mode: 'checkfail' }), command, queue, logDir: tmp });
  assert.equal(bad.result.ok, false);
  assert.match(bad.result.error, /unweighted_vertices/);
  await assert.rejects(runBlenderStep({ step: '../evil', request: req(), command, queue }), (e) => e.code === 'bad_step');
  await assert.rejects(runBlenderStep({ step: 'inspect', request: { inputs: {}, params: {}, out_dir: 'relative' }, command, queue }), (e) => e.code === 'bad_request');
});

test('cancellation kills the whole process tree', async () => {
  const controller = new AbortController();
  const r = req({ mode: 'hang' });
  let started = false;
  const pending = runBlenderStep({
    step: 'render_sprites', request: r, command, queue: new StepQueue(1), logDir: tmp, signal: controller.signal,
    onLog: (e) => { if (e.msg === 'started') started = true; },
  });
  for (let i = 0; i < 100 && !started; i += 1) await sleep(50);
  assert.ok(started, 'fake Blender started');
  const childPid = Number(readFileSync(path.join(r.out_dir, 'child.pid'), 'utf8'));
  assert.ok(alive(childPid));
  controller.abort();
  await assert.rejects(pending, (e) => e.name === 'AbortError');
  for (let i = 0; i < 40 && alive(childPid); i += 1) await sleep(50);
  assert.equal(alive(childPid), false, 'grandchild process is killed with the tree');
});

test('timeouts stop the step', async () => {
  const r = req({ mode: 'hang' });
  await assert.rejects(runBlenderStep({ step: 'cleanup', request: r, command, queue: new StepQueue(1), logDir: tmp, timeoutSec: 0.6 }),
    (e) => e.code === 'timeout' && /time limit/.test(e.message));
  const childPid = Number(readFileSync(path.join(r.out_dir, 'child.pid'), 'utf8'));
  for (let i = 0; i < 40 && alive(childPid); i += 1) await sleep(50);
  assert.equal(alive(childPid), false);
});

test('the queue is FIFO with the configured concurrency; queued runs can be aborted', async () => {
  const queue = new StepQueue(1);
  const a = req({ mode: 'slow' });
  const b = req({ mode: 'slow' });
  const c = req({ mode: 'slow' });
  const controller = new AbortController();
  const pa = runBlenderStep({ step: 'inspect', request: a, command, queue, logDir: tmp });
  const pb = runBlenderStep({ step: 'inspect', request: b, command, queue, logDir: tmp });
  const pc = runBlenderStep({ step: 'inspect', request: c, command, queue, logDir: tmp, signal: controller.signal });
  for (let i = 0; i < 200 && queue.stats().queued < 2; i += 1) await sleep(10);
  assert.equal(queue.stats().running, 1);
  assert.equal(queue.stats().queued, 2);
  controller.abort();
  await assert.rejects(pc, (e) => e.name === 'AbortError');
  await Promise.all([pa, pb]);
  const endA = Number(readFileSync(path.join(a.out_dir, 'end.txt'), 'utf8'));
  const startB = Number(readFileSync(path.join(b.out_dir, 'start.txt'), 'utf8'));
  // Timestamps come from two processes; V8 clocks on Windows may differ by ~16 ms between
  // processes, while overlapping runs would differ by the full 350 ms run time.
  assert.ok(startB >= endA - 50, `second run starts after the first one ends (${startB - endA} ms)`);
  assert.equal(existsSync(path.join(c.out_dir, 'start.txt')), false, 'aborted queued run never started');

  const wide = new StepQueue(2);
  const d = req({ mode: 'slow' });
  const e = req({ mode: 'slow' });
  let done = false;
  let maxRunning = 0;
  const both = Promise.all([
    runBlenderStep({ step: 'inspect', request: d, command, queue: wide, logDir: tmp }),
    runBlenderStep({ step: 'inspect', request: e, command, queue: wide, logDir: tmp }),
  ]).finally(() => { done = true; });
  while (!done) {
    maxRunning = Math.max(maxRunning, wide.stats().running);
    await sleep(5);
  }
  await both;
  assert.equal(maxRunning, 2, 'concurrency 2 runs both at once');
});

test('GPU steps take the lock around the run and always release it', async () => {
  const events = [];
  const acquireGpu = async () => { events.push('acquire'); return { noop: false, abandoned: false, release: async () => { events.push('release'); } }; };
  await runBlenderStep({ step: 'render_sprites', request: req({ mode: 'ok' }), command, queue: new StepQueue(1), logDir: tmp, gpu: true, acquireGpu });
  await assert.rejects(runBlenderStep({ step: 'render_sprites', request: req({ mode: 'error' }), command, queue: new StepQueue(1), logDir: tmp, gpu: true, acquireGpu }));
  await runBlenderStep({ step: 'inspect', request: req({ mode: 'ok' }), command, queue: new StepQueue(1), logDir: tmp, gpu: false, acquireGpu });
  assert.deepEqual(events, ['acquire', 'release', 'acquire', 'release']);
});

test('a GPU step waits for the lock outside the queue, then goes ahead of waiting steps', async () => {
  const queue = new StepQueue(1);
  let grant;
  const granted = new Promise((resolve) => { grant = resolve; });
  const events = [];
  const acquireGpu = async () => {
    events.push('gpu-wait');
    await granted;
    events.push('gpu-locked');
    return { noop: false, abandoned: false, release: async () => { events.push('gpu-release'); } };
  };
  const gpuRun = runBlenderStep({ step: 'render_sprites', request: req({ mode: 'ok' }), command, queue, logDir: tmp, gpu: true, acquireGpu, onStart: () => events.push('gpu-start') });
  const cpu = await runBlenderStep({ step: 'inspect', request: req({ mode: 'ok' }), command, queue, logDir: tmp, onStart: () => events.push('cpu-start') });
  assert.equal(cpu.result.ok, true, 'a CPU step runs while the GPU step waits for the mutex');
  assert.deepEqual(events, ['gpu-wait', 'cpu-start']);
  grant();
  await gpuRun;
  assert.deepEqual(events, ['gpu-wait', 'cpu-start', 'gpu-locked', 'gpu-start', 'gpu-release']);

  // holding the mutex, the GPU step queues ahead of ordinary waiting steps
  const order = [];
  const free = async () => ({ noop: false, abandoned: false, release: async () => {} });
  const slow = runBlenderStep({ step: 'inspect', request: req({ mode: 'slow' }), command, queue, logDir: tmp, onStart: () => order.push('slow') });
  const waiting = runBlenderStep({ step: 'inspect', request: req({ mode: 'ok' }), command, queue, logDir: tmp, onStart: () => order.push('cpu') });
  const gpu = runBlenderStep({ step: 'render_sprites', request: req({ mode: 'ok' }), command, queue, logDir: tmp, gpu: true, acquireGpu: free, onStart: () => order.push('gpu') });
  await Promise.all([slow, waiting, gpu]);
  assert.deepEqual(order, ['slow', 'gpu', 'cpu']);
});

test('a queue slot can be given back and taken again ahead of the queue', async () => {
  const q = new StepQueue(1);
  const order = [];
  let resume;
  const paused = new Promise((resolve) => { resume = resolve; });
  const a = q.run(async (slot) => {
    order.push('a1');
    slot.release();
    await paused;
    assert.equal(await slot.reacquire(), true);
    order.push('a2');
  });
  await q.run(async () => { order.push('b'); }); // runs while a has given its place back
  const c = q.run(async () => { order.push('c'); await sleep(30); });
  resume(); // a wants its place back: after c, but before d
  const d = q.run(async () => { order.push('d'); });
  await Promise.all([a, c, d]);
  assert.deepEqual(order, ['a1', 'b', 'c', 'a2', 'd']);
  assert.equal(q.stats().running, 0);

  // an aborted re-entry resolves false and leaks nothing
  const controller = new AbortController();
  let block;
  let fStarted;
  const fRunning = new Promise((resolve) => { fStarted = resolve; });
  const e = q.run(async (slot) => {
    slot.release(); // f takes the place
    await fRunning;
    const back = slot.reacquire(controller.signal); // queued behind f
    controller.abort();
    assert.equal(await back, false);
  });
  const f = q.run(() => new Promise((resolve) => { fStarted(); block = resolve; }));
  await e;
  assert.equal(q.stats().running, 1, 'only f holds a place');
  block();
  await f;
  await sleep(0); // the queue frees the place in a finally right after the task settles
  assert.equal(q.stats().running, 0);
  assert.equal(q.stats().queued, 0);
});

test('a missing Blender is reported in English without throwing', async () => {
  const info = await blenderInfo({ path: path.join(tmp, 'no-blender.exe') });
  assert.equal(info.ok, false);
  assert.match(info.error, /Blender not found/);
});
