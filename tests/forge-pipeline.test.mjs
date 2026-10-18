// Forge pipeline engine: manifests, versions, input resolution, clip sources, jobs, exports, guards.
// Uses a stub step runner (no Blender) and a temporary library folder.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { existsSync, mkdtempSync, promises as fs, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import JSZip from 'jszip';
import {
  ASSET_ID_RE, ForgeEngine, LIBRARY_LOCK_FILE, assetSlug, expandSocketMasks, findLocalPath, isInside, readGlbSockets, resolveClipSources, sniffMeshUpload,
} from '../server/forge-pipeline.mjs';
import { StepQueue, runBlenderStep } from '../server/blender.mjs';

const GLB = Buffer.concat([Buffer.from('glTF'), Buffer.from([2, 0, 0, 0, 20, 0, 0, 0]), Buffer.alloc(8)]);
const FBX = Buffer.concat([Buffer.from('Kaydara FBX Binary  \0'), Buffer.from([0x1a, 0, 0xe8, 0x1c, 0, 0]), Buffer.alloc(64)]);
const OBJ = Buffer.from('# test\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// temporary libraries are removed when the file is done (best effort)
const TEMP_DIRS = [];
const tempDir = (prefix) => { const d = mkdtempSync(path.join(tmpdir(), prefix)); TEMP_DIRS.push(d); return d; };
after(async () => { for (const d of TEMP_DIRS) await fs.rm(d, { recursive: true, force: true }).catch(() => {}); });

/** A minimal GLB with only a JSON chunk (enough for socket discovery). */
function makeGlb(json) {
  let body = Buffer.from(JSON.stringify(json), 'utf8');
  body = Buffer.concat([body, Buffer.alloc((4 - (body.length % 4)) % 4, 0x20)]);
  const header = Buffer.alloc(12);
  header.write('glTF', 0, 'latin1');
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(20 + body.length, 8);
  const chunk = Buffer.alloc(8);
  chunk.writeUInt32LE(body.length, 0);
  chunk.writeUInt32LE(0x4e4f534a, 4);
  return Buffer.concat([header, chunk, body]);
}
// two thrusters (one found by extras.game_socket only, as Blender may rename nodes) and a turret
const SHIP_GLB = makeGlb({
  asset: { version: '2.0' },
  nodes: [
    { name: 'ship_root', children: [1, 2, 3] },
    { name: 'socket_thruster_main_l', extras: { game_socket: 'socket_thruster_main_l', kind: 'thruster' } },
    { name: 'Empty.001', extras: { game_socket: 'socket_thruster_main_r', kind: 'thruster' } },
    { name: 'socket_turret_top_01' },
  ],
});

/** Stub for a generator step that writes the given GLB as its output. */
const generatorWith = (buf) => async (o) => {
  const outputs = { glb: path.join(o.request.out_dir, 'ship.glb'), blend: path.join(o.request.out_dir, 'ship.blend') };
  await fs.writeFile(outputs.glb, buf);
  await fs.writeFile(outputs.blend, 'blend');
  return { result: { ok: true, step: o.step, outputs, stats: {}, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
};

function stubRunner(overrides = {}) {
  const calls = [];
  const runStep = async (o) => {
    calls.push(o);
    if (o.signal?.aborted) throw Object.assign(new Error('Step cancelled'), { name: 'AbortError' });
    o.onStart?.();
    const out = o.request.out_dir;
    await fs.mkdir(out, { recursive: true });
    if (overrides[o.step]) return overrides[o.step](o);
    o.onProgress?.(50, {});
    o.onLog?.({ msg: 'working' });
    const outputs = {};
    if (o.step === 'inspect') {
      outputs.preview = path.join(out, 'preview.png');
      await fs.writeFile(outputs.preview, 'png');
    } else if (o.step === 'godot_package') {
      // like the real step: the package folder plus its (possibly re-oriented) GLB as outputs.glb
      const pkg = path.join(out, 'package');
      await fs.mkdir(path.join(pkg, 'scenes'), { recursive: true });
      await fs.writeFile(path.join(pkg, 'model.glb'), GLB);
      await fs.writeFile(path.join(pkg, 'scenes', 'model.tscn'), '[gd_scene]');
      outputs.package = pkg;
      outputs.glb = path.join(pkg, 'model.glb');
      outputs.tscn = path.join(pkg, 'scenes', 'model.tscn');
    } else if (o.step === 'clip_library') {
      outputs.index = path.join(out, 'index.json');
      await fs.writeFile(outputs.index, '{}');
    } else {
      outputs.glb = path.join(out, `${o.step}.glb`);
      outputs.blend = path.join(out, `${o.step}.blend`);
      outputs.preview = path.join(out, 'preview.png');
      for (const f of [outputs.glb, outputs.blend, outputs.preview]) await fs.writeFile(f, 'x');
    }
    const result = { ok: true, step: o.step, outputs, stats: {}, checks: [{ name: `${o.step}_ok`, ok: true, severity: 'error' }], warnings: [] };
    await fs.writeFile(path.join(out, 'result.json'), JSON.stringify(result));
    return { result, logTail: ['ok'], logFile: null, warnings: [] };
  };
  return { runStep, calls };
}

const hang = (o) => new Promise((resolve, reject) => {
  const fail = () => reject(Object.assign(new Error('Step cancelled'), { name: 'AbortError' }));
  if (o.signal.aborted) return fail();
  o.signal.addEventListener('abort', fail, { once: true });
});

async function makeEngine(overrides = {}, opts = {}) {
  const dir = opts.dir || tempDir('af-forge-');
  const stub = opts.stub || stubRunner(overrides);
  const engine = new ForgeEngine({
    libraryDir: path.join(dir, 'library'),
    runtimeDir: path.join(dir, 'runtime'),
    studioAssetsDir: path.join(dir, 'studio', 'assets'),
    studioJobsDir: path.join(dir, 'studio', 'jobs'),
    runStep: stub.runStep,
    blenderInfo: () => ({ ok: false, path: 'none', version: null }),
    godotInfo: () => ({ ok: false, path: null, version: null }),
    persistIntervalMs: 0,
    ...opts.engine,
  });
  await engine.init();
  await fs.mkdir(engine.studioAssetsDir, { recursive: true });
  await fs.mkdir(engine.studioJobsDir, { recursive: true });
  return { engine, dir, calls: stub.calls, stub };
}

async function writeLibrary(engine, clips) {
  await fs.mkdir(path.join(engine.animationsDir, 'mixamo'), { recursive: true });
  for (const c of clips) if (c.file) await fs.writeFile(path.join(engine.animationsDir, c.file), FBX);
  await fs.writeFile(path.join(engine.animationsDir, 'index.json'), JSON.stringify({ clips }));
}

const LIB = [
  { id: 'mixamo-walk-b', category: 'walk', file: 'mixamo/mixamo-walk-b.fbx', tags: ['sword'] },
  { id: 'mixamo-walk-a', category: 'walk', file: 'mixamo/mixamo-walk-a.fbx', tags: [] },
  { id: 'mixamo-idle', category: 'idle', file: 'mixamo/mixamo-idle.fbx' },
  { id: 'mixamo-attack', category: 'attack', file: 'mixamo/mixamo-attack.fbx' },
  { id: 'mixamo-hit', category: 'hit', file: 'mixamo/mixamo-hit.fbx' },
  { id: 'mixamo-death', category: 'death', file: 'mixamo/mixamo-death.fbx' },
];

test('upload sniffing accepts GLB/FBX/OBJ by signature only', () => {
  assert.equal(sniffMeshUpload(GLB, 'a.glb'), '.glb');
  assert.equal(sniffMeshUpload(FBX, 'clip.FBX'), '.fbx');
  assert.equal(sniffMeshUpload(Buffer.from('; FBX 7.4.0 project file\nFBXHeaderExtension: {'), 'ascii.fbx'), '.fbx');
  assert.equal(sniffMeshUpload(OBJ, 'm.obj'), '.obj');
  assert.throws(() => sniffMeshUpload(GLB, 'a.fbx'), /does not match the file format/);
  assert.throws(() => sniffMeshUpload(Buffer.from('MZ....'), 'a.glb'), /does not match the file format/);
  assert.throws(() => sniffMeshUpload(GLB, 'a.exe'), /GLB, FBX and OBJ/);
  assert.throws(() => sniffMeshUpload(Buffer.alloc(0), 'a.glb'), /is empty/);
});

test('game asset ids are snake_case ASCII', () => {
  const id = '1a2b3c4d-0000-4000-8000-000000000000';
  assert.equal(assetSlug({ id, name: 'Волшебник — исправленный' }), 'volshebnik_ispravlennyy_1a2b3c4d');
  assert.equal(assetSlug({ id, name: 'X-Wing Mk.2' }), 'x_wing_mk_2_1a2b3c4d');
  assert.equal(assetSlug({ id, name: '42 ★' }), 'asset_42_1a2b3c4d');
  assert.equal(assetSlug({ id, name: '' }), 'asset_1a2b3c4d');
  assert.equal(assetSlug({ id, name: 'x', profile: { asset_id: 'af_militia' } }), 'af_militia');
  for (const s of ['volshebnik_ispravlennyy_1a2b3c4d', 'asset_1a2b3c4d']) assert.match(s, ASSET_ID_RE);
});

test('assets are created from params, uploads, generation jobs and other assets', async () => {
  const { engine } = await makeEngine();
  const p = await engine.createAsset({ kind: 'ship', name: 'Fighter', source: { type: 'params', params: { seed: 3 } } });
  assert.equal(p.kind, 'ship');
  assert.equal(p.source.params.seed, 3);
  assert.deepEqual(p.pipeline, []);
  assert.equal(p.qa.reviewed, false);

  const up = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'Волшебник.glb' });
  assert.equal(up.name, 'Волшебник');
  assert.equal(up.profile.height_m, 1.8);
  assert.ok(existsSync(path.join(engine.assetDir(up.id), 'source', 'source.glb')));
  await assert.rejects(engine.createAsset({ kind: 'item', source: { type: 'upload' } }, { buffer: GLB, originalName: 'x.fbx' }), /does not match the file format/);
  await assert.rejects(engine.createAsset({ kind: 'item', source: { type: 'upload' } }, { path: 'C:/x.glb' }), /--direct/);
  await assert.rejects(engine.createAsset({ kind: 'dragon', source: { type: 'params' } }), /Asset kind/);

  // generation job: copies data/assets/<asset>.glb
  const genAsset = randomUUID();
  const genJob = randomUUID();
  await fs.writeFile(path.join(engine.studioAssetsDir, genAsset + '.glb'), GLB);
  await fs.writeFile(path.join(engine.studioJobsDir, genJob + '.json'), JSON.stringify({ id: genJob, status: 'completed', prompt: 'robed mage', imageId: randomUUID(), asset: { id: genAsset, extension: '.glb' } }));
  const fromJob = await engine.createAsset({ kind: 'humanoid', source: { type: 'job', jobId: genJob } });
  assert.equal(fromJob.name, 'robed mage');
  assert.equal(fromJob.source.jobId, genJob);
  assert.deepEqual(readFileSync(path.join(engine.assetDir(fromJob.id), 'source', 'source.glb')), GLB);
  const running = randomUUID();
  await fs.writeFile(path.join(engine.studioJobsDir, running + '.json'), JSON.stringify({ id: running, status: 'running' }));
  await assert.rejects(engine.createAsset({ kind: 'humanoid', source: { type: 'job', jobId: running } }), (e) => e.status === 409);
  await assert.rejects(engine.createAsset({ kind: 'humanoid', source: { type: 'job', jobId: 'not-a-uuid' } }), (e) => e.status === 400);

  // in-memory generation jobs (server mode) take precedence
  const memEngine = new ForgeEngine({ libraryDir: engine.libraryDir, runtimeDir: engine.runtimeDir, studioAssetsDir: engine.studioAssetsDir, studioJobsDir: engine.studioJobsDir, getGenerationJob: (id) => (id === running ? { status: 'completed', asset: { id: genAsset, extension: '.glb' }, prompt: 'memory' } : null) });
  await memEngine.init();
  assert.equal((await memEngine.createAsset({ kind: 'prop', source: { type: 'job', jobId: running } })).name, 'memory');

  // studio asset (OBJ) and forge asset sources
  const studioObj = randomUUID();
  await fs.writeFile(path.join(engine.studioAssetsDir, studioObj + '.obj'), OBJ);
  await fs.writeFile(path.join(engine.studioAssetsDir, studioObj + '.json'), JSON.stringify({ id: studioObj, extension: '.obj', name: 'Crate' }));
  const fromStudio = await engine.createAsset({ kind: 'prop', source: { type: 'asset', assetId: studioObj } });
  assert.equal(fromStudio.source.file, 'source/source.obj');
  const fromForge = await engine.createAsset({ kind: 'humanoid', source: { type: 'asset', assetId: up.id } });
  assert.equal(fromForge.source.forgeAsset, true);
  assert.match(fromForge.name, /copy/);

  const list = await engine.listAssets();
  assert.equal(list.length, 6);
  assert.ok(list.every((a) => a.lastStatus === 'new'));
});

test('a run creates v001 with numbered step folders and resolves inputs along the chain', async () => {
  const { engine, calls } = await makeEngine();
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'hero.glb' });
  const src = path.join(engine.assetDir(a.id), 'source', 'source.glb');
  const run = await engine.startRun(a.id, { steps: [
    { step: 'inspect', params: { size: 128 } },
    { step: 'cleanup', params: { target_tris: 40000 } },
    { step: 'rig_humanoid' },
    { step: 'godot_package' },
  ] });
  assert.equal(run.version, 'v001');
  const job = await engine.waitJob(run.jobId);
  assert.equal(job.status, 'completed', job.error);
  const v1 = path.join(engine.assetDir(a.id), 'v001');
  assert.deepEqual(calls.map((c) => path.relative(v1, c.request.out_dir)), ['01_inspect', '02_cleanup', '03_rig_humanoid', '04_godot_package']);
  assert.deepEqual(calls[0].request.inputs, { mesh: src }, 'first step reads the asset source');
  assert.deepEqual(calls[1].request.inputs, { mesh: src }, 'inspect does not change the chain state');
  assert.deepEqual(calls[2].request.inputs, { blend: path.join(v1, '02_cleanup', 'cleanup.blend') }, 'outputs.blend -> inputs.blend');
  assert.equal(calls[3].request.inputs.glb, path.join(v1, '03_rig_humanoid', 'rig_humanoid.glb'), 'godot_package gets the current GLB');
  assert.ok(existsSync(calls[3].request.inputs.manifest), 'godot_package gets a manifest snapshot');
  assert.equal(calls[3].request.params.asset_id, `hero_${a.id.slice(0, 8)}`, 'server fills a snake_case asset_id');
  assert.match(calls[3].request.params.asset_id, ASSET_ID_RE);
  assert.deepEqual(calls[1].request.params, { target_tris: 40000 }, 'only explicit params reach Blender (step defaults stay authoritative)');
  assert.deepEqual(calls[0].request.params, { size: 128 });
  assert.equal(calls[0].logName, 'inspect.log');
  assert.equal(calls[1].gpu, false);
  assert.equal(calls[1].timeoutSec, 1800);

  const m = await engine.getAsset(a.id);
  assert.equal(m.pipeline.length, 4);
  assert.ok(m.pipeline.every((e) => e.status === 'done' && e.version === 'v001'));
  assert.match(m.pipeline[1].urls.glb, new RegExp(`/api/forge/assets/${a.id}/files/v001/02_cleanup/cleanup.glb$`));
  assert.equal(m.versions[0].status, 'completed');
  assert.equal(m.exports.length, 1);
  assert.equal(m.exports[0].profile, 'godot');
  const zip = await JSZip.loadAsync(readFileSync(m.exports[0].path));
  assert.deepEqual(Object.keys(zip.files).filter((n) => !zip.files[n].dir).sort(), ['model.glb', 'scenes/model.tscn']);
  assert.ok(m.qa.checks.some((c) => c.name === 'rig_humanoid_ok' && c.step === 'rig_humanoid'));
  const summary = (await engine.listAssets()).find((x) => x.id === a.id);
  assert.equal(summary.lastStatus, 'done');
  assert.match(summary.thumbnail, /03_rig_humanoid\/preview\.png$/);
  const vj = JSON.parse(readFileSync(path.join(v1, 'version.json'), 'utf8'));
  assert.equal(vj.status, 'completed');
  assert.equal(vj.final.glb, path.join(v1, '03_rig_humanoid', 'rig_humanoid.glb'));
  assert.ok(vj.pipeline.every((e) => e.status === 'done'));

  // the job record is persisted
  const record = JSON.parse(readFileSync(path.join(engine.jobsDir, run.jobId + '.json'), 'utf8'));
  assert.equal(record.status, 'completed');
  assert.equal(record.steps[3].status, 'completed');
  assert.equal(record.progress, 100);

  // from: latest -> v002 on top of v001; versions are never overwritten
  calls.length = 0;
  const r2 = await engine.startRun(a.id, { steps: [{ step: 'inspect' }], from: 'latest' });
  assert.equal(r2.version, 'v002');
  await engine.waitJob(r2.jobId);
  assert.deepEqual(calls[0].request.inputs, { mesh: path.join(v1, '03_rig_humanoid', 'rig_humanoid.glb') });
  const m2 = await engine.getAsset(a.id);
  assert.equal(m2.pipeline.length, 5, 'recipe = v001 steps + new step');
  assert.equal(m2.versions[1].base, 'v001');
  const r3 = await engine.startRun(a.id, { steps: [{ step: 'inspect' }], from: 'v001' });
  assert.equal(r3.version, 'v003');
  await engine.waitJob(r3.jobId);
  await assert.rejects(engine.startRun(a.id, { steps: [{ step: 'inspect' }], from: 'v009' }), (e) => e.status === 404);
  await assert.rejects(engine.startRun(a.id, { steps: [{ step: 'inspect' }], from: 'yesterday' }), (e) => e.status === 400);
  await assert.rejects(engine.startRun(a.id, { steps: [{ step: 'clip_library' }] }), /clip library/);
  await assert.rejects(engine.startRun(a.id, { preset: 'nope' }), /Unknown preset/);

  // export -> new version from the latest completed one, zipped into exports/
  calls.length = 0;
  const ex = await engine.startExport(a.id, { profile: 'godot', params: { forward_axis: '-Z' } });
  assert.equal(ex.version, 'v004');
  const exJob = await engine.waitJob(ex.jobId);
  assert.equal(exJob.type, 'export');
  assert.equal(calls[0].step, 'godot_package');
  assert.equal(calls[0].request.params.profile, 'godot');
  assert.equal(calls[0].request.params.forward_axis, '-Z');
  assert.equal(calls[0].request.inputs.glb, path.join(v1, '03_rig_humanoid', 'rig_humanoid.glb'));
  const m4 = await engine.getAsset(a.id);
  assert.ok(m4.exports.some((e) => e.profile === 'godot' && e.version === 'v004' && existsSync(e.path)));
  await assert.rejects(engine.startExport(a.id, { profile: 'unity' }), /Export profile/);
});

