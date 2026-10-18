// GPU hand-over between the Hunyuan (8190) and Pixal3D (8191) ComfyUI engines: server/gpu-handover.mjs. No network, no GPU, no PowerShell:
// every ComfyUI is a function that answers like /queue, /free, /system_stats.
import test from 'node:test';
import assert from 'node:assert/strict';
import { handoverTo, acquireEngineGpu, withEngineGpu, freeIfIdle, engineQueue, engineUrls } from '../server/gpu-handover.mjs';

const H = 'http://127.0.0.1:8190', P = 'http://127.0.0.1:8191';
const urls = { hunyuan: H, pixal3d: P };
const ok = (body) => ({ ok: true, status: 200, json: async () => body });

/** A pretend ComfyUI pair. engines[url] = {up, busyPolls, vram[]}; every request is logged. */
function fakeComfy(engines) {
  const log = [];
  const fetchFn = async (url, options = {}) => {
    const u = new URL(url), base = u.origin, e = engines[base];
    log.push({ base, path: u.pathname, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined });
    if (!e || !e.up) throw new Error('ECONNREFUSED');
    if (u.pathname === '/queue') {
      if (e.notComfy) return ok({ hello: 'world' });
      const busy = e.busyPolls > 0;
      if (busy) e.busyPolls -= 1;
      return ok({ queue_running: busy ? [[1, 'job']] : [], queue_pending: [] });
    }
    if (u.pathname === '/free') { e.freed = (e.freed || 0) + 1; return ok({}); }
    if (u.pathname === '/system_stats') return ok({ devices: [{ torch_vram_total: e.vram?.length ? e.vram.shift() : 0 }] });
    throw new Error('unexpected ' + u.pathname);
  };
  return { fetchFn, log, engines };
}
const noSleep = async () => {};

test('hand-over: an idle other engine is told to unload its models, the target engine is never touched', async () => {
  const c = fakeComfy({ [H]: { up: true }, [P]: { up: true } });
  const report = await handoverTo('pixal3d', { urls, fetchFn: c.fetchFn, sleep: noSleep });
  assert.deepEqual(report.freed, ['hunyuan']);
  assert.equal(report.waited, false);
  const free = c.log.filter((r) => r.path === '/free');
  assert.equal(free.length, 1);
  assert.equal(free[0].base, H);
  assert.deepEqual(free[0].body, { unload_models: true, free_memory: true });
  assert.equal(c.log.some((r) => r.base === P), false, 'Pixal3D itself must not be queried when it is the target');
  // and the other way round
  const c2 = fakeComfy({ [H]: { up: true }, [P]: { up: true } });
  const back = await handoverTo('hunyuan', { urls, fetchFn: c2.fetchFn, sleep: noSleep });
  assert.deepEqual(back.freed, ['pixal3d']);
  assert.equal(c2.log.filter((r) => r.path === '/free')[0].base, P);
});

test('hand-over: an engine that is not running (or is not a ComfyUI) is skipped without errors', async () => {
  const down = fakeComfy({ [H]: { up: false }, [P]: { up: true } });
  const r1 = await handoverTo('pixal3d', { urls, fetchFn: down.fetchFn, sleep: noSleep });
  assert.deepEqual(r1.freed, []);
  assert.deepEqual(r1.skipped, ['hunyuan']);
  assert.equal(down.log.some((r) => r.path === '/free'), false);
  const stranger = fakeComfy({ [H]: { up: true, notComfy: true }, [P]: { up: true } });
  const r2 = await handoverTo('pixal3d', { urls, fetchFn: stranger.fetchFn, sleep: noSleep });
  assert.deepEqual(r2.freed, [], 'something that does not answer like /queue of ComfyUI is left alone');
  assert.equal(stranger.log.some((r) => r.path === '/free'), false);
});

test('hand-over: a busy engine is waited for, never interrupted, and only freed once its queue is empty', async () => {
  const c = fakeComfy({ [H]: { up: true, busyPolls: 3 }, [P]: { up: true } });
  const waits = [], sleeps = [];
  const report = await handoverTo('pixal3d', { urls, fetchFn: c.fetchFn, sleep: async (ms) => { sleeps.push(ms); }, onWait: (t) => waits.push(t), pollMs: 777 });
  assert.equal(report.waited, true);
  assert.equal(waits.length, 1, 'the wait message is announced once');
  assert.match(waits[0], /Hunyuan3D/);
  assert.match(waits[0], /not interrupting/);
  assert.ok(sleeps.filter((ms) => ms === 777).length >= 3);
  const firstFree = c.log.findIndex((r) => r.path === '/free');
  const lastBusyQueue = c.log.map((r, i) => (r.path === '/queue' ? i : -1)).filter((i) => i >= 0).slice(-1)[0];
  assert.ok(firstFree > lastBusyQueue - 0, '/free comes after the last queue poll that still showed work');
  assert.equal(c.log.some((r) => r.path === '/interrupt' || (r.path === '/queue' && r.method === 'POST')), false, 'a running job is never interrupted or deleted');
  assert.equal(c.engines[H].freed, 1);
});

