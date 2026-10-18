// /api/forge routes behind the studio security middleware, plus the CLI in HTTP and --direct modes.
// Uses an isolated Express app on a random port and a stub step runner (no Blender, no real data/).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, promises as fs, existsSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import { ForgeEngine } from '../server/forge-pipeline.mjs';
import { registerForge } from '../server/forge.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CLI = path.join(ROOT, 'scripts', 'forge.mjs');
const GLB = Buffer.concat([Buffer.from('glTF'), Buffer.from([2, 0, 0, 0, 20, 0, 0, 0]), Buffer.alloc(8)]);
const FBX = Buffer.concat([Buffer.from('Kaydara FBX Binary  \0'), Buffer.alloc(32)]);
const dir = mkdtempSync(path.join(tmpdir(), 'af-routes-'));
let server;
let base;
let engine;
const cliDirs = [];

/** GLB with only a JSON chunk: two thruster sockets for the ship presets (fx mask socket_thruster_*). */
function shipGlb() {
  let body = Buffer.from(JSON.stringify({ asset: { version: '2.0' }, nodes: [
    { name: 'ship' },
    { name: 'socket_thruster_main_l', extras: { game_socket: 'socket_thruster_main_l' } },
    { name: 'socket_thruster_main_r', extras: { game_socket: 'socket_thruster_main_r' } },
  ] }));
  body = Buffer.concat([body, Buffer.alloc((4 - (body.length % 4)) % 4, 0x20)]);
  const head = Buffer.alloc(20);
  head.write('glTF', 0, 'latin1');
  head.writeUInt32LE(2, 4);
  head.writeUInt32LE(20 + body.length, 8);
  head.writeUInt32LE(body.length, 12);
  head.writeUInt32LE(0x4e4f534a, 16);
  return Buffer.concat([head, body]);
}