test('generators take asset source params; attach_prop gets character and item', async () => {
  const { engine, calls } = await makeEngine();
  const sword = await engine.createAsset({ kind: 'item', name: 'Axe', source: { type: 'params', params: { type: 'axe', seed: 7 } } });
  const r = await engine.startRun(sword.id, { preset: 'item_kit_sword' });
  await engine.waitJob(r.jobId);
  assert.equal(calls[0].step, 'item_kit');
  assert.deepEqual(calls[0].request.inputs, {});
  assert.equal(calls[0].request.params.type, 'axe');
  assert.equal(calls[0].request.params.seed, 7);
  assert.equal(calls[0].request.params.style, 'medieval');

  const hero = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'hero.glb' });
  await assert.rejects(engine.startRun(hero.id, { steps: [{ step: 'attach_prop' }] }), /props/);
  await engine.patchAsset(hero.id, { props: [{ asset: sword.id, socket: 'socket_mount_hand_r' }] });
  calls.length = 0;
  const r2 = await engine.startRun(hero.id, { steps: [{ step: 'cleanup' }, { step: 'attach_prop', params: { hand: 'r' } }] });
  const job = await engine.waitJob(r2.jobId);
  assert.equal(job.status, 'completed', job.error);
  const swordGlb = path.join(engine.assetDir(sword.id), 'v001', '01_item_kit', 'item_kit.glb');
  assert.deepEqual(calls[1].request.inputs, { character: path.join(engine.assetDir(hero.id), 'v001', '01_cleanup', 'cleanup.glb'), item: swordGlb });
  assert.equal('prop_asset' in calls[1].request.params, false);

  // a step that needs an input fails fast when the asset has none
  const empty = await engine.createAsset({ kind: 'ship', source: { type: 'params' } });
  await assert.rejects(engine.startRun(empty.id, { steps: [{ step: 'cleanup' }] }), (e) => e.code === 'no_input' && /needs an input/.test(e.message));
});