test('hand-over: cancelling while waiting stops the wait and frees nothing; a stuck engine ends with a Russian error', async () => {
  const c = fakeComfy({ [H]: { up: true, busyPolls: 1e9 }, [P]: { up: true } });
  const controller = new AbortController();
  let n = 0;
  const sleep = async () => { if (++n === 2) controller.abort(); };
  await assert.rejects(handoverTo('pixal3d', { urls, fetchFn: c.fetchFn, sleep, signal: controller.signal }), (e) => e.name === 'AbortError');
  assert.equal(c.log.some((r) => r.path === '/free'), false);

  const c2 = fakeComfy({ [H]: { up: true, busyPolls: 1e9 }, [P]: { up: true } });
  await assert.rejects(handoverTo('pixal3d', { urls, fetchFn: c2.fetchFn, sleep: noSleep, maxWaitMs: -1 }), /Hunyuan3D has been busy too long/);
  assert.equal(c2.log.some((r) => r.path === '/free'), false);
});

test('hand-over: waits until the other engine really gave the memory back (torch VRAM drops), but only for a bounded time', async () => {
  const gb = 1024 ** 3;
  const c = fakeComfy({ [H]: { up: true, vram: [8 * gb, 8 * gb, 6 * gb, 0.4 * gb] }, [P]: { up: true } });
  await handoverTo('pixal3d', { urls, fetchFn: c.fetchFn, sleep: noSleep });
  assert.equal(c.engines[H].vram.length, 0, 'it kept polling /system_stats until the value fell below half');
  const stuck = fakeComfy({ [H]: { up: true, vram: Array(500).fill(8 * gb) }, [P]: { up: true } });
  let t = 0; const realNow = Date.now;
  Date.now = () => (t += 2500);
  try { await handoverTo('pixal3d', { urls, fetchFn: stuck.fetchFn, sleep: noSleep, settleMs: 8000 }); } finally { Date.now = realNow; }
  assert.ok(stuck.engines[H].vram.length > 400, 'the settle wait gave up after settleMs instead of polling for ever');
});

test('acquireEngineGpu: the hand-over runs INSIDE the lock; a failed hand-over releases the lock', async () => {
  const events = [];
  const acquire = async () => { events.push('lock'); return { release: async () => { events.push('release'); } }; };
  const c = fakeComfy({ [H]: { up: true }, [P]: { up: true } });
  const fetchFn = async (url, o) => { if (String(url).endsWith('/free')) events.push('free'); return c.fetchFn(url, o); };
  const lock = await acquireEngineGpu('pixal3d', { acquire, urls, fetchFn, sleep: noSleep });
  assert.deepEqual(events, ['lock', 'free']);
  assert.deepEqual(lock.handover.freed, ['hunyuan']);
  await lock.release();
  assert.deepEqual(events, ['lock', 'free', 'release']);

  events.length = 0;
  const busy = fakeComfy({ [H]: { up: true, busyPolls: 1e9 }, [P]: { up: true } });
  await assert.rejects(acquireEngineGpu('pixal3d', { acquire, urls, fetchFn: busy.fetchFn, sleep: noSleep, maxWaitMs: -1 }), /has been busy too long/);
  assert.deepEqual(events, ['lock', 'release'], 'no lock is leaked when the hand-over fails');
});

test('two jobs on different engines never generate at the same time (shared lock, hand-over inside it)', async () => {
  let held = false, maxHeld = 0, running = 0;
  const waiters = [];
  const acquire = () => new Promise((resolve) => {
    const take = () => { held = true; resolve({ release: async () => { held = false; waiters.shift()?.(); } }); };
    if (!held) take(); else waiters.push(take);
  });
  const comfy = fakeComfy({ [H]: { up: true }, [P]: { up: true } });
  const job = (engine, ms) => withEngineGpu(engine, async () => {
    running += 1; maxHeld = Math.max(maxHeld, running);
    await new Promise((r) => setTimeout(r, ms));
    running -= 1;
    return engine;
  }, { acquire, urls, fetchFn: comfy.fetchFn, sleep: noSleep });
  const done = await Promise.all([job('pixal3d', 25), job('hunyuan', 10), job('pixal3d', 5), job('hunyuan', 5)]);
  assert.deepEqual(done, ['pixal3d', 'hunyuan', 'pixal3d', 'hunyuan']);
  assert.equal(maxHeld, 1, 'at most one engine generates at a time');
  assert.equal(comfy.engines[H].freed + comfy.engines[P].freed, 4, 'every start unloaded the other engine');
});

test('freeIfIdle and engineQueue: only an idle ComfyUI is told to free memory', async () => {
  const idle = fakeComfy({ [P]: { up: true } });
  assert.equal(await freeIfIdle(P, idle.fetchFn), true);
  const busy = fakeComfy({ [P]: { up: true, busyPolls: 5 } });
  assert.equal(await freeIfIdle(P, busy.fetchFn), false);
  assert.equal(busy.log.some((r) => r.path === '/free'), false);
  assert.equal(await freeIfIdle(P, fakeComfy({ [P]: { up: false } }).fetchFn), false);
  assert.deepEqual(await engineQueue(P, fakeComfy({ [P]: { up: true, busyPolls: 1 } }).fetchFn), { running: 1, pending: 0 });
  assert.equal(await engineQueue(P, fakeComfy({ [P]: { up: true, notComfy: true } }).fetchFn), null);
});

test('engineUrls: Hunyuan 8190 and Pixal3D 8191 by default, overridable', () => {
  const d = engineUrls({}, {});
  assert.equal(d.hunyuan, 'http://127.0.0.1:8190');
  assert.equal(d.pixal3d, 'http://127.0.0.1:8191');
  const e = engineUrls({ hunyuan: 'http://127.0.0.1:9000' }, { ASTRA_PIXAL3D_URL: 'http://127.0.0.1:9191' });
  assert.equal(e.hunyuan, 'http://127.0.0.1:9000');
  assert.equal(e.pixal3d, 'http://127.0.0.1:9191');
});