async function stubRun(o) {
  o.onStart?.();
  await fs.mkdir(o.request.out_dir, { recursive: true });
  const outputs = {};
  if (o.step === 'inspect') {
    outputs.preview = path.join(o.request.out_dir, 'preview.png');
    await fs.writeFile(outputs.preview, 'png');
  } else if (o.step !== 'clip_library') {
    outputs.glb = path.join(o.request.out_dir, 'out.glb');
    outputs.blend = path.join(o.request.out_dir, 'out.blend');
    await fs.writeFile(outputs.glb, o.step === 'ship_gen' ? shipGlb() : GLB);
    await fs.writeFile(outputs.blend, 'blend');
  }
  return { result: { ok: true, step: o.step, outputs, stats: {}, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
}

before(async () => {
  engine = new ForgeEngine({
    libraryDir: path.join(dir, 'library'), runtimeDir: path.join(dir, 'runtime'),
    studioAssetsDir: path.join(dir, 'studio', 'assets'), studioJobsDir: path.join(dir, 'studio', 'jobs'),
    runStep: stubRun, persistIntervalMs: 0,
    blenderInfo: () => ({ path: 'C:/fake/blender.exe', ok: true, version: '5.0.1' }),
    godotInfo: () => ({ path: null, ok: false, version: null, error: 'Godot not found.' }),
  });
  const app = express();
  // same guard as server/index.mjs
  app.use((req, res, next) => {
    const host = req.headers.host?.split(':')[0];
    if (!['127.0.0.1', 'localhost', '['].includes(host)) return res.status(403).json({ error: 'Invalid Host' });
    if (req.path.startsWith('/api') && req.headers.origin && req.headers.origin !== `http://${req.headers.host}`) return res.status(403).json({ error: 'Invalid request origin' });
    if (!['GET', 'HEAD'].includes(req.method) && req.path.startsWith('/api') && req.headers['x-astra-client'] !== 'studio') return res.status(403).json({ error: 'The request must be sent from the studio.' });
    next();
  });
  app.use(express.json({ limit: '90mb' }));
  registerForge(app, null, { engine });
  await new Promise((resolve) => { server = app.listen(0, '127.0.0.1', resolve); });
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server?.closeAllConnections?.();
  await new Promise((resolve) => server.close(resolve));
  for (const d of [dir, ...cliDirs]) await fs.rm(d, { recursive: true, force: true }).catch(() => {});
});

const H = { 'x-astra-client': 'studio' };
async function api(method, url, body, headers = H) {
  const init = { method, headers: { ...headers } };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers['content-type'] = 'application/json'; }
  const res = await fetch(base + url, init);
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { json = null; }
  return { status: res.status, json, text, headers: res.headers };
}
async function waitJob(jobId) {
  for (let i = 0; i < 200; i += 1) {
    const { json } = await api('GET', `/api/forge/jobs/${jobId}`);
    if (['completed', 'failed', 'cancelled'].includes(json.status)) return json;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error('job did not finish');
}
function runCli(args, env = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [CLI, ...args], { cwd: ROOT, env: { ...process.env, ...env }, windowsHide: true });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('registries and status are served', async () => {
  const steps = await api('GET', '/api/forge/steps');
  assert.equal(steps.status, 200);
  assert.equal(steps.json.length, 28, '19 + bone_kit + replace_part + kitbash + place_parts + motion_style + paint_from_concept + extract_props + assemble_parts');
  assert.ok(['bone_kit', 'replace_part', 'kitbash', 'place_parts', 'motion_style', 'extract_props'].every((n) => steps.json.some((s) => s.step === n)));
  const ms = steps.json.find((s) => s.step === 'motion_style');
  assert.deepEqual(ms.inputs.slots, ['blend'], 'motion_style reads the animated .blend of retarget');
  assert.deepEqual(ms.outputs, ['glb', 'blend', 'clips', 'preview']);
  const pp = steps.json.find((s) => s.step === 'place_parts');
  assert.deepEqual(pp.inputs.slots, ['mesh', 'blend', 'part']);
  assert.equal(pp.params.part_asset.server, true, 'GET /steps lists the server-filled part asset like replace_part');
  assert.ok(steps.json.every((s) => s.step && s.title && s.params));
  const presets = await api('GET', '/api/forge/presets');
  assert.ok(presets.json.some((p) => p.id === 'ship_procedural'));
  const status = await api('GET', '/api/forge/status');
  assert.deepEqual(status.json.blender, { path: 'C:/fake/blender.exe', ok: true, version: '5.0.1' });
  assert.equal(status.json.godot.ok, false);
  assert.deepEqual(Object.keys(status.json.queue).slice(0, 2), ['running', 'queued']);
  assert.equal(status.json.library.clips, 0);
  assert.ok('held' in status.json.gpuLock && status.json.gpuLock.name);
});

test('mutating requests need the studio header; errors are {error, code}', async () => {
  const denied = await api('POST', '/api/forge/assets', { kind: 'item', source: { type: 'params' } }, {});
  assert.equal(denied.status, 403);
  const bad = await api('POST', '/api/forge/assets', { kind: 'unicorn', source: { type: 'params' } });
  assert.equal(bad.status, 400);
  assert.equal(bad.json.code, 'bad_kind');
  assert.match(bad.json.error, /Asset kind/);
  const missing = await api('GET', '/api/forge/assets/00000000-0000-4000-8000-000000000000');
  assert.equal(missing.status, 404);
  assert.equal(missing.json.code, 'not_found');
  const nope = await api('GET', '/api/forge/nothing-here');
  assert.equal(nope.status, 404);
  assert.equal(nope.json.code, 'not_found');
  // a malformed JSON body fails in the app-wide express.json, before the forge router
  const broken = await fetch(base + '/api/forge/assets', { method: 'POST', headers: { ...H, 'content-type': 'application/json' }, body: '{kind: oops' });
  assert.equal(broken.status, 400);
  const brokenJson = await broken.json();
  assert.equal(brokenJson.code, 'bad_json');
  assert.match(brokenJson.error, /not valid JSON/);
  // local paths in step params are refused over HTTP
  const item = (await api('POST', '/api/forge/assets', { kind: 'humanoid', source: { type: 'params' } })).json;
  const local = await api('POST', `/api/forge/assets/${item.id}/run`, { steps: [{ step: 'import_mixamo_rig', params: { texture_source: 'C:/Users/x/secret.glb' } }] });
  assert.equal(local.status, 400);
  assert.equal(local.json.code, 'no_local_paths');
});

test('upload → run → job polling → file download, with path guards', async () => {
  const form = new FormData();
  form.append('kind', 'humanoid');
  form.append('name', 'Wizard');
  form.append('file', new Blob([GLB]), 'mage.glb');
  const created = await api('POST', '/api/forge/assets', form);
  assert.equal(created.status, 201, created.text);
  const id = created.json.id;
  assert.equal(created.json.source.type, 'upload');
  assert.equal(created.json.source.file, 'source/source.glb');
  const uploads = path.join(engine.workDir, 'uploads');
  assert.deepEqual(existsSync(uploads) ? readdirSync(uploads) : [], [], 'staging folder is cleaned');

  const badForm = new FormData();
  badForm.append('kind', 'humanoid');
  badForm.append('file', new Blob([Buffer.from('MZ not a glb')]), 'evil.glb');
  const rejected = await api('POST', '/api/forge/assets', badForm);
  assert.equal(rejected.status, 400);
  assert.equal(rejected.json.code, 'bad_signature');

  const started = await api('POST', `/api/forge/assets/${id}/run`, { steps: [{ step: 'inspect' }, { step: 'cleanup', params: { target_tris: 1000 } }] });
  assert.equal(started.status, 202);
  assert.equal(started.json.version, 'v001');
  const job = await waitJob(started.json.jobId);
  assert.equal(job.status, 'completed');
  assert.equal(job.assetId, id);
  assert.deepEqual(job.steps.map((s) => [s.step, s.status, s.progress]), [['inspect', 'completed', 100], ['cleanup', 'completed', 100]]);
  const badParams = await api('POST', `/api/forge/assets/${id}/run`, { steps: [{ step: 'cleanup', params: { target_tris: 'many' } }] });
  assert.equal(badParams.status, 400);
  assert.equal(badParams.json.code, 'bad_params');

  const manifest = (await api('GET', `/api/forge/assets/${id}`)).json;
  const previewUrl = manifest.pipeline[0].urls.preview;
  const preview = await api('GET', previewUrl.replace(base, ''));
  assert.equal(preview.status, 200);
  assert.equal(preview.text, 'png');
  const blend = await fetch(base + `/api/forge/assets/${id}/files/v001/02_cleanup/out.blend`);
  assert.equal(blend.status, 200);
  assert.match(blend.headers.get('content-disposition'), /attachment; filename="out.blend"/);
  const glb = await fetch(base + `/api/forge/assets/${id}/files/v001/02_cleanup/out.glb?download=1`);
  assert.match(glb.headers.get('content-disposition'), /attachment/);
  assert.equal((await api('GET', `/api/forge/assets/${id}/files/v001/..%2F..%2F..%2Fasset.json`)).status, 400);
  assert.equal((await api('GET', `/api/forge/assets/${id}/files/v001/01_inspect/request.exe`)).status, 403);
  assert.equal((await api('GET', `/api/forge/assets/${id}/files/v001/01_inspect/nothing.png`)).status, 404);
  const jobs = await api('GET', '/api/forge/jobs');
  assert.ok(jobs.json.some((j) => j.id === started.json.jobId));

  const patched = await api('PATCH', `/api/forge/assets/${id}`, { qa: { reviewed: 'yes' } });
  assert.equal(patched.status, 400);
  assert.equal(patched.json.code, 'validation');
  const exp = await api('POST', `/api/forge/assets/${id}/export`, { profile: 'godot' });
  assert.equal(exp.status, 202);
  assert.equal((await waitJob(exp.json.jobId)).status, 'completed');

  const list = (await api('GET', '/api/forge/assets')).json;
  assert.ok(list.some((a) => a.id === id && a.lastStatus === 'done'));
  const del = await api('DELETE', `/api/forge/assets/${id}`);
  assert.equal(del.status, 200);
  assert.ok(existsSync(path.join(del.json.trash, 'asset.json')));
  assert.equal((await api('GET', `/api/forge/assets/${id}`)).status, 404);
});

test('clip uploads are signature-checked and start an import job; batches report progress', async () => {
  const junk = new FormData();
  junk.append('files[]', new Blob([Buffer.from('hello')]), 'walk.fbx');
  const bad = await api('POST', '/api/forge/clips', junk);
  assert.equal(bad.status, 400);
  assert.equal(bad.json.code, 'bad_signature');
  const wrongExt = new FormData();
  wrongExt.append('files[]', new Blob([FBX]), 'walk.bvh');
  assert.equal((await api('POST', '/api/forge/clips', wrongExt)).status, 400);

  const form = new FormData();
  form.append('files[]', new Blob([FBX]), 'Walking.fbx');
  form.append('files', new Blob([Buffer.from('; FBX 7.4.0 project file\n')]), 'Idle.fbx');
  form.append('files[]', new Blob([Buffer.from('readme')]), 'notes.txt');
  form.append('files[]', new Blob([Buffer.from('junk')]), 'broken.fbx');
  const ok = await api('POST', '/api/forge/clips', form);
  assert.equal(ok.status, 202, ok.text);
  assert.deepEqual(ok.json.files, ['Walking.fbx', 'Idle.fbx']);
  assert.deepEqual(ok.json.rejected, ['notes.txt', 'broken.fbx']);
  const job = await waitJob(ok.json.jobId);
  assert.equal(job.type, 'clips');
  assert.equal(job.steps[0].params.files.length, 2);
  const uploads = path.join(engine.workDir, 'uploads');
  assert.ok(job.steps[0].params.files.every((f) => f.startsWith(uploads + path.sep)), 'staged outside the library');
  assert.deepEqual(job.steps[0].params.files.map((f) => path.basename(f)), ['Walking.fbx', 'Idle.fbx'], 'original names kept (clip ids come from them)');
  for (let i = 0; i < 40 && existsSync(path.dirname(job.steps[0].params.files[0])); i += 1) await new Promise((r) => setTimeout(r, 25));
  assert.equal(existsSync(path.dirname(job.steps[0].params.files[0])), false, 'staging folder removed after the import');
  assert.equal(existsSync(path.join(engine.animationsDir, '_incoming')), false);
  assert.deepEqual((await api('GET', '/api/forge/clips')).json, { clips: [] });
  assert.equal((await api('GET', '/api/forge/clips/nope/poses')).status, 404);

  const batch = await api('POST', '/api/forge/batch', { items: [
    { name: 'Turret', kind: 'ship', source: { type: 'params' }, preset: 'turret' },
    { name: 'Ship', kind: 'ship', source: { type: 'params', params: { seed: 5 } }, preset: 'ship_procedural' },
  ] });
  assert.equal(batch.status, 202);
  for (const it of batch.json.items) await waitJob(it.jobId);
  const summary = await api('GET', `/api/forge/batch/${batch.json.batchId}`);
  assert.equal(summary.json.status, 'completed');
  assert.equal(summary.json.counts.completed, 2);

  const files = new FormData();
  files.append('items', JSON.stringify([{ name: 'Crate', kind: 'prop', source: { type: 'upload', fileIndex: 0 }, steps: [{ step: 'inspect' }] }]));
  files.append('files[]', new Blob([GLB]), 'box.glb');
  const up = await api('POST', '/api/forge/batch', files);
  assert.equal(up.status, 202, up.text);
  assert.equal(up.json.items[0].error, null);
  assert.equal((await waitJob(up.json.items[0].jobId)).status, 'completed');
  const local = await api('POST', '/api/forge/batch', { items: [{ kind: 'prop', source: { type: 'upload', path: 'C:/Windows/win.ini' } }] });
  assert.match(local.json.items[0].error, /no file to upload was provided/, 'HTTP clients cannot make the server read local paths');
});

test('CLI talks to the server over HTTP', async () => {
  const item = (await api('POST', '/api/forge/assets', { kind: 'item', name: 'CLI sword', source: { type: 'params', params: { seed: 9 } } })).json;
  const listed = await runCli(['assets', '--json', '--server', base]);
  assert.equal(listed.code, 0, listed.stderr);
  assert.ok(JSON.parse(listed.stdout).some((a) => a.id === item.id));
  const run = await runCli(['run', item.id, '--preset', 'item_kit_sword', '--server', base]);
  assert.equal(run.code, 0, run.stderr + run.stdout);
  assert.match(run.stdout, /done/);
  assert.match(run.stdout, /item_kit/);
  const missing = await runCli(['show', '00000000-0000-4000-8000-000000000000', '--server', base]);
  assert.equal(missing.code, 1);
  assert.match(missing.stderr, /Asset not found/);
  const usage = await runCli(['run', item.id, '--server', base]);
  assert.equal(usage.code, 2);
  const down = await runCli(['status', '--server', 'http://127.0.0.1:9']);
  assert.equal(down.code, 1);
  assert.match(down.stderr, /unreachable/);
});

test('CLI --direct works without a server', async () => {
  const libDir = mkdtempSync(path.join(tmpdir(), 'af-cli-'));
  cliDirs.push(libDir);
  const env = {
    ASTRA_FORGE_LIBRARY: path.join(libDir, 'library'), ASTRA_FORGE_RUNTIME: path.join(libDir, 'runtime'),
    ASTRA_BLENDER: path.join(libDir, 'no-blender.exe'), ASTRA_GODOT: path.join(libDir, 'no-godot.exe'),
  };
  const status = await runCli(['status', '--direct', '--json'], env);
  assert.equal(status.code, 0, status.stderr);
  const s = JSON.parse(status.stdout);
  assert.equal(s.blender.ok, false);
  assert.match(s.blender.error, /Blender not found/);
  assert.equal(s.library.path, env.ASTRA_FORGE_LIBRARY);
  const created = await runCli(['create', '--direct', '--json', '--kind', 'ship', '--name', 'Scout', '--params', '{"seed":4}'], env);
  assert.equal(created.code, 0, created.stderr);
  const m = JSON.parse(created.stdout);
  assert.equal(m.name, 'Scout');
  assert.ok(existsSync(path.join(env.ASTRA_FORGE_LIBRARY, 'assets', m.id, 'asset.json')));
  const glbPath = path.join(libDir, 'model.glb');
  await fs.writeFile(glbPath, GLB);
  const upload = await runCli(['create', '--direct', '--json', '--kind', 'prop', '--from-glb', glbPath], env);
  assert.equal(upload.code, 0, upload.stderr);
  assert.equal(JSON.parse(upload.stdout).source.file, 'source/source.glb');
  // --no-wait with --direct would kill the job with the process: the CLI warns and waits instead.
  // (Blender is missing here, so the job fails, but it runs and is recorded.)
  const noWait = await runCli(['run', JSON.parse(upload.stdout).id, '--direct', '--json', '--no-wait', '--steps', '[{"step":"inspect"}]'], env);
  assert.equal(noWait.code, 1, noWait.stdout + noWait.stderr);
  assert.match(noWait.stderr, /--no-wait does not work with --direct/);
  const report = JSON.parse(noWait.stdout);
  assert.equal(report.status, 'failed');
  assert.match(report.error, /Failed to start Blender/);
  const record = JSON.parse(await fs.readFile(path.join(env.ASTRA_FORGE_LIBRARY, 'jobs', report.id + '.json'), 'utf8'));
  assert.equal(record.status, 'failed', 'the job record exists and is final');
  const shown = JSON.parse((await runCli(['show', report.assetId, '--direct', '--json'], env)).stdout);
  assert.equal(shown.versions[0].status, 'failed', 'the version is not left "queued"');
  const help = await runCli(['--help']);
  assert.equal(help.code, 0);
  assert.match(help.stdout, /command line/);
});