test('clip sources resolve deterministically from the library', async () => {
  const { engine, calls } = await makeEngine();
  const lib = engine.animationsDir;
  await assert.rejects(
    engine.startRun((await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' })).id, { preset: 'humanoid_from_mesh' }),
    (e) => e.code === 'clips_missing' && /no category "idle"/.test(e.message));
  await writeLibrary(engine, LIB);
  const index = await engine.clipIndex();
  const resolved = resolveClipSources([
    { source: 'category:walk', loop: true, in_place: true },
    { source: 'category:walk:sword', name: 'sword-walk' },
    { source: 'library:mixamo-death' },
    { source: 'category:idle' },
    { source: 'procedural:breathe' },
  ], index, lib);
  assert.equal(resolved[0].clip_id, 'mixamo-walk-a', 'sorted by id, first wins');
  assert.equal(resolved[0].source, 'library:mixamo-walk-a');
  assert.equal(resolved[0].requested, 'category:walk');
  assert.equal(resolved[0].file, path.join(lib, 'mixamo', 'mixamo-walk-a.fbx'));
  assert.equal(resolved[0].name, 'walk');
  assert.equal(resolved[0].in_place, true);
  assert.equal(resolved[1].clip_id, 'mixamo-walk-b');
  assert.equal(resolved[1].name, 'sword-walk');
  assert.equal(resolved[2].name, 'mixamo-death');
  assert.equal(resolved[2].loop, false);
  assert.equal(resolved[3].loop, true, 'idle loops by default');
  assert.deepEqual(resolved[4], { source: 'procedural:breathe' });
  assert.throws(() => resolveClipSources([{ source: 'category:jump' }], index, lib), /no category "jump"/);
  assert.throws(() => resolveClipSources([{ source: 'category:dance' }], index, lib), /unknown category/);
  assert.throws(() => resolveClipSources([{ source: 'library:nope' }], index, lib), /not found/);
  assert.throws(() => resolveClipSources([{ source: 'library:x' }], { clips: [{ id: 'x', category: 'idle', file: '../../evil.fbx' }] }, lib), /outside the library/);
  assert.throws(() => resolveClipSources([{ source: 'library:y' }], { clips: [{ id: 'y', category: 'idle', file: 'mixamo/missing.fbx' }] }, lib), /missing from the library/);

  const hero = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const r = await engine.startRun(hero.id, { preset: 'humanoid_from_mesh' });
  const job = await engine.waitJob(r.jobId);
  assert.equal(job.status, 'completed', job.error);
  const retarget = calls.find((c) => c.step === 'retarget');
  assert.deepEqual(retarget.request.params.clips.map((c) => c.clip_id), ['mixamo-idle', 'mixamo-walk-a', 'mixamo-attack', 'mixamo-hit', 'mixamo-death']);
  assert.equal(retarget.request.params.library, lib);
  assert.equal(retarget.request.params.root_motion, 'extract', 'the Godot preset asks for root motion explicitly');
  assert.equal('leg_ik' in retarget.request.params, false, 'registry defaults are not forced on the step');
  const m = await engine.getAsset(hero.id);
  const entry = m.pipeline.find((e) => e.step === 'retarget');
  assert.equal(entry.params.clips[0].source, 'category:idle', 'recipe keeps the requested source');
  assert.equal(entry.resolvedClips[1].clip_id, 'mixamo-walk-a', 'resolution is recorded for reproducibility');
});

test('failures, skipped steps and cancellation are recorded in the job and manifest', async () => {
  const { engine } = await makeEngine({
    rig_humanoid: async () => ({ result: { ok: false, step: 'rig_humanoid', outputs: {}, checks: [{ name: 'unweighted_vertices', ok: false, severity: 'error' }], error: 'checks failed: unweighted_vertices' }, logTail: ['bad'] }),
    separate: hang,
  });
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const r = await engine.startRun(a.id, { steps: [{ step: 'cleanup' }, { step: 'rig_humanoid' }, { step: 'godot_package' }] });
  const job = await engine.waitJob(r.jobId);
  assert.equal(job.status, 'failed');
  assert.match(job.error, /unweighted_vertices/);
  assert.deepEqual(job.steps.map((s) => s.status), ['completed', 'failed', 'skipped']);
  let m = await engine.getAsset(a.id);
  assert.deepEqual(m.pipeline.map((e) => e.status), ['done', 'failed', 'failed']);
  assert.equal(m.versions[0].status, 'failed');
  assert.equal((await engine.listAssets())[0].lastStatus, 'failed');
  await assert.rejects(engine.startRun(a.id, { steps: [{ step: 'inspect' }], from: 'v001' }), (e) => e.status === 409);

  // cancel a running step and a queued job (CPU steps: one job at a time)
  const running = await engine.startRun(a.id, { steps: [{ step: 'separate' }, { step: 'godot_package' }] });
  const queued = await engine.startRun(a.id, { steps: [{ step: 'inspect' }] });
  for (let i = 0; i < 50 && (await engine.getJob(running.jobId)).steps[0].status !== 'running'; i += 1) await new Promise((res) => setTimeout(res, 20));
  assert.equal((await engine.getJob(queued.jobId)).status, 'queued', 'one job at a time');
  await engine.cancelJob(queued.jobId);
  await engine.cancelJob(running.jobId);
  const [jr, jq] = await Promise.all([engine.waitJob(running.jobId), engine.waitJob(queued.jobId)]);
  assert.equal(jr.status, 'cancelled');
  assert.deepEqual(jr.steps.map((s) => s.status), ['cancelled', 'cancelled']);
  assert.equal(jq.status, 'cancelled');
  assert.equal(jq.steps[0].status, 'cancelled');
  m = await engine.getAsset(a.id);
  assert.ok(m.versions.find((v) => v.version === jq.version).status === 'cancelled');
  assert.deepEqual((await engine.cancelJob(running.jobId)).status, 'cancelled', 'cancelling a finished job is a no-op');
});

test('a restart marks running jobs failed (job record, manifest and version)', async () => {
  const first = await makeEngine({ cleanup: hang });
  const a = await first.engine.createAsset({ kind: 'prop', source: { type: 'upload' } }, { buffer: GLB, originalName: 'p.glb' });
  const r = await first.engine.startRun(a.id, { steps: [{ step: 'cleanup' }, { step: 'inspect' }] });
  for (let i = 0; i < 50 && (await first.engine.getJob(r.jobId)).steps[0].status !== 'running'; i += 1) await new Promise((res) => setTimeout(res, 20));
  assert.equal(JSON.parse(readFileSync(path.join(first.engine.jobsDir, r.jobId + '.json'), 'utf8')).status, 'running');

  const second = await makeEngine({}, { dir: first.dir });
  const job = await second.engine.getJob(r.jobId);
  assert.equal(job.status, 'failed');
  assert.match(job.error, /interrupted.*restart/);
  assert.deepEqual(job.steps.map((s) => s.status), ['failed', 'cancelled']);
  const m = await second.engine.getAsset(a.id);
  assert.deepEqual(m.pipeline.map((e) => e.status), ['failed', 'failed']);
  assert.equal(m.versions[0].status, 'failed');
  await first.engine.cancelJob(r.jobId); // stop the hanging stub of the first "process"
  await first.engine.waitJob(r.jobId);
});

test('file serving is limited to the asset folder and the extension whitelist', async () => {
  const { engine } = await makeEngine();
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const r = await engine.startRun(a.id, { steps: [{ step: 'cleanup' }] });
  await engine.waitJob(r.jobId);
  const ok = await engine.resolveAssetFile(a.id, ['v001', '01_cleanup', 'cleanup.glb']);
  assert.equal(ok.ext, 'glb');
  assert.equal(ok.download, false);
  assert.equal((await engine.resolveAssetFile(a.id, ['v001', '01_cleanup', 'cleanup.blend'])).download, true);
  assert.ok(isInside(engine.assetDir(a.id), ok.path));
  assert.equal((await engine.resolveAssetFile(a.id, ['source', 'source.glb'])).name, 'source.glb');
  const bad = async (segments, status) => assert.rejects(engine.resolveAssetFile(a.id, segments), (e) => e.status === status);
  await bad(['v001', '..', '..', 'asset.json'], 400);
  await bad(['v001', '..%2F..', 'x.json'], 400);
  await bad(['..', 'jobs', 'x.json'], 400);
  await bad(['v001', '01_cleanup\\..\\..', 'x.json'], 400);
  await bad(['asset.json', 'x.json'], 400);
  await bad(['v001', '01_cleanup', 'request.exe'], 403);
  await bad(['v001', '01_cleanup', 'missing.glb'], 404);
  await bad(['v001'], 400);
  await assert.rejects(engine.resolveAssetFile('../etc', ['v001', 'a.json']), (e) => e.status === 400);
  await assert.rejects(engine.getAsset(randomUUID()), (e) => e.status === 404);
});

test('patch rules, trash instead of delete, batches and clip library maintenance', async () => {
  const { engine } = await makeEngine();
  const a = await engine.createAsset({ kind: 'creature', source: { type: 'upload' } }, { buffer: GLB, originalName: 'wolf.glb' });
  let m = await engine.patchAsset(a.id, { name: 'Wolf', profile: { length_m: 2 }, qa: { notes: 'short tail' } });
  assert.equal(m.name, 'Wolf');
  assert.equal(m.profile.template, 'auto');
  assert.equal(m.profile.length_m, 2);
  assert.equal(m.qa.reviewed, false, 'reviewed changes only with an explicit field');
  m = await engine.patchAsset(a.id, { qa: { reviewed: true } });
  assert.equal(m.qa.reviewed, true);
  await assert.rejects(engine.patchAsset(a.id, { props: [{ asset: 'x', socket: 'hand' }] }), (e) => e.status === 400);
  await assert.rejects(engine.patchAsset(a.id, { pipeline: [] }), (e) => e.status === 400);

  const trashed = await engine.trashAsset(a.id);
  assert.ok(existsSync(path.join(trashed.trash, 'asset.json')), 'asset moved into trash');
  assert.ok(isInside(engine.trashDir, trashed.trash));
  await assert.rejects(engine.getAsset(a.id), (e) => e.status === 404);

  const batch = await engine.startBatch({ items: [
    { name: 'Sword 1', kind: 'item', source: { type: 'params', params: { seed: 1 } }, preset: 'item_kit_sword' },
    { name: 'Sword 2', kind: 'item', source: { type: 'params', params: { seed: 2 } }, preset: 'item_kit_sword' },
    { name: 'Error', kind: 'unicorn', source: { type: 'params' } },
    { name: 'Upload', kind: 'prop', source: { type: 'upload', fileIndex: 0 }, steps: [{ step: 'inspect' }] },
  ] }, [{ buffer: OBJ, originalName: 'box.obj' }]);
  assert.equal(batch.items.length, 4);
  assert.match(batch.items[2].error, /Asset kind/);
  for (const it of batch.items) if (it.jobId) await engine.waitJob(it.jobId);
  const summary = await engine.getBatch(batch.batchId);
  assert.equal(summary.counts.completed, 3);
  assert.equal(summary.counts.failed, 1);
  assert.equal(summary.status, 'partial');
  await assert.rejects(engine.startBatch({ items: [] }), /non-empty/);

  await writeLibrary(engine, LIB);
  await fs.writeFile(path.join(engine.animationsDir, 'mixamo', 'mixamo-idle.poses.json'), '{"frames":[]}');
  assert.match(await engine.clipPosesPath('mixamo-idle'), /mixamo-idle\.poses\.json$/);
  await assert.rejects(engine.clipPosesPath('mixamo-hit'), (e) => e.status === 404);
  await assert.rejects(engine.clipPosesPath('../index'), (e) => e.status === 400);
  const removed = await engine.removeClip('mixamo-idle');
  assert.equal(removed.moved.length, 2);
  assert.ok(existsSync(path.join(removed.trash, 'mixamo-idle.fbx')));
  assert.equal((await engine.clipIndex()).clips.some((c) => c.id === 'mixamo-idle'), false);
  await assert.rejects(engine.removeClip('mixamo-idle'), (e) => e.status === 404);

  const incoming = path.join(engine.runtimeDir, 'incoming');
  await fs.mkdir(incoming, { recursive: true });
  await fs.writeFile(path.join(incoming, 'Walking.fbx'), FBX);
  const imp = await engine.importClips({ folder: incoming });
  const job = await engine.waitJob(imp.jobId);
  assert.equal(job.type, 'clips');
  assert.deepEqual(job.steps[0].params.files, [path.join(incoming, 'Walking.fbx')]);
  assert.equal(job.steps[0].params.action, 'import');
  assert.equal(job.steps[0].request.params.library, engine.animationsDir);
  await fs.writeFile(path.join(incoming, 'fake.fbx'), 'not an fbx');
  await assert.rejects(engine.importClips({ folder: incoming }), /does not look like an FBX/);

  const status = await engine.status();
  assert.equal(status.library.clips, LIB.length - 1);
  assert.equal(status.gpuLock.name, process.env.ASTRA_GPU_LOCK_NAME || process.env.ASTRA_GPU_MUTEX || 'Global\\AF_GPU_RENDER');
  assert.equal(typeof status.queue.running, 'number');
  const jobs = await engine.listJobs();
  assert.ok(jobs.length >= 4 && jobs.every((j) => j.id && j.status));
});

// ------------------------------------------------------------------ fix round (review 10.10)

test('fx socket masks are expanded against the sockets of the input GLB', async () => {
  const tmp = tempDir('af-glb-');
  const file = path.join(tmp, 'ship.glb');
  await fs.writeFile(file, SHIP_GLB);
  const sockets = await readGlbSockets(file);
  assert.deepEqual([...sockets].sort(), ['socket_thruster_main_l', 'socket_thruster_main_r', 'socket_turret_top_01']);
  const fx = expandSocketMasks([
    { preset: 'engine_trail', socket: 'socket_thruster_*', params: { rate: 2 } },
    { preset: 'muzzle_flash', socket: 'socket_turret_top_01' },
  ], sockets);
  assert.deepEqual(fx.map((f) => f.socket), ['socket_thruster_main_l', 'socket_thruster_main_r', 'socket_turret_top_01']);
  assert.deepEqual(fx[1], { preset: 'engine_trail', socket: 'socket_thruster_main_r', params: { rate: 2 } });
  assert.equal(expandSocketMasks([{ preset: 'engine_trail', socket: 'socket_thruster_main_?' }], sockets).length, 2);
  assert.throws(() => expandSocketMasks([{ preset: 'cargo_glow', socket: 'socket_cargo_*' }], sockets), (e) => e.code === 'fx_socket_missing' && /matched no socket/.test(e.message));
  await fs.writeFile(path.join(tmp, 'bad.glb'), 'not a glb');
  await assert.rejects(readGlbSockets(path.join(tmp, 'bad.glb')), /Failed to read the model sockets/);

  // the ship_procedural preset end to end: godot_package receives explicit sockets
  const { engine, calls } = await makeEngine({ ship_gen: generatorWith(SHIP_GLB) });
  const ship = await engine.createAsset({ kind: 'ship', name: 'Fighter', source: { type: 'params', params: { seed: 3 } } });
  const r = await engine.startRun(ship.id, { preset: 'ship_procedural' });
  const job = await engine.waitJob(r.jobId);
  assert.equal(job.status, 'completed', job.error);
  const pkg = calls.find((c) => c.step === 'godot_package');
  assert.deepEqual(pkg.request.params.fx, [
    { preset: 'engine_trail', socket: 'socket_thruster_main_l', params: {} },
    { preset: 'engine_trail', socket: 'socket_thruster_main_r', params: {} },
  ]);
  assert.match(calls[0].request.params.asset_id, ASSET_ID_RE, 'ship_gen gets the same snake_case asset_id');
  const m = await engine.getAsset(ship.id);
  assert.equal(m.pipeline.find((e) => e.step === 'godot_package').params.fx[0].socket, 'socket_thruster_*', 'the recipe keeps the mask');

  // passport fx with a mask are expanded too; a mask without matches fails in Russian
  await engine.patchAsset(ship.id, { fx: [{ preset: 'engine_trail', socket: 'socket_thruster_main_*' }] });
  calls.length = 0;
  const r2 = await engine.startRun(ship.id, { steps: [{ step: 'ship_gen' }, { step: 'godot_package', params: { profile: 'godot' } }] });
  assert.equal((await engine.waitJob(r2.jobId)).status, 'completed');
  assert.equal(calls[1].request.params.fx.length, 2);
  const r3 = await engine.startRun(ship.id, { steps: [{ step: 'ship_gen' }, { step: 'godot_package', params: { fx: [{ preset: 'cargo_glow', socket: 'socket_cargo_*' }] } }] });
  const failed = await engine.waitJob(r3.jobId);
  assert.equal(failed.status, 'failed');
  assert.match(failed.error, /socket mask "socket_cargo_\*" matched no socket/);
  assert.deepEqual(failed.steps.map((s) => s.status), ['completed', 'failed']);
});

test('export steps keep the chain model: render_sprites after godot_package gets the rig .blend', async () => {
  const { engine, calls } = await makeEngine();
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'hero.glb' });
  const r = await engine.startRun(a.id, { steps: [
    { step: 'cleanup' }, { step: 'rig_humanoid' }, { step: 'godot_package' }, { step: 'render_sprites', params: { profile: 'iso8' } },
  ] });
  const job = await engine.waitJob(r.jobId);
  assert.equal(job.status, 'completed', job.error);
  const v1 = path.join(engine.assetDir(a.id), 'v001');
  const rigBlend = path.join(v1, '02_rig_humanoid', 'rig_humanoid.blend');
  const rigGlb = path.join(v1, '02_rig_humanoid', 'rig_humanoid.glb');
  const sprites = calls.find((c) => c.step === 'render_sprites').request.inputs;
  assert.deepEqual(Object.keys(sprites).sort(), ['blend', 'manifest']);
  assert.equal(sprites.blend, rigBlend);
  assert.ok(existsSync(sprites.manifest), 'render_sprites gets the manifest snapshot (kind -> forward axis)');
  const vj = JSON.parse(readFileSync(path.join(v1, 'version.json'), 'utf8'));
  assert.equal(vj.final.blend, rigBlend);
  assert.equal(vj.final.glb, rigGlb);
  assert.equal(vj.final.exports.godot_package.glb, path.join(v1, '03_godot_package', 'package', 'model.glb'), 'the package is recorded separately');
  assert.ok(vj.final.exports.render_sprites);
  // later runs from 'latest' (export, wizard continuation, asset copies) see the rig, not the package
  calls.length = 0;
  const ex = await engine.startExport(a.id, { profile: 'godot' });
  assert.equal((await engine.waitJob(ex.jobId)).status, 'completed');
  assert.equal(calls[0].request.inputs.glb, rigGlb);
  const m = await engine.getAsset(a.id);
  assert.equal(m.versions.find((v) => v.version === 'v002').final.blend, rigBlend, 'an export-only version keeps the .blend');
  assert.equal((await engine._latestFile(m, ['glb'])).file, rigGlb);
  const copy = await engine.createAsset({ kind: 'humanoid', source: { type: 'asset', assetId: a.id } });
  assert.equal(copy.source.version, 'v002');
  assert.ok(existsSync(path.join(engine.assetDir(copy.id), 'source', 'source.glb')));
});

test('a failing job-file write never crashes the process; the job still finishes and is saved later', async () => {
  const unhandled = [];
  const onUnhandled = (err) => unhandled.push(err);
  process.on('unhandledRejection', onUnhandled);
  const warn = console.warn;
  console.warn = () => {};
  try {
    const { engine } = await makeEngine();
    const a = await engine.createAsset({ kind: 'prop', source: { type: 'upload' } }, { buffer: GLB, originalName: 'p.glb' });
    let failing = true;
    const realWrite = engine._writeJobFile.bind(engine);
    engine._writeJobFile = async (file, value) => {
      if (failing) throw Object.assign(new Error('EPERM: operation not permitted, rename'), { code: 'EPERM' });
      return realWrite(file, value);
    };
    const r = await engine.startRun(a.id, { steps: [{ step: 'inspect' }, { step: 'cleanup' }] });
    const job = await engine.waitJob(r.jobId);
    assert.equal(job.status, 'completed');
    assert.equal((await engine.getAsset(a.id)).versions[0].status, 'completed', 'manifest is still finalised');
    assert.ok(engine.writeFailures.get(r.jobId) > 0);
    // the cancel path (the _enqueue catch handler) must not reject either
    const q = await engine.startRun(a.id, { steps: [{ step: 'inspect' }] });
    await engine.cancelJob(q.jobId);
    await engine.waitJob(q.jobId);
    await sleep(30);
    assert.deepEqual(unhandled, [], 'no unhandled rejection');
    failing = false;
    await engine._writeJob(job, true);
    assert.equal(JSON.parse(readFileSync(path.join(engine.jobsDir, r.jobId + '.json'), 'utf8')).status, 'completed');
    assert.equal(engine.writeFailures.has(r.jobId), false);
  } finally {
    process.off('unhandledRejection', onUnhandled);
    console.warn = warn;
  }
});

test('a queued run is on disk at once, with a heartbeat', async () => {
  const { engine } = await makeEngine({ inspect: hang }, { engine: { persistIntervalMs: 60000 } });
  const a = await engine.createAsset({ kind: 'prop', source: { type: 'upload' } }, { buffer: GLB, originalName: 'p.glb' });
  const r = await engine.startRun(a.id, { steps: [{ step: 'inspect' }] });
  const record = JSON.parse(readFileSync(path.join(engine.jobsDir, r.jobId + '.json'), 'utf8'));
  assert.ok(['queued', 'running'].includes(record.status));
  assert.ok(record.heartbeatAt && record.pid === process.pid);
  await engine.cancelJob(r.jobId);
  assert.equal((await engine.waitJob(r.jobId)).status, 'cancelled');
});

test('jobs of other processes: a live pid with a stale heartbeat is interrupted, a fresh one is external', async () => {
  const { engine, dir } = await makeEngine();
  const a = await engine.createAsset({ kind: 'prop', source: { type: 'upload' } }, { buffer: GLB, originalName: 'p.glb' });
  const record = (heartbeatAgoMs, pid) => {
    const at = new Date(Date.now() - heartbeatAgoMs).toISOString();
    return { id: randomUUID(), type: 'run', assetId: a.id, version: null, status: 'running', pid, host: 'cli', createdAt: at, updatedAt: at, heartbeatAt: at, steps: [{ index: 1, step: 'inspect', status: 'running' }] };
  };
  const livePid = process.ppid; // the test runner: alive, but not the owner of these jobs
  const stale = record(10 * 60 * 1000, livePid);
  const fresh = record(1000, livePid);
  const samePid = record(1000, process.pid); // our pid on a job we did not create = an earlier process
  for (const j of [stale, fresh, samePid]) await fs.writeFile(path.join(engine.jobsDir, j.id + '.json'), JSON.stringify(j));
  const { engine: other } = await makeEngine({}, { dir });
  const s = await other.getJob(stale.id);
  assert.equal(s.status, 'failed');
  assert.match(s.error, /interrupted/);
  assert.equal((await other.getJob(samePid.id)).status, 'failed');
  const f = await other.getJob(fresh.id);
  assert.equal(f.status, 'running');
  assert.equal(f.external, true);
  await assert.rejects(other.cancelJob(fresh.id), (e) => e.code === 'external_job');
  await assert.rejects(other.trashAsset(a.id), (e) => e.status === 409);
  // the owner stops beating (crashed, or its pid now belongs to another program): settled, not stuck
  other.staleMs = 500;
  await sleep(600);
  assert.equal((await other.getJob(fresh.id)).status, 'failed');
  assert.ok((await other.listJobs()).every((j) => j.status === 'failed'));
  assert.equal((await other.trashAsset(a.id)).ok, true);
});

test('a version left queued without a job record is failed on the next start', async () => {
  const { engine, dir } = await makeEngine();
  const a = await engine.createAsset({ kind: 'prop', source: { type: 'upload' } }, { buffer: GLB, originalName: 'p.glb' });
  const entry = (version, jobId) => ({ step: 'inspect', params: {}, status: 'pending', result: null, version, jobId, index: 1, dir: '01_inspect' });
  const old = new Date(Date.now() - 10 * 60 * 1000).toISOString();
  const lost = randomUUID();
  const recent = randomUUID();
  for (const [v, jobId, at] of [['v001', lost, old], ['v002', recent, new Date().toISOString()]]) {
    await fs.mkdir(path.join(engine.assetDir(a.id), v));
    await fs.writeFile(path.join(engine.assetDir(a.id), v, 'version.json'), JSON.stringify({ version: v, jobId, status: 'queued', createdAt: at, pipeline: [entry(v, jobId)], final: null }));
  }
  const file = path.join(engine.assetDir(a.id), 'asset.json');
  const m0 = JSON.parse(readFileSync(file, 'utf8'));
  m0.versions = [
    { version: 'v001', jobId: lost, status: 'queued', createdAt: old, steps: ['inspect'] },
    { version: 'v002', jobId: recent, status: 'queued', createdAt: new Date().toISOString(), steps: ['inspect'] },
  ];
  m0.pipeline = [entry('v001', lost), entry('v002', recent)];
  await fs.writeFile(file, JSON.stringify(m0));
  const { engine: next } = await makeEngine({}, { dir });
  const m = await next.getAsset(a.id);
  assert.equal(m.versions[0].status, 'failed');
  assert.equal(m.versions[1].status, 'queued', 'a version inside the grace period is left alone');
  const v1 = m.pipeline.find((e) => e.version === 'v001');
  assert.equal(v1?.status, 'failed');
  assert.match(v1.result.error, /was not saved/);
  assert.equal(JSON.parse(readFileSync(path.join(next.assetDir(a.id), 'v001', 'version.json'), 'utf8')).status, 'failed');
});

test('manifest pipeline keeps the latest completed recipe while other runs are queued, running or failed', async () => {
  const { engine } = await makeEngine({
    separate: hang,
    rig_humanoid: async () => ({ result: { ok: false, step: 'rig_humanoid', outputs: {}, checks: [], error: 'no arms' }, logTail: [] }),
  }, { engine: { jobConcurrency: 2 } });
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const r1 = await engine.startRun(a.id, { steps: [{ step: 'cleanup' }, { step: 'inspect' }] });
  await engine.waitJob(r1.jobId);
  const slow = await engine.startRun(a.id, { steps: [{ step: 'separate' }, { step: 'inspect' }] }); // v002 keeps running
  for (let i = 0; i < 50 && (await engine.getJob(slow.jobId)).steps[0].status !== 'running'; i += 1) await sleep(20);
  const quick = await engine.startRun(a.id, { steps: [{ step: 'inspect' }], from: 'latest' }); // v003 completes meanwhile
  assert.equal((await engine.waitJob(quick.jobId)).status, 'completed');
  let m = await engine.getAsset(a.id);
  const view = (mm) => mm.pipeline.map((e) => [e.version, e.step, e.status].join(':'));
  assert.deepEqual(view(m), ['v001:cleanup:done', 'v001:inspect:done', 'v003:inspect:done', 'v002:separate:pending', 'v002:inspect:pending'],
    'the running v002 is still visible (and still receives its updates)');
  await engine.cancelJob(slow.jobId);
  await engine.waitJob(slow.jobId);
  m = await engine.getAsset(a.id);
  assert.deepEqual(view(m), ['v001:cleanup:done', 'v001:inspect:done', 'v003:inspect:done'], 'an attempt older than the latest completed version is dropped');
  const bad = await engine.startRun(a.id, { steps: [{ step: 'rig_humanoid' }, { step: 'inspect' }], from: 'latest' });
  assert.equal((await engine.waitJob(bad.jobId)).status, 'failed');
  m = await engine.getAsset(a.id);
  assert.deepEqual(view(m), ['v001:cleanup:done', 'v001:inspect:done', 'v003:inspect:done', 'v004:rig_humanoid:failed', 'v004:inspect:failed']);
  const done = m.pipeline.filter((e) => e.status === 'done');
  assert.deepEqual(done.map((e) => e.step), ['cleanup', 'inspect', 'inspect'], 'replaying the done entries rebuilds v003');
  const summary = (await engine.listAssets()).find((x) => x.id === a.id);
  assert.equal(summary.lastStatus, 'failed');
  assert.match(summary.thumbnail, /v003\/01_inspect\/preview\.png$/, 'the thumbnail survives a failed run');
});

test('a GPU step gives its job-queue place back while it waits for the GPU', async () => {
  let open;
  const gate = new Promise((resolve) => { open = resolve; });
  const { engine, calls } = await makeEngine({
    render_sprites: async (o) => {
      await gate;
      return { result: { ok: true, step: o.step, outputs: {}, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
    },
  });
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const gpuRun = await engine.startRun(a.id, { steps: [{ step: 'render_sprites' }, { step: 'inspect' }] });
  const cpuRun = await engine.startRun(a.id, { steps: [{ step: 'inspect' }] });
  assert.equal((await engine.waitJob(cpuRun.jobId)).status, 'completed', 'the CPU job ran while the GPU job waited');
  assert.equal((await engine.getJob(gpuRun.jobId)).status, 'running');
  open();
  const gpu = await engine.waitJob(gpuRun.jobId);
  assert.equal(gpu.status, 'completed', gpu.error);
  assert.deepEqual(gpu.steps.map((s) => s.status), ['completed', 'completed']);
  assert.equal(calls.find((c) => c.step === 'render_sprites').gpu, true);
  await sleep(10); // waiters are told inside _finishJob, the queue frees the place right after
  assert.equal(engine.jobQueue.stats().running, 0, 'no job-queue place leaked');
});

test('HTTP clients cannot point steps at local files; the CLI (--direct) can', async () => {
  const { engine, stub } = await makeEngine();
  await writeLibrary(engine, LIB);
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const refused = (steps, re = /over HTTP/) => assert.rejects(engine.startRun(a.id, { steps }), (e) => e.code === 'no_local_paths' && e.status === 400 && re.test(e.message));
  await refused([{ step: 'import_mixamo_rig', params: { texture_source: 'C:/secret/model.glb' } }]);
  await refused([{ step: 'import_mixamo_rig', params: { texture_source: 'data/vault.json' } }]);
  await refused([{ step: 'retarget', params: { library: 'D:/other-library', clips: [{ source: 'category:idle' }] } }], /the server fills it in/);
  await refused([{ step: 'retarget', params: { clips: [{ source: 'C:/x/walk.fbx' }] } }]);
  await refused([{ step: 'retarget', params: { clips: [{ source: 'library:mixamo-idle', file: 'C:/x.fbx' }] } }]);
  await refused([{ step: 'cleanup', params: { custom_texture: '\\\\server\\share\\a.png' } }], /looks like a file path/);
  await refused([{ step: 'cleanup', params: { name: '../../evil' } }], /looks like a file path/);
  assert.equal(findLocalPath({ fx: [{ socket: 'socket_thruster_*', color: '#ff0000' }], forward_axis: '-Z' }), null);
  // library ids and categories are fine; the server fills the library folder itself
  const ok = await engine.startRun(a.id, { steps: [{ step: 'cleanup' }, { step: 'retarget', params: { clips: [{ source: 'category:idle' }, { source: 'mixamo-hit' }] } }] });
  assert.equal((await engine.waitJob(ok.jobId)).status, 'completed');
  assert.equal(stub.calls.find((c) => c.step === 'retarget').request.params.library, engine.animationsDir);
  // the CLI engine accepts local paths
  const cli = new ForgeEngine({
    libraryDir: engine.libraryDir, runtimeDir: engine.runtimeDir, studioAssetsDir: engine.studioAssetsDir, studioJobsDir: engine.studioJobsDir,
    runStep: stub.runStep, persistIntervalMs: 0, allowLocalPaths: true,
  });
  await cli.init();
  const local = await cli.startRun(a.id, { steps: [{ step: 'cleanup', params: { custom_texture: 'C:/tex.png' } }] });
  assert.equal((await cli.waitJob(local.jobId)).status, 'completed');
});

test('clip imports run one at a time and remove their staged uploads when they end', async () => {
  let open;
  const gate = new Promise((resolve) => { open = resolve; });
  const { engine } = await makeEngine({
    clip_library: async (o) => {
      await gate;
      return { result: { ok: true, step: o.step, outputs: {}, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
    },
  });
  const staged = path.join(engine.workDir, 'uploads', randomUUID());
  await fs.mkdir(staged, { recursive: true });
  await fs.writeFile(path.join(staged, 'Walking.fbx'), FBX);
  const first = await engine.importClips({ files: [path.join(staged, 'Walking.fbx')], stagedDir: staged });
  await assert.rejects(engine.importClips({ folder: staged }), (e) => e.status === 409 && /already running/.test(e.message));
  await assert.rejects(engine.removeClip('mixamo-anything'), (e) => e.status === 409);
  open();
  assert.equal((await engine.waitJob(first.jobId)).status, 'completed');
  assert.equal(existsSync(staged), false, 'the staging folder is removed after the import');
  // a folder outside runtime/forge/uploads is never removed
  const keep = path.join(engine.runtimeDir, 'keep');
  await fs.mkdir(keep, { recursive: true });
  await fs.writeFile(path.join(keep, 'Idle.fbx'), FBX);
  const second = await engine.importClips({ folder: keep, stagedDir: keep });
  await engine.waitJob(second.jobId);
  assert.ok(existsSync(path.join(keep, 'Idle.fbx')));
});

// ------------------------------------------------------------------ integration round (10.10, real step modules)

test('attach_prop: FBX characters, several items by asset, library clips as inputs.clips', async () => {
  const { engine, calls } = await makeEngine();
  await writeLibrary(engine, LIB);
  const sword = await engine.createAsset({ kind: 'item', name: 'Sword', source: { type: 'params', params: { type: 'sword' } } });
  const shield = await engine.createAsset({ kind: 'item', name: 'Shield', source: { type: 'params', params: { type: 'shield_kite' } } });
  for (const it of [sword, shield]) await engine.waitJob((await engine.startRun(it.id, { steps: [{ step: 'item_kit' }] })).jobId);
  const hero = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: FBX, originalName: 'hero.fbx' });
  calls.length = 0;
  const r = await engine.startRun(hero.id, { steps: [{ step: 'attach_prop', params: {
    attachments: [{ asset: sword.id, hand: 'r' }, { asset: shield.id, hand: 'l', fit: 'auto' }],
    clips: ['category:idle', { source: 'library:mixamo-attack', name: 'attack' }],
  } }] });
  const job = await engine.waitJob(r.jobId);
  assert.equal(job.status, 'completed', job.error);
  const req = calls[0].request;
  assert.equal(req.inputs.character, path.join(engine.assetDir(hero.id), 'source', 'source.fbx'), 'a rigged FBX source is a character');
  assert.equal('item' in req.inputs, false, 'attachments carry their own items');
  assert.deepEqual(req.inputs.clips, [
    { file: path.join(engine.animationsDir, 'mixamo', 'mixamo-idle.fbx'), name: 'idle', loop: true },
    { file: path.join(engine.animationsDir, 'mixamo', 'mixamo-attack.fbx'), name: 'attack', loop: false },
  ]);
  assert.equal('clips' in req.params, false);
  assert.equal(req.params.attachments[0].item, path.join(engine.assetDir(sword.id), 'v001', '01_item_kit', 'item_kit.glb'));
  assert.equal(req.params.attachments[1].hand, 'l');
  const entry = (await engine.getAsset(hero.id)).pipeline.find((e) => e.step === 'attach_prop');
  assert.equal(entry.params.attachments[0].asset, sword.id, 'the recipe keeps the asset ids');
  assert.equal('item' in entry.params.attachments[0], false);
  // HTTP clients name items by asset and clips by library reference only; unknown assets fail before the run
  const refused = (params, re) => assert.rejects(engine.startRun(hero.id, { steps: [{ step: 'attach_prop', params }] }), (e) => re.test(e.message));
  await refused({ attachments: [{ item: 'C:/x/sword.glb' }] }, /over HTTP an item is given as an asset/);
  await refused({ attachments: [{ asset: sword.id }], clips: [{ source: 'library:mixamo-idle', file: 'C:/x.fbx' }] }, /over HTTP only/);
  await refused({ attachments: [{ asset: randomUUID() }] }, /not found/);
});

test('godot_package after render_sprites packs the sprites of the same model', async () => {
  const { engine, calls } = await makeEngine({
    render_sprites: async (o) => {
      const sprites = path.join(o.request.out_dir, 'sprites.json');
      await fs.writeFile(sprites, '{}');
      return { result: { ok: true, step: o.step, outputs: { sprites_json: sprites, sheets: [] }, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
    },
  });
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'h.glb' });
  const r = await engine.startRun(a.id, { steps: [{ step: 'cleanup' }, { step: 'render_sprites', params: { profile: 'iso8' } }, { step: 'godot_package' }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const pkg = calls.find((c) => c.step === 'godot_package').request.inputs;
  assert.equal(pkg.sprites, path.join(engine.assetDir(a.id), 'v001', '02_render_sprites', 'sprites.json'));
  assert.ok(existsSync(pkg.manifest));
  // a new model drops the old sprites
  calls.length = 0;
  const r2 = await engine.startRun(a.id, { steps: [{ step: 'cleanup' }, { step: 'godot_package' }], from: 'latest' });
  assert.equal((await engine.waitJob(r2.jobId)).status, 'completed');
  assert.equal('sprites' in calls.find((c) => c.step === 'godot_package').request.inputs, false);
});

test('a step error reaches the job unchanged (render_sprites units hint through the real Blender runner)', async () => {
  const tmp = tempDir('af-fake-');
  const fake = path.join(tmp, 'fake-blender.mjs');
  const units = 'ValueError: projected model is only 0.4 px (min_body_px=24; model 0.018 m tall at rest, 100 px/m; fix the units with params.height_m or change pixels_per_meter)';
  await fs.writeFile(fake, [
    "const step = process.argv[2];",
    `process.stdout.write('AFERROR ' + JSON.stringify({ step, error: ${JSON.stringify(units)}, trace: 'Traceback ...' }) + '\\n');`,
    'process.exit(1);',
  ].join('\n'));
  const queue = new StepQueue(1);
  const command = ({ step, requestPath }) => ({ command: process.execPath, args: [fake, step, requestPath] });
  const { engine } = await makeEngine({}, { stub: { calls: [], runStep: (o) => runBlenderStep({ ...o, gpu: false, command, queue }) } });
  const a = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'tiny.glb' });
  const job = await engine.waitJob((await engine.startRun(a.id, { steps: [{ step: 'render_sprites', params: { profile: 'iso8' } }] })).jobId);
  assert.equal(job.status, 'failed');
  assert.ok(job.error.includes(units), job.error);
  assert.equal(job.steps[0].result.af_error, units, 'the raw AFERROR text is kept for the UI');
  assert.equal(job.steps[0].result.code, 'step_error');
  const m = await engine.getAsset(a.id);
  assert.ok(m.pipeline[0].result.error.includes('params.height_m'));
});

test('clip_library jobs hold the library lock: they wait for another holder and take over a dead one', async () => {
  const running = [];
  let overlap = 0;
  const { engine, calls } = await makeEngine({
    clip_library: async (o) => {
      running.push(o.request.out_dir);
      if (running.length > 1) overlap += 1;
      assert.ok(existsSync(path.join(engine.animationsDir, LIBRARY_LOCK_FILE)), 'the lock file exists while the step runs');
      await sleep(40);
      running.splice(running.indexOf(o.request.out_dir), 1);
      return { result: { ok: true, step: o.step, outputs: {}, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
    },
  }, { engine: { jobConcurrency: 2, libraryLockPollMs: 20 } });
  const lock = path.join(engine.animationsDir, LIBRARY_LOCK_FILE);
  // two library jobs of one process (the 409 guard bypassed) never run at the same time
  const [j1, j2] = await Promise.all([engine._startLibraryJob({ action: 'preview' }), engine._startLibraryJob({ action: 'preview' })]);
  for (const j of [j1, j2]) assert.equal((await engine.waitJob(j.jobId)).status, 'completed');
  assert.equal(overlap, 0);
  assert.equal(existsSync(lock), false, 'released after the step');

  // another live process holds the library: the job waits, removal is refused, then it runs
  await fs.writeFile(lock, JSON.stringify({ pid: process.ppid, host: 'cli', token: 'other', at: new Date().toISOString() }));
  calls.length = 0;
  const waiting = await engine._startLibraryJob({ action: 'preview' });
  for (let i = 0; i < 50 && !/Waiting for the clip library/.test((await engine.getJob(waiting.jobId)).steps[0].message); i += 1) await sleep(10);
  assert.match((await engine.getJob(waiting.jobId)).steps[0].message, /Waiting for the clip library/);
  assert.equal(calls.length, 0, 'Blender not started while another process holds the library');
  await writeLibrary(engine, LIB);
  await assert.rejects(engine._acquireLibraryLock({ wait: false }), (e) => e.status === 409 && e.code === 'busy');
  await fs.unlink(lock);
  assert.equal((await engine.waitJob(waiting.jobId)).status, 'completed');
  assert.equal(calls.length, 1);

  // a dead owner (or one that stopped refreshing the file) is taken over at once
  await fs.writeFile(lock, JSON.stringify({ pid: 2147483000, host: 'cli', token: 'dead', at: new Date().toISOString() }));
  const removed = await engine.removeClip('mixamo-hit');
  assert.equal(removed.ok, true);
  assert.equal(existsSync(lock), false);
  await fs.writeFile(lock, JSON.stringify({ pid: process.ppid, host: 'cli', token: 'stale', at: '2026-01-01T00:00:00Z' }));
  const old = new Date(Date.now() - 10 * 60 * 1000);
  await fs.utimes(lock, old, old);
  const taken = await engine._startLibraryJob({ action: 'preview' });
  assert.equal((await engine.waitJob(taken.jobId)).status, 'completed');

  // cancelling a job that waits for the library leaves no lock behind
  await fs.writeFile(lock, JSON.stringify({ pid: process.ppid, host: 'cli', token: 'other', at: new Date().toISOString() }));
  const cancelMe = await engine._startLibraryJob({ action: 'preview' });
  for (let i = 0; i < 50 && !/Waiting/.test((await engine.getJob(cancelMe.jobId)).steps[0].message); i += 1) await sleep(10);
  await engine.cancelJob(cancelMe.jobId);
  assert.equal((await engine.waitJob(cancelMe.jobId)).status, 'cancelled');
  assert.equal(JSON.parse(readFileSync(lock, 'utf8')).token, 'other', 'the other holder keeps its lock');
  await fs.unlink(lock);
  const after = await engine._acquireLibraryLock({ wait: false });
  await after();
});

test('creature size: the passport height reaches cleanup and rig_creature; an implausible native size is a warning', async () => {
  let size = [0.38, 1.0, 0.874];
  const { engine, calls } = await makeEngine({
    rig_creature: async (o) => {
      const out = o.request.out_dir;
      const outputs = { glb: path.join(out, 'rig.glb'), blend: path.join(out, 'rig.blend') };
      for (const f of Object.values(outputs)) await fs.writeFile(f, 'x');
      const stats = { template: 'quadruped', size };
      return { result: { ok: true, step: o.step, outputs, stats, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
    },
  });
  const params = (step) => calls.filter((c) => c.step === step).at(-1).request.params;
  // profile.height_m (the wizard stores it) -> cleanup + rig_creature, recorded in the recipe
  const horse = await engine.createAsset({ kind: 'creature', name: 'Horse', profile: { height_m: 2.2 }, source: { type: 'upload' } }, { buffer: GLB, originalName: 'horse.glb' });
  const r1 = await engine.startRun(horse.id, { preset: 'creature_auto' });
  const j1 = await engine.waitJob(r1.jobId);
  assert.equal(j1.status, 'completed');
  assert.equal(params('cleanup').height_m, 2.2);
  assert.equal(params('cleanup').target_tris, 30000);
  assert.equal(params('rig_creature').height_m, 2.2);
  assert.equal('length_m' in params('rig_creature'), false);
  assert.equal((await engine.getAsset(horse.id)).pipeline.find((s) => s.step === 'rig_creature').params.height_m, 2.2);
  // length only: rig_creature scales by length, cleanup keeps the native size
  const wolf = await engine.createAsset({ kind: 'creature', name: 'Wolf', profile: { length_m: 1.6 }, source: { type: 'upload' } }, { buffer: GLB, originalName: 'wolf.glb' });
  await engine.waitJob((await engine.startRun(wolf.id, { preset: 'creature_auto' })).jobId);
  assert.equal(params('rig_creature').length_m, 1.6);
  assert.equal('height_m' in params('cleanup'), false);
  // explicit params win over the passport
  await engine.waitJob((await engine.startRun(horse.id, { steps: [{ step: 'rig_creature', params: { length_m: 2.4 } }] })).jobId);
  assert.equal(params('rig_creature').length_m, 2.4);
  assert.equal('height_m' in params('rig_creature'), false);
  // no size anywhere: native size kept; a plausible one is silent, an implausible one warns
  const bare = await engine.createAsset({ kind: 'creature', name: 'No size', source: { type: 'upload' } }, { buffer: GLB, originalName: 'x.glb' });
  const j3 = await engine.waitJob((await engine.startRun(bare.id, { preset: 'creature_auto' })).jobId);
  assert.equal('height_m' in params('rig_creature'), false);
  assert.deepEqual(j3.warnings.filter((w) => /implausible/.test(w)), []);
  size = [0.02, 0.06, 0.05];
  const j4 = await engine.waitJob((await engine.startRun(bare.id, { preset: 'creature_auto' })).jobId);
  assert.equal(j4.status, 'completed');
  assert.match(j4.warnings.join('\n'), /implausible for template quadruped: height 0\.05 m \(expected 0\.3–6 m\)/);
  assert.match(j4.steps.find((s) => s.step === 'rig_creature').warnings.join('\n'), /implausible/);
});

test('replace_part: the body from the chain, inputs.part from another asset or (CLI) a file', async () => {
  const { engine, calls } = await makeEngine();
  const body = await engine.createAsset({ kind: 'humanoid', name: 'Wizard', source: { type: 'upload' } }, { buffer: GLB, originalName: 'mage.glb' });
  const skull = await engine.createAsset({ kind: 'prop', name: 'Skull', source: { type: 'upload' } }, { buffer: GLB, originalName: 'skull.glb' });
  const region = { min: [-0.2, -0.2, 1.45], max: [0.2, 0.2, 1.85] };
  const r = await engine.startRun(body.id, { steps: [{ step: 'cleanup' }, { step: 'replace_part', params: { part_asset: skull.id, region, eyes: { auto: true } } }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const req = calls.find((c) => c.step === 'replace_part').request;
  assert.equal(req.inputs.mesh, path.join(engine.assetDir(body.id), 'v001', '01_cleanup', 'cleanup.glb'), 'the body is the chain model');
  assert.equal(req.inputs.part, path.join(engine.assetDir(skull.id), 'source', 'source.glb'), 'the part: latest model of the other asset');
  assert.deepEqual(req.params.region, region);
  assert.equal('part_asset' in req.params, false, 'server-only');
  // a part asset with a completed version gives its newest GLB
  await engine.waitJob((await engine.startRun(skull.id, { steps: [{ step: 'cleanup' }] })).jobId);
  calls.length = 0;
  await engine.waitJob((await engine.startRun(body.id, { steps: [{ step: 'replace_part', params: { part_asset: skull.id, region } }] })).jobId);
  assert.equal(calls[0].request.inputs.part, path.join(engine.assetDir(skull.id), 'v001', '01_cleanup', 'cleanup.glb'));
  assert.equal(calls[0].request.inputs.mesh, path.join(engine.assetDir(body.id), 'source', 'source.glb'));
  // preflight: region and a part are required, the part asset must exist; HTTP clients cannot pass part_file
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'replace_part', params: { part_asset: skull.id } }] }), /region/);
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'replace_part', params: { region } }] }), (e) => e.code === 'no_part');
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'replace_part', params: { region, part_asset: randomUUID() } }] }), (e) => e.status === 404);
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'replace_part', params: { region, part_file: 'C:/parts/skull.glb' } }] }), (e) => e.code === 'no_local_paths');
  // the CLI (--direct) may point at a file
  const cli = await makeEngine({}, { engine: { allowLocalPaths: true } });
  const b2 = await cli.engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'b.glb' });
  await cli.engine.waitJob((await cli.engine.startRun(b2.id, { steps: [{ step: 'replace_part', params: { region, part_file: 'C:/parts/skull.glb' } }] })).jobId);
  assert.equal(cli.calls[0].request.inputs.part, 'C:/parts/skull.glb');
  assert.equal('part_file' in cli.calls[0].request.params, false);
});

test('place_parts: the target from the chain, inputs.part from part_asset, points[].asset resolve to their GLB; paths only via the CLI', async () => {
  const { engine, calls } = await makeEngine();
  const body = await engine.createAsset({ kind: 'humanoid', name: 'Wizard v2', source: { type: 'upload' } }, { buffer: GLB, originalName: 'mage.glb' });
  const toggle = await engine.createAsset({ kind: 'prop', name: 'Clasp', source: { type: 'params', params: { part: 'bone_toggle' } } });
  const hand = await engine.createAsset({ kind: 'prop', name: 'Hand', source: { type: 'upload' } }, { buffer: GLB, originalName: 'hand.glb' });
  // the toggle is a bone_kit generator asset: its newest completed GLB is the part
  await engine.waitJob((await engine.startRun(toggle.id, { steps: [{ step: 'bone_kit' }] })).jobId);
  const toggleGlb = path.join(engine.assetDir(toggle.id), 'v001', '01_bone_kit', 'bone_kit.glb');
  const handGlb = path.join(engine.assetDir(hand.id), 'source', 'source.glb');
  const points = [
    [-0.08, 1.32],
    { at: [0.08, -0.1, 1.32], name: 'clasp_r' },
    { at: [0.31, -0.02, 1.0], asset: hand.id, snap: 'none', rot_deg: [0, 0, 90] },
    { at: [-0.31, -0.02, 1.0], asset: hand.id, snap: 'none' },
  ];
  calls.length = 0;
  const r = await engine.startRun(body.id, { steps: [{ step: 'cleanup' }, { step: 'place_parts', params: { part_asset: toggle.id, points, ray: '+Y' } }, { step: 'rig_humanoid' }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const req = calls.find((c) => c.step === 'place_parts').request;
  assert.equal(req.inputs.mesh, path.join(engine.assetDir(body.id), 'v001', '01_cleanup', 'cleanup.glb'), 'the target is the chain model');
  assert.equal(req.inputs.part, toggleGlb, 'the default part: latest model of the part asset');
  assert.deepEqual(req.params.points.map((p) => (Array.isArray(p) ? null : p.part ?? null)), [null, null, handGlb, handGlb], 'points[].asset -> part');
  assert.equal(req.params.points[2].asset, hand.id, 'the UUID stays next to the resolved file');
  assert.equal(req.params.ray, '+Y');
  assert.equal('part_asset' in req.params, false, 'server-only');
  assert.equal(calls.find((c) => c.step === 'rig_humanoid').request.inputs.blend,
    path.join(engine.assetDir(body.id), 'v001', '02_place_parts', 'place_parts.blend'), 'the next step takes the placed model');
  const m = await engine.getAsset(body.id);
  assert.deepEqual(m.pipeline.find((e) => e.step === 'place_parts').params.points, points, 'the recipe keeps the asset UUIDs, not the files');

  // every point may bring its own part: no default part needed
  calls.length = 0;
  const own = [{ at: [0.31, -0.02, 1.0], asset: hand.id, snap: 'none' }, { at: [0, 1.2], asset: toggle.id }];
  await engine.waitJob((await engine.startRun(body.id, { steps: [{ step: 'place_parts', params: { points: own } }] })).jobId);
  assert.equal(calls[0].request.inputs.part, undefined);
  assert.equal(calls[0].request.inputs.mesh, path.join(engine.assetDir(body.id), 'source', 'source.glb'));
  assert.deepEqual(calls[0].request.params.points.map((p) => p.part), [handGlb, toggleGlb]);

  // preflight: points are required, a part is required, part assets must exist
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'place_parts', params: { part_asset: toggle.id } }] }), (e) => e.code === 'bad_params' && /points/.test(e.message));
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'place_parts', params: { points: [[0, 1.2]] } }] }), (e) => e.code === 'no_part');
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'cleanup' }, { step: 'place_parts', params: { points: [[0, 1.2], { at: [0, 1], asset: hand.id }] } }] }), (e) => e.code === 'no_part');
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'place_parts', params: { points: [[0, 1.2]], part_asset: randomUUID() } }] }), (e) => e.status === 404);
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'cleanup' }, { step: 'place_parts', params: { part_asset: toggle.id, points: [{ at: [0, 1], asset: randomUUID() }] } }] }),
    (e) => e.status === 404 && /part of point #1/.test(e.message));
  // HTTP clients cannot pass part_file or a part path in a point (not even a relative one)
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'place_parts', params: { points: [[0, 1.2]], part_file: 'C:/parts/toggle.glb' } }] }), (e) => e.code === 'no_local_paths');
  await assert.rejects(engine.startRun(body.id, { steps: [{ step: 'place_parts', params: { part_asset: toggle.id, points: [{ at: [0, 1], part: 'hand.glb' }] } }] }),
    (e) => e.code === 'no_local_paths' && /Point #1/.test(e.message));

  // the CLI (--direct) may point at files: part_file -> inputs.part, points[].part passes through
  const cli = await makeEngine({}, { engine: { allowLocalPaths: true } });
  const b2 = await cli.engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'b.glb' });
  const cliPoints = [[0, 1.2], { at: [0.3, -0.02, 1.0], part: 'C:/parts/hand_r.glb', snap: 'none' }];
  await cli.engine.waitJob((await cli.engine.startRun(b2.id, { steps: [{ step: 'place_parts', params: { part_file: 'C:/parts/toggle.glb', points: cliPoints } }] })).jobId);
  assert.equal(cli.calls[0].request.inputs.part, 'C:/parts/toggle.glb');
  assert.equal('part_file' in cli.calls[0].request.params, false);
  assert.deepEqual(cli.calls[0].request.params.points, cliPoints);
});

test('bone_kit is a generator: no inputs, its batch outputs feed the chain', async () => {
  const { engine, calls } = await makeEngine();
  const staff = await engine.createAsset({ kind: 'item', name: 'Bone staff', source: { type: 'params', params: { part: 'bone_staff', seed: 2 } } });
  const r = await engine.startRun(staff.id, { steps: [{ step: 'bone_kit' }, { step: 'godot_package' }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const bk = calls.find((c) => c.step === 'bone_kit').request;
  assert.deepEqual(bk.inputs, {});
  assert.equal(bk.params.part, 'bone_staff', 'asset source params feed the generator');
  assert.equal(calls.find((c) => c.step === 'godot_package').request.inputs.glb, path.join(engine.assetDir(staff.id), 'v001', '01_bone_kit', 'bone_kit.glb'));
});

test('kitbash takes the ship .blend of the chain; paint.asset resolves to a studio image, paths only via the CLI', async () => {
  const { engine, calls } = await makeEngine();
  const imgId = randomUUID();
  await fs.writeFile(path.join(engine.studioAssetsDir, imgId + '.png'), 'png');
  const ship = await engine.createAsset({ kind: 'ship', name: 'Ship 03', source: { type: 'upload' } }, { buffer: GLB, originalName: 'raw.glb' });
  const parts = [{ kind: 'gatling', at: 'socket_turret_*', length: 5 }, { kind: 'nozzle', at: 'socket_thruster_*' },
    { kind: 'piping', points: [[0, -25, 3], [5, -27, 1]], radius: 0.2, mirror: 'x' }];
  const r = await engine.startRun(ship.id, { steps: [
    { step: 'ship_from_mesh', params: { class: 'gunship', paint: { asset: imgId, camera: 'fit', mirror: 'x' }, palette: 'input' } },
    { step: 'kitbash', params: { parts } },
    { step: 'godot_package', params: { profile: 'godot' } }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const sfm = calls.find((c) => c.step === 'ship_from_mesh').request;
  assert.equal(sfm.params.paint.image, path.join(engine.studioAssetsDir, imgId + '.png'), 'studio image path for Blender');
  const kb = calls.find((c) => c.step === 'kitbash').request;
  assert.equal(kb.inputs.blend, path.join(engine.assetDir(ship.id), 'v001', '01_ship_from_mesh', 'ship_from_mesh.blend'));
  assert.deepEqual(kb.params.parts, parts);
  assert.equal(calls.find((c) => c.step === 'godot_package').request.inputs.glb,
    path.join(engine.assetDir(ship.id), 'v001', '02_kitbash', 'kitbash.glb'), 'the package takes the kitbashed GLB');
  // HTTP: a local concept path is refused; a missing studio image is a 404 before Blender runs
  await assert.rejects(engine.startRun(ship.id, { steps: [{ step: 'ship_from_mesh', params: { paint: { image: 'C:/concepts/sq03.png' } } }] }),
    (e) => e.code === 'no_local_paths');
  calls.length = 0;
  const bad = await engine.startRun(ship.id, { steps: [{ step: 'ship_from_mesh', params: { paint: { asset: randomUUID() } } }] });
  assert.equal((await engine.waitJob(bad.jobId)).status, 'failed');
  assert.equal(calls.length, 0, 'Blender is not started without the concept image');
  // the CLI (--direct) passes the concept path through
  const cli = await makeEngine({}, { engine: { allowLocalPaths: true } });
  const s2 = await cli.engine.createAsset({ kind: 'ship', source: { type: 'upload' } }, { buffer: GLB, originalName: 'raw.glb' });
  await cli.engine.waitJob((await cli.engine.startRun(s2.id, { steps: [{ step: 'ship_from_mesh', params: { paint: { image: 'C:/c/sq03.png' } } }] })).jobId);
  assert.equal(cli.calls[0].request.params.paint.image, 'C:/c/sq03.png');
});

test('assemble_parts: the whole-body base of the chain + the generation reference + parts.json (CLI only)', async () => {
  const { engine } = await makeEngine();
  // HTTP: a local path for the parts file / the concept is refused before Blender runs
  const up = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'militia.glb' });
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'assemble_parts', params: { parts_file: 'C:/p/parts.json' } }] }), (e) => e.code === 'no_local_paths');
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'assemble_parts', params: { concept_file: 'C:/p/front.png' } }] }), (e) => e.code === 'no_local_paths');
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'assemble_parts', params: { refine: 'maybe' } }] }), (e) => e.code === 'bad_params');
  // the CLI (--direct): the upload's own GLB is the base, concept_file -> inputs.concept, parts_file -> inputs.parts, none of them is a Blender param
  const cli = await makeEngine({}, { engine: { allowLocalPaths: true } });
  const u2 = await cli.engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'militia.glb' });
  await cli.engine.waitJob((await cli.engine.startRun(u2.id, { steps: [{ step: 'assemble_parts', params: {
    parts_file: 'C:/p/parts.json', concept_file: 'C:/p/ref_front.png', voxel_m: 0.003, export_mv: true } }] })).jobId);
  const req = cli.calls[0].request;
  assert.equal(req.step, 'assemble_parts');
  assert.equal(req.inputs.mesh, path.join(cli.engine.assetDir(u2.id), 'source', 'source.glb'));
  assert.equal(req.inputs.concept, 'C:/p/ref_front.png');
  assert.equal(req.inputs.parts, 'C:/p/parts.json');
  assert.deepEqual(req.params, { voxel_m: 0.003, export_mv: true });
  // without parts_file the run is refused with a clear message
  await assert.rejects(cli.engine.startRun(u2.id, { steps: [{ step: 'assemble_parts', params: { concept_file: 'C:/p/ref_front.png' } }] }),
    (e) => e.code === 'no_parts' && /parts_file/.test(e.message));
});

test('paint_from_concept: the chain .blend + the generation reference (with the original and framing), concept_asset, CLI concept_file', async () => {
  const { engine, calls } = await makeEngine();
  // a finished generation job: GLB, prepared concept with its RGBA reference, the original upload
  const genAsset = randomUUID();
  const refId = randomUUID();
  const origId = randomUUID();
  const genJob = randomUUID();
  await fs.writeFile(path.join(engine.studioAssetsDir, genAsset + '.glb'), GLB);
  await fs.writeFile(path.join(engine.studioAssetsDir, refId + '.png'), 'png');
  await fs.writeFile(path.join(engine.studioAssetsDir, origId + '.jpg'), 'jpg');
  const framing = { originalSize: [1376, 768], crop: [286, 28, 1044, 759], size: 910, contentRect: [76, 89, 758, 731], upscaled: false };
  await fs.writeFile(path.join(engine.studioJobsDir, genJob + '.json'), JSON.stringify({
    id: genJob, status: 'completed', prompt: '', imageId: origId, asset: { id: genAsset, extension: '.glb' },
    preparedConcept: { id: randomUUID(), extension: '.png', reference: { id: refId, extension: '.png', originalId: origId, framing } },
  }));
  const knight = await engine.createAsset({ kind: 'humanoid', name: 'Knight', source: { type: 'job', jobId: genJob } });
  const r = await engine.startRun(knight.id, { steps: [{ step: 'cleanup', params: { target_tris: 40000 } }, { step: 'paint_from_concept' }, { step: 'rig_humanoid' }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const req = calls.find((c) => c.step === 'paint_from_concept').request;
  const v = path.join(engine.assetDir(knight.id), 'v001');
  assert.equal(req.inputs.blend, path.join(v, '01_cleanup', 'cleanup.blend'), 'the cleanup .blend of the chain');
  assert.equal(req.inputs.concept, path.join(engine.studioAssetsDir, refId + '.png'), 'the RGBA reference of the generation');
  assert.deepEqual(req.inputs.concept_original, { image: path.join(engine.studioAssetsDir, origId + '.jpg'), crop: framing.crop, content_rect: framing.contentRect });
  assert.equal(calls.find((c) => c.step === 'rig_humanoid').request.inputs.blend, path.join(v, '02_paint_from_concept', 'paint_from_concept.blend'),
    'the rig takes the painted .blend');
  // a copy of that forge asset still finds the job's reference
  const copy = await engine.createAsset({ kind: 'humanoid', source: { type: 'asset', assetId: knight.id } });
  calls.length = 0;
  await engine.waitJob((await engine.startRun(copy.id, { steps: [{ step: 'paint_from_concept' }] })).jobId);
  assert.equal(calls[0].request.inputs.concept, path.join(engine.studioAssetsDir, refId + '.png'));
  // concept_asset: a studio image wins over the job reference and is not sent to Blender as a param
  const other = randomUUID();
  await fs.writeFile(path.join(engine.studioAssetsDir, other + '.png'), 'png');
  calls.length = 0;
  await engine.waitJob((await engine.startRun(knight.id, { steps: [{ step: 'paint_from_concept', params: { concept_asset: other, size: 1024 } }] })).jobId);
  assert.equal(calls[0].request.inputs.concept, path.join(engine.studioAssetsDir, other + '.png'));
  assert.equal(calls[0].request.inputs.concept_original, undefined);
  assert.equal('concept_asset' in calls[0].request.params, false, 'server-only');
  assert.equal(calls[0].request.params.size, 1024);
  // without a concept: an upload is refused before Blender runs; a missing studio image is a 404
  const up = await engine.createAsset({ kind: 'humanoid', source: { type: 'upload' } }, { buffer: GLB, originalName: 'hero.glb' });
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'paint_from_concept' }] }), (e) => e.code === 'no_concept' && /concept_asset/.test(e.message));
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'paint_from_concept', params: { concept_asset: randomUUID() } }] }), (e) => e.status === 404);
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'paint_from_concept', params: { concept_file: 'C:/concepts/knight.png' } }] }),
    (e) => e.code === 'no_local_paths');
  await assert.rejects(engine.startRun(up.id, { steps: [{ step: 'paint_from_concept', params: { camera: { yaw_deg: 10 } } }] }),
    (e) => e.code === 'bad_params' && /px_per_m/.test(e.message));
  // the CLI (--direct) passes a concept file through; the upload's own GLB is the model
  const cli = await makeEngine({}, { engine: { allowLocalPaths: true } });
  const u2 = await cli.engine.createAsset({ kind: 'creature', source: { type: 'upload' } }, { buffer: GLB, originalName: 'wolf.glb' });
  await cli.engine.waitJob((await cli.engine.startRun(u2.id, { steps: [{ step: 'paint_from_concept', params: { concept_file: 'C:/c/wolf.png' } }] })).jobId);
  assert.equal(cli.calls[0].request.inputs.concept, 'C:/c/wolf.png');
  assert.equal(cli.calls[0].request.inputs.mesh, path.join(cli.engine.assetDir(u2.id), 'source', 'source.glb'));
  assert.equal(cli.calls[0].request.asset.kind, 'creature', 'the step reads the kind (wider yaw search, no head guard)');
});

test('extract_props -> rig_humanoid -> attach_prop extracted: props.json reaches attach_prop in the same run, on the rig .blend', async () => {
  const extractStub = async (o) => {
    const out = o.request.out_dir;
    const outputs = { glb: path.join(out, 'body.glb'), blend: path.join(out, 'body.blend'), props_json: path.join(out, 'props.json'),
      items: [path.join(out, 'items', 'r_sword', 'item.glb')], preview: path.join(out, 'exploded.png') };
    await fs.mkdir(path.join(out, 'items', 'r_sword'), { recursive: true });
    for (const f of [outputs.glb, outputs.blend, outputs.items[0], outputs.preview]) await fs.writeFile(f, 'x');
    await fs.writeFile(outputs.props_json, JSON.stringify({ items: [{ id: 'r_sword', hand: 'r', files: { glb: outputs.items[0] } }] }));
    return { result: { ok: true, step: o.step, outputs, stats: {}, checks: [], warnings: [] }, logTail: [], logFile: null, warnings: [] };
  };
  const { engine, calls } = await makeEngine({ extract_props: extractStub });
  const knight = await engine.createAsset({ kind: 'humanoid', name: 'Knight', source: { type: 'upload' } }, { buffer: GLB, originalName: 'k.glb' });
  const r = await engine.startRun(knight.id, { steps: [
    { step: 'extract_props', params: { hints: { hand_weapon: 'sword', hand: 'r', shield: true }, front_prior: '-Y' } },
    { step: 'rig_humanoid', params: { hard_surface: 'auto', build: 'armored' } },
    { step: 'attach_prop', params: { extracted: true, extracted_mode: 'skin_separate' } },
    { step: 'retarget', params: { clips: [] } }] });
  assert.equal((await engine.waitJob(r.jobId)).status, 'completed');
  const v = path.join(engine.assetDir(knight.id), 'v001');
  const ex = calls.find((c) => c.step === 'extract_props').request;
  assert.deepEqual(ex.params.hints, { hand_weapon: 'sword', hand: 'r', shield: true });
  assert.equal(calls.find((c) => c.step === 'rig_humanoid').request.inputs.blend, path.join(v, '01_extract_props', 'body.blend'), 'the body without the props is rigged');
  const ap = calls.find((c) => c.step === 'attach_prop').request;
  assert.equal(ap.inputs.extracted, path.join(v, '01_extract_props', 'props.json'), 'props.json of this run, carried past the rig step');
  assert.equal(ap.inputs.character, path.join(v, '02_rig_humanoid', 'rig_humanoid.blend'), 'the rig .blend (its placement sockets live on the hand bones)');
  assert.equal(ap.inputs.item, undefined, 'no passport prop needed');
  assert.equal('extracted' in ap.params, false, 'server-only flag');
  assert.equal(ap.params.extracted_mode, 'skin_separate');
  // the batch order: attach_prop after retarget (the shield mount is fitted over the clips) - props.json is
  // carried past rig_humanoid and retarget, the character is the retargeted .blend
  calls.length = 0;
  const r2 = await engine.startRun(knight.id, { steps: [
    { step: 'extract_props', params: { hints: { shield: true } } },
    { step: 'rig_humanoid', params: {} },
    { step: 'retarget', params: { clips: [] } },
    { step: 'attach_prop', params: { extracted: true, extracted_mode: 'skin_separate', extracted_shield_fit: 'auto' } }] });
  assert.equal((await engine.waitJob(r2.jobId)).status, 'completed');
  const v2 = path.join(engine.assetDir(knight.id), 'v002');
  const ap2 = calls.find((c) => c.step === 'attach_prop').request;
  assert.equal(ap2.inputs.extracted, path.join(v2, '01_extract_props', 'props.json'));
  assert.equal(ap2.inputs.character, calls.find((c) => c.step === 'retarget').request.out_dir.replace(/[\\/]+$/, '') + path.sep + 'retarget.blend');
  assert.equal(ap2.params.extracted_shield_fit, 'auto');
  await assert.rejects(engine.startRun(knight.id, { steps: [{ step: 'extract_props' }, { step: 'attach_prop', params: { extracted: true, extracted_shield_fit: 'sideways' } }] }),
    /extracted_shield_fit/);
  // attach_prop extracted without an extract_props step in the run: refused before Blender runs
  calls.length = 0;
  await assert.rejects(engine.startRun(knight.id, { steps: [{ step: 'attach_prop', params: { extracted: true } }] }),
    (e) => e.code === 'no_extracted' && /extract_props/.test(e.message));
  assert.equal(calls.length, 0);
  // the chain state keeps props.json across steps that do not write one
  const st = ForgeEngine.advanceState({ blend: 'a.blend', glb: 'a.glb', extracted: 'p.json' }, { blend: 'b.blend', glb: 'b.glb' });
  assert.equal(st.extracted, 'p.json');
  assert.equal(ForgeEngine.advanceState({}, { blend: 'b.blend', props_json: 'q.json' }).extracted, 'q.json');
});
