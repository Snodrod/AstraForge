// Review view: GET /api/forge/review, decisions through PATCH qa, review_decisions.json export
// (server/forge-review.mjs) and the pure helpers of the view (src/forge/review-core.js).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdtempSync, existsSync, readFileSync, promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import express from 'express';
import { ForgeEngine } from '../server/forge-pipeline.mjs';
import { registerForge } from '../server/forge.mjs';
import { ForgeReview, explainFailure, tearSummary } from '../server/forge-review.mjs';
import { listSteps } from '../server/forge-steps.mjs';
import {
  suggestUnit, toggleReason, hasReason, splitReasons, moveSelection, neighbourId, filterItems, reviewCounts, chipCounts,
  decisionPatch, withDecision, reviewKeyAction, percent, rowKey, matchesBuild, distinct, REWORK_PRESETS,
} from '../src/forge/review-core.js';

const dir = mkdtempSync(path.join(tmpdir(), 'af-review-'));
const libraryDir = path.join(dir, 'library');
const studioAssets = path.join(dir, 'studio', 'assets');
const batchDir = path.join(dir, 'incoming', 'build');
const stateFile = path.join(batchDir, 'build.state.json');
const decisionsFile = path.join(batchDir, 'review_decisions.json');
const titleOf = (step) => listSteps().find((s) => s.step === step)?.title || step;

let engine;
let server;
let base;
let review;
const ids = {};
const imageIds = {};

const deform = (torn, robe, ok) => ({ worst_torn_frac: torn, worst_robe_frac: robe, worst_limit: 0.01, worst_frame: 7, over_frames: ok ? 0 : 9, frames_measured: 12, torn_by_bone_m2: { LeftUpLeg: 0.2, Hips: 0.1 }, ok });

async function touch(file, content = 'x') {
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, content);
}

/** An asset manifest with step entries whose result files exist on disk. */
async function makeAsset({ name, steps, qa, source, imageId }) {
  const id = randomUUID();
  const assetDir = path.join(libraryDir, 'assets', id);
  const pipeline = [];
  let index = 0;
  for (const s of steps) {
    index += 1;
    const dirName = `${String(index).padStart(2, '0')}_${s.step}`;
    const outputs = {};
    for (const [key, file] of Object.entries(s.files || {})) {
      const abs = path.join(assetDir, 'v001', dirName, file);
      if (s.exists !== false) await touch(abs);
      outputs[key] = s.stale ? `C:\\old\\library\\assets\\${id}\\v001\\${dirName}\\${file}` : abs;
    }
    for (const file of s.extraFiles || []) await touch(path.join(assetDir, 'v001', dirName, file));
    pipeline.push({
      step: s.step, params: {}, status: s.status || 'done', version: 'v001', jobId: 'job', index, dir: dirName,
      result: { ok: (s.status || 'done') === 'done', step: s.step, outputs, stats: s.stats || {}, ...(s.error ? { error: s.error } : {}), ...(s.skipped ? { skipped: true } : {}) },
    });
  }
  await touch(path.join(assetDir, 'source', 'source.glb'));
  const manifest = {
    id, name, kind: 'humanoid', created: '2026-10-10T00:00:00.000Z', updated: '2026-10-10T00:00:00.000Z',
    source: { type: 'job', jobId: randomUUID(), image: imageId || null, file: 'source/source.glb', ...(source || {}) },
    profile: { height_m: 1.8 }, pipeline, clips: [], props: [], fx: [], exports: [], qa: { checks: [], reviewed: false, ...(qa || {}) }, versions: [],
  };
  await fs.writeFile(path.join(assetDir, 'asset.json'), JSON.stringify(manifest, null, 2));
  return id;
}

const FULL = (motionStats) => [
  { step: 'cleanup', files: { glb: 'clean.glb', preview: 'preview.png' } },
  { step: 'paint_from_concept', files: { compare: 'compare.png', glb: 'painted.glb' } },
  { step: 'rig_humanoid', files: { markers_png: 'markers.png', poses_png: 'poses.png', glb: 'rigged.glb' } },
  { step: 'retarget', files: { glb: 'animated.glb', preview: 'preview.png' } },
  { step: 'motion_style', files: { glb: 'styled.glb', preview: 'preview.png' }, stats: motionStats },
  { step: 'render_sprites', files: { preview: 'preview.png' } },
  { step: 'godot_package', files: { glb: 'pkg/nested.glb' } },
];

async function writeState(items) {
  await fs.mkdir(batchDir, { recursive: true });
  await fs.writeFile(stateFile, JSON.stringify({ items, watch_stalled: null }));
}

before(async () => {
  await fs.mkdir(studioAssets, { recursive: true });
  for (const k of ['knight', 'acolyte', 'archer']) {
    imageIds[k] = randomUUID();
    await fs.writeFile(path.join(studioAssets, `${imageIds[k]}.json`), JSON.stringify({ id: imageIds[k], extension: '.jpg' }));
  }
  engine = new ForgeEngine({
    libraryDir, runtimeDir: path.join(dir, 'runtime'), studioAssetsDir: studioAssets, studioJobsDir: path.join(dir, 'studio', 'jobs'),
    persistIntervalMs: 0,
    blenderInfo: () => ({ path: 'C:/fake/blender.exe', ok: true, version: '5.0.1' }),
    godotInfo: () => ({ path: null, ok: false, version: null }),
  });
  await engine.init();
  ids.knight = await makeAsset({ name: 'Knight holding sword and shield', imageId: imageIds.knight, steps: FULL({ deform_qa: { 'idle-loop': deform(0.004, 0.01, true), 'walk-loop': deform(0.008, 0.02, true) }, deform_gate: 'pass' }) });
  ids.acolyte = await makeAsset({
    name: 'Acolyte holding staff and book', imageId: imageIds.acolyte,
    steps: FULL({ deform_qa: { 'idle-loop': deform(0.1295, 0.1588, false), death: deform(0.3074, 0.4911, false), 'walk-loop': deform(0.0059, 0.01, true) }, deform_gate: 'warning' }),
  });
  ids.archer = await makeAsset({
    name: 'Arcane archer shooting blue light', imageId: imageIds.archer,
    steps: [
      { step: 'cleanup', files: { glb: 'clean.glb' } },
      { step: 'paint_from_concept', files: { compare: 'compare.png', glb: 'painted.glb' } },
      { step: 'rig_humanoid', status: 'failed', files: {}, extraFiles: ['markers.png', 'markers.json'], error: 'rig not built: not a humanoid of supported proportions: left arm 0.74 H (humanoid range 0.15..0.50)' },
      { step: 'retarget', status: 'failed', skipped: true, error: 'Skipped after an error' },
    ],
  });
  ids.moved = await makeAsset({ name: 'Moved library', steps: [{ step: 'cleanup', files: { glb: 'clean.glb', preview: 'preview.png' }, stale: true }, { step: 'paint_from_concept', files: { compare: 'compare.png' }, stale: true }] });
  ids.empty = await makeAsset({ name: 'Never built', steps: [] });
  await writeState({
    'Knight_holding_sword_and_shield_1.jpg': { title: 'Knight holding sword and shield', role: 'melee_shield', faction: 'order', assetId: ids.knight, status: 'done' },
    'Acolyte_holding_staff_and_book_1.jpg': { title: 'Acolyte holding staff and book', role: 'caster', faction: 'order', assetId: ids.acolyte, imageId: imageIds.acolyte, status: 'failed_qa' },
    'Arcane_archer_1.jpg': { title: 'Arcane archer shooting blue light', role: 'ranged', faction: 'order', assetId: ids.archer, status: 'failed_forge', failedStep: 'rig_humanoid' },
    'Ghost_1.jpg': { title: 'Ghost floating with glowing eyes', role: 'skip', faction: 'undead', status: 'skipped' },
    'Lost_1.jpg': { title: 'Lost passport', role: 'melee', faction: 'undead', assetId: randomUUID(), status: 'failed_qa' },
    'Moved_1.jpg': { title: 'Moved library', role: 'melee', faction: 'order', assetId: ids.moved, status: 'failed_forge' },
    'Empty_1.jpg': { title: 'Never built', role: 'melee', faction: 'order', assetId: ids.empty },
  });
  review = new ForgeReview(engine, { stateFile, decisionsFile });

  const app = express();
  app.use((req, res, next) => {
    const host = req.headers.host?.split(':')[0];
    if (!['127.0.0.1', 'localhost', '['].includes(host)) return res.status(403).json({ error: 'Invalid Host' });
    if (!['GET', 'HEAD'].includes(req.method) && req.path.startsWith('/api') && req.headers['x-astra-client'] !== 'studio') return res.status(403).json({ error: 'The request must be sent from the studio.' });
    next();
  });
  app.use(express.json({ limit: '90mb' }));
  registerForge(app, null, { engine, review: { stateFile, decisionsFile } });
  await new Promise((resolve) => { server = app.listen(0, '127.0.0.1', resolve); });
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server?.closeAllConnections?.();
  await new Promise((resolve) => server.close(resolve));
  await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
});

const H = { 'x-astra-client': 'studio' };
async function api(method, url, body, headers = H) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) { init.body = JSON.stringify(body); init.headers['content-type'] = 'application/json'; }
  const res = await fetch(base + url, init);
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { json = null; }
  return { status: res.status, json, text, headers: res.headers };
}
const row = (list, id) => list.items.find((r) => r.assetId === id);

// ------------------------------------------------------------------------------------ server rows

test('review: rows get a status from the manifest, with URLs that the file route serves', async () => {
  const res = await api('GET', '/api/forge/review');
  assert.equal(res.status, 200);
  const list = res.json;
  assert.equal(list.batch.name, 'build');
  assert.equal(list.batch.source, 'state');
  assert.equal(list.batch.decisionsFile, decisionsFile);
  assert.equal(list.items.length, 7);
  assert.deepEqual(list.items.map((r) => r.status), ['built', 'failed_qa', 'failed_forge', 'skipped', 'missing', 'incomplete', 'new']);

  const knight = row(list, ids.knight);
  assert.equal(knight.status, 'built');
  assert.equal(knight.role, 'melee_shield');
  assert.equal(knight.faction, 'order');
  assert.equal(knight.canApprove, true);
  assert.equal(knight.urls.concept, `/api/assets/${imageIds.knight}`);
  assert.equal(knight.urls.glb, `/api/forge/assets/${ids.knight}/files/v001/05_motion_style/styled.glb`, 'final model = styled.glb, not the Godot package copy');
  assert.equal(knight.glbStep, 'motion_style');
  assert.equal(knight.tear.ok, true);
  assert.equal(knight.decision, null);
  // every URL of a row is really served (extension whitelist, no traversal)
  for (const url of [knight.urls.compare, knight.urls.motion, knight.urls.sprites, knight.urls.markers, knight.urls.poses, knight.urls.glb]) {
    assert.ok(url, 'URL present');
    assert.equal((await fetch(base + url)).status, 200, url);
  }
});

test('review: tear numbers come from the motion_style result and turn into a plain-Russian reason', async () => {
  const list = (await api('GET', '/api/forge/review')).json;
  const acolyte = row(list, ids.acolyte);
  assert.equal(acolyte.status, 'failed_qa');
  assert.equal(acolyte.label, 'Mesh tears');
  assert.equal(acolyte.canApprove, true, 'a torn but fully built character can still be approved');
  assert.equal(acolyte.tear.gate, 'warning');
  assert.equal(acolyte.tear.failedClips, 2);
  assert.deepEqual(acolyte.tear.worst, { clip: 'death', tornPct: 30.7, robePct: 49.1, limitPct: 1 });
  assert.equal(acolyte.tear.clips.length, 3);
  assert.deepEqual(acolyte.tear.clips.find((c) => c.clip === 'idle-loop'), { clip: 'idle-loop', tornPct: 13, robePct: 15.9, limitPct: 1, ok: false, frame: 7, overFrames: 9, frames: 12, bones: ['LeftUpLeg', 'Hips'] });
  assert.match(acolyte.reason, /Built in full, but the mesh tears: worst clip "death" — 30\.7% of the surface \(limit 1%\)/);
  assert.match(acolyte.reason, /Clips with tearing: 2 of 3/);
});

test('review: a failed rig shows markers.png (found by convention), the error in Russian and the model without a skeleton', async () => {
  const list = (await api('GET', '/api/forge/review')).json;
  const archer = row(list, ids.archer);
  assert.equal(archer.status, 'failed_forge');
  assert.equal(archer.failedStep, 'rig_humanoid');
  assert.equal(archer.label, `Stopped: ${titleOf('rig_humanoid')}`);
  assert.equal(archer.reason, 'Rig not built: the model does not look like a human — left arm is 0.74 of body height (humanoid range 0.15–0.50).');
  assert.match(archer.error, /^rig not built:/);
  assert.equal(archer.canApprove, false);
  assert.equal(archer.urls.markers, `/api/forge/assets/${ids.archer}/files/v001/03_rig_humanoid/markers.png`);
  assert.equal(archer.urls.poses, null);
  assert.equal(archer.urls.motion, null);
  assert.equal(archer.urls.sprites, null);
  assert.equal(archer.glbStep, 'paint_from_concept');
  assert.equal(archer.tear, null);
  assert.equal(archer.urls.concept, `/api/assets/${imageIds.archer}`);
});

test('review: skipped, missing, unfinished and never-built rows are listed; counts add up', async () => {
  const list = (await api('GET', '/api/forge/review')).json;
  const moved = row(list, ids.moved);
  assert.equal(moved.status, 'incomplete');
  assert.equal(moved.canApprove, false);
  assert.match(moved.reason, /did not reach sprites \(last step: "Paint from concept"\)/);
  const skipped = list.items.find((r) => r.status === 'skipped');
  assert.equal(skipped.reviewable, false);
  assert.equal(skipped.assetId, null);
  assert.match(skipped.reason, /Skipped when the batch was built: role "skip"/);
  const lost = list.items.find((r) => r.status === 'missing');
  assert.equal(lost.reviewable, false);
  assert.match(lost.reason, /Asset passport not found/);
  const fresh = row(list, ids.empty);
  assert.equal(fresh.status, 'new');
  assert.equal(fresh.urls.glb, `/api/forge/assets/${ids.empty}/files/source/source.glb`, 'no model yet: the source GLB');
  assert.equal(fresh.glbStep, 'source');
  assert.deepEqual(list.counts, {
    total: 7, reviewable: 5, built: 1, failed_qa: 1, failed_forge: 1, skipped: 1, other: 3, approved: 0, rework: 0, rejected: 0, notReviewed: 5,
  });
});

test('review: result paths of a moved library are rebuilt from the step folder; files that are gone give null', async () => {
  const list = (await api('GET', '/api/forge/review')).json;
  const moved = row(list, ids.moved);
  assert.equal(moved.urls.compare, `/api/forge/assets/${ids.moved}/files/v001/02_paint_from_concept/compare.png`);
  assert.equal(moved.urls.glb, `/api/forge/assets/${ids.moved}/files/v001/02_paint_from_concept/compare.png`.replace('02_paint_from_concept/compare.png', '01_cleanup/clean.glb'));
  assert.equal(moved.urls.concept, null, 'no concept image for this asset');
  const gone = await makeAsset({ name: 'Files gone', steps: [{ step: 'cleanup', files: { glb: 'clean.glb', preview: 'preview.png' }, exists: false }] });
  const again = (await review.list({})).items;
  assert.equal(again.find((r) => r.assetId === gone), undefined, 'not in the state file');
  const solo = new ForgeReview(engine, { stateFile: path.join(dir, 'none', 'x.state.json') });
  const fallback = await solo.list({});
  const goneRow = fallback.items.find((r) => r.assetId === gone);
  assert.equal(goneRow.urls.glb, `/api/forge/assets/${gone}/files/source/source.glb`);
  assert.equal(goneRow.urls.motion, null);
});

test('review: without a state file the library is listed; a named batch that does not exist is a 404', async () => {
  const solo = new ForgeReview(engine, { stateFile: path.join(dir, 'none', 'x.state.json') });
  const list = await solo.list({});
  assert.equal(list.batch.source, 'library');
  assert.equal(list.batch.stateFile, null);
  assert.ok(list.items.length >= 6 && list.items.every((r) => r.assetId));
  assert.ok(list.items.some((r) => r.assetId === ids.knight));
  const missing = await api('GET', '/api/forge/review?batch=nope');
  assert.equal(missing.status, 404);
  assert.equal(missing.json.code, 'not_found');
  assert.match(missing.json.error, /Batch "nope" not found/);
  const bad = await api('GET', '/api/forge/review?batch=' + encodeURIComponent('..\\..\\secret'));
  assert.equal(bad.status, 400);
  assert.equal(bad.json.code, 'validation');
});

test('review: another batch is read from <incoming>/<name>/<name>.state.json; the env variable moves the default file', async () => {
  const other = path.join(dir, 'incoming', 'pilot2');
  await fs.mkdir(other, { recursive: true });
  await fs.writeFile(path.join(other, 'pilot2.state.json'), JSON.stringify({ items: [{ file: 'a.jpg', title: 'Pilot knight', role: 'melee', faction: 'order', assetId: ids.knight }] }));
  const res = await api('GET', '/api/forge/review?batch=pilot2');
  assert.equal(res.status, 200);
  assert.equal(res.json.batch.name, 'pilot2');
  assert.equal(res.json.batch.decisionsFile, path.join(other, 'review_decisions.json'));
  assert.equal(res.json.items.length, 1);
  assert.equal((await api('GET', '/api/forge/review?batch=build')).json.items.length, 7, 'the default batch by name');
  const viaEnv = new ForgeReview(engine, { env: { ASTRA_FORGE_REVIEW_STATE: path.join(other, 'pilot2.state.json') } });
  assert.equal((await viaEnv.list({})).items.length, 1);
  assert.equal(viaEnv.resolveBatch().decisionsFile, path.join(other, 'review_decisions.json'));
  const envDecisions = new ForgeReview(engine, { stateFile, env: { ASTRA_FORGE_REVIEW_DECISIONS: path.join(dir, 'custom.json') } });
  assert.equal(envDecisions.resolveBatch().decisionsFile, path.join(dir, 'custom.json'));
});

test('review: a state file that is being rewritten (broken JSON) answers 503 in Russian', async () => {
  const broken = path.join(dir, 'incoming', 'half', 'half.state.json');
  await fs.mkdir(path.dirname(broken), { recursive: true });
  await fs.writeFile(broken, '{"items": {"a.jpg": {"title"');
  const res = await api('GET', '/api/forge/review?batch=half');
  assert.equal(res.status, 503);
  assert.equal(res.json.code, 'bad_state');
});

// ------------------------------------------------------------------------------------ decisions

test('review: a decision is stored in the manifest through PATCH qa and shows up in the next list', async () => {
  const denied = await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { reviewed: true, decision: 'approved' } }, {});
  assert.equal(denied.status, 403, 'mutating calls need the x-astra-client header');
  const ok = await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { reviewed: true, decision: 'approved', comment: '', game_unit: 'knight', at: 'ignored' } });
  assert.equal(ok.status, 200, ok.text);
  assert.equal(ok.json.qa.decision, 'approved');
  assert.equal(ok.json.qa.reviewed, true);
  assert.equal(ok.json.qa.game_unit, 'knight');
  assert.notEqual(ok.json.qa.at, 'ignored', 'the server sets the time');
  assert.ok(Date.parse(ok.json.qa.at) > Date.parse('2026-01-01'));
  assert.ok(Array.isArray(ok.json.qa.checks), 'earlier QA data is kept');

  const bad = await api('PATCH', `/api/forge/assets/${ids.acolyte}`, { qa: { decision: 'maybe' } });
  assert.equal(bad.status, 400);
  assert.equal(bad.json.code, 'validation');
  assert.equal((await api('PATCH', `/api/forge/assets/${ids.acolyte}`, { qa: { decision: 'rework', comment: 'x'.repeat(2001) } })).status, 400);

  const rework = await api('PATCH', `/api/forge/assets/${ids.acolyte}`, { qa: { reviewed: true, decision: 'rework', comment: 'clothing/cloak tears; weapon is not in the hand', game_unit: 'acolyte' } });
  assert.equal(rework.status, 200);
  const list = (await api('GET', '/api/forge/review')).json;
  assert.deepEqual(row(list, ids.knight).decision, { decision: 'approved', comment: '', game_unit: 'knight', at: ok.json.qa.at });
  assert.equal(row(list, ids.acolyte).decision.comment, 'clothing/cloak tears; weapon is not in the hand');
  assert.deepEqual([list.counts.approved, list.counts.rework, list.counts.rejected, list.counts.notReviewed], [1, 1, 0, 3]);
});

test('review: the unit and the comment of a decision can be edited alone; decision null clears everything', async () => {
  const unit = await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { game_unit: 'paladin' } });
  assert.equal(unit.json.qa.game_unit, 'paladin');
  assert.equal(unit.json.qa.decision, 'approved', 'the decision itself stays');
  const none = await api('PATCH', `/api/forge/assets/${ids.empty}`, { qa: { comment: 'no decision' } });
  assert.equal(none.status, 200);
  assert.equal(none.json.qa.comment, undefined, 'a comment without a decision is not stored');
  const cleared = await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { reviewed: false, decision: null } });
  assert.equal(cleared.json.qa.decision, undefined);
  assert.equal(cleared.json.qa.game_unit, undefined);
  assert.equal(cleared.json.qa.reviewed, false);
  const list = (await api('GET', '/api/forge/review')).json;
  assert.equal(row(list, ids.knight).decision, null);
  // `reviewed` alone keeps the old behaviour: it does not invent a decision
  const plain = await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { reviewed: true } });
  assert.equal(plain.json.qa.reviewed, true);
  assert.equal(plain.json.qa.decision, undefined);
  assert.equal(row((await api('GET', '/api/forge/review')).json, ids.knight).decision, null, 'reviewed without a decision is not a verdict');
  await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { reviewed: false } });
});

test('review: export writes review_decisions.json with the decided characters only', async () => {
  await api('PATCH', `/api/forge/assets/${ids.knight}`, { qa: { reviewed: true, decision: 'approved', comment: '', game_unit: 'paladin' } });
  await api('PATCH', `/api/forge/assets/${ids.archer}`, { qa: { reviewed: true, decision: 'rejected', comment: 'no skeleton', game_unit: '' } });
  const denied = await api('POST', '/api/forge/review/export', {}, {});
  assert.equal(denied.status, 403);
  assert.equal(existsSync(decisionsFile), false, 'nothing is written without the studio header');

  const res = await api('POST', '/api/forge/review/export', {});
  assert.equal(res.status, 200, res.text);
  assert.equal(res.json.ok, true);
  assert.equal(res.json.path, decisionsFile);
  assert.equal(res.json.count, 3);
  assert.equal(res.json.counts.notReviewed, 2);
  const written = JSON.parse(readFileSync(decisionsFile, 'utf8'));
  assert.ok(Array.isArray(written));
  assert.equal(written.length, 3);
  const knight = written.find((d) => d.assetId === ids.knight);
  assert.deepEqual(
    { assetId: knight.assetId, name: knight.name, decision: knight.decision, comment: knight.comment, game_unit: knight.game_unit },
    { assetId: ids.knight, name: 'Knight holding sword and shield', decision: 'approved', comment: '', game_unit: 'paladin' },
  );
  assert.equal(knight.role, 'melee_shield');
  assert.equal(knight.status, 'built');
  const archer = written.find((d) => d.assetId === ids.archer);
  assert.equal(archer.decision, 'rejected');
  assert.equal(archer.comment, 'no skeleton');
  assert.equal(archer.failedStep, 'rig_humanoid');
  assert.ok(written.find((d) => d.assetId === ids.acolyte).decision === 'rework');
  assert.deepEqual(await fs.readdir(batchDir).then((l) => l.filter((f) => f.endsWith('.tmp'))), [], 'atomic write leaves no temp file');
});

// ------------------------------------------------------------------------------------ pure server helpers

test('explainFailure: rig, sprite and creature errors become plain Russian', () => {
  const e = (step, text) => explainFailure(step, text, titleOf);
  assert.equal(e('rig_humanoid', 'rig not built: not a humanoid of supported proportions: shoulders at 0.66 H (humanoid range 0.70..0.88); right arm 0.14 H (humanoid range 0.15..0.50)'),
    'Rig not built: the model does not look like a human — shoulders at 0.66 of body height (humanoid range 0.70–0.88); right arm is 0.14 of body height (humanoid range 0.15–0.50).');
  assert.match(e('rig_humanoid', 'rig not built: landmarks fail the proportion checks: left forearm 0.080 H (< 0.09); arm landmarks are asymmetric: mirrored Hand 0.292 H apart (limit 0.10) and no hand holds a detected prop - an undetected prop (bow?) or a turned/crooked pose; correct params.markers'),
    /left forearm is too short \(0\.080 of body height, need at least 0\.09\); arms are asymmetric \(hands are 0\.292 of body height apart, limit 0\.10\), and no weapon was found in the hand/);
  assert.equal(e('rig_humanoid', 'rig not built: landmark confidence 0.33 below 0.35: weak landmarks'), 'Rig not built: head, arms and legs were detected with low confidence (0.33, need 0.35).');
  assert.match(e('rig_humanoid', 'rig not built: no ground support: the lowest 2% of the mesh covers only 0.04 x 0.01 H (157 samples)'), /no ground support/);
  assert.equal(e('render_sprites', 'checks failed: cell_fit, frames_body_inside_cell'), `Step "${titleOf('render_sprites')}" failed its checks: figure does not fit the frame cell; body goes outside the frame.`);
  assert.match(e('anim_creature', 'checks failed: ground_penetration_cm:idle-loop, foot_slide_cm:walk-loop, ground_penetration_cm:walk-loop'),
    /sinks into the ground \(idle-loop, walk-loop\); feet slide on the ground \(walk-loop\)/);
  assert.equal(e('cleanup', 'Step "cleanup" exceeded the time limit (30 min) and was stopped.'), `Step "${titleOf('cleanup')}" exceeded the time limit (30 min) and was stopped.`);
  assert.match(e('retarget', 'Traceback (most recent call last):\n  File "x.py", line 1\nKeyError: bone'), /^Step "Clip retargeting" failed: Traceback/);
  assert.match(e('retarget', ''), /failed with no explanation/);
});

test('tearSummary: fractions become percent, the worst clip is picked, no data gives null', () => {
  assert.equal(tearSummary(null), null);
  assert.equal(tearSummary({}), null);
  const t = tearSummary({ a: deform(0.02, null, false), b: deform(0.005, 0.03, true) }, 'warning');
  assert.equal(t.ok, false);
  assert.equal(t.failedClips, 1);
  assert.deepEqual(t.worst, { clip: 'a', tornPct: 2, robePct: null, limitPct: 1 });
  assert.deepEqual(t.worstRobe, { clip: 'b', robePct: 3 });
  assert.equal(t.gate, 'warning');
});

// ------------------------------------------------------------------------------------ view helpers

const sample = (over) => ({ assetId: randomUUID(), name: 'Knight', role: 'melee_shield', faction: 'order', status: 'built', reviewable: true, decision: null, ...over });

test('view: unit suggestion keeps the leading words, drops the weapon part and keeps «#N»', () => {
  assert.equal(suggestUnit('Acolyte holding staff and book'), 'acolyte');
  assert.equal(suggestUnit('Sorcerer archer holding bone bow #2'), 'sorcerer_archer_2');
  assert.equal(suggestUnit('Knight holding sword and shield #5'), 'knight_5');
  assert.equal(suggestUnit('Pale aristocrat standing in studio'), 'pale_aristocrat');
  assert.equal(suggestUnit('Sorcerer in robes holding sta'), 'sorcerer');
  assert.equal(suggestUnit('Order captain holding banner sta'), 'order_captain');
  assert.equal(suggestUnit('Zombie standing in studio'), 'zombie');
  assert.equal(suggestUnit('Very long leading words before holding a sword'), 'very_long_leading');
  assert.equal(suggestUnit(''), '');
  assert.equal(suggestUnit('holding a sword'), 'a_sword');
});

test('view: rework reasons toggle in and out of the comment', () => {
  assert.equal(REWORK_PRESETS.length, 5);
  assert.equal(toggleReason('', 'clothing/cloak tears'), 'clothing/cloak tears');
  assert.equal(toggleReason('clothing/cloak tears', 'weapon is not in the hand'), 'clothing/cloak tears; weapon is not in the hand');
  assert.equal(toggleReason('clothing/cloak tears; weapon is not in the hand', 'clothing/cloak tears'), 'weapon is not in the hand');
  assert.equal(toggleReason('short cloak; no skeleton', 'no skeleton'), 'short cloak');
  assert.ok(hasReason('cloak; no skeleton', 'no skeleton'));
  assert.ok(!hasReason('no skeleton?', 'no skeleton'));
  assert.deepEqual(splitReasons(' a ;b;; c '), ['a', 'b', 'c']);
});

test('view: arrows move over the visible cards, up/down by a grid row, edges stay', () => {
  const keys = ['a', 'b', 'c', 'd', 'e'];
  assert.equal(moveSelection(keys, null, 'ArrowRight', 2), 'a');
  assert.equal(moveSelection(keys, 'a', 'ArrowRight', 2), 'b');
  assert.equal(moveSelection(keys, 'a', 'ArrowLeft', 2), 'a');
  assert.equal(moveSelection(keys, 'a', 'ArrowDown', 2), 'c');
  assert.equal(moveSelection(keys, 'c', 'ArrowUp', 2), 'a');
  assert.equal(moveSelection(keys, 'b', 'ArrowUp', 2), 'b');
  assert.equal(moveSelection(keys, 'd', 'ArrowDown', 2), 'e', 'down from a short last row goes to the last card');
  assert.equal(moveSelection(keys, 'e', 'ArrowDown', 2), 'e');
  assert.equal(moveSelection(keys, 'gone', 'ArrowDown', 2), 'a');
  assert.equal(moveSelection([], 'a', 'ArrowDown', 2), null);
  assert.equal(moveSelection(keys, 'b', 'ArrowDown', 1), 'c', 'one column = list');
  assert.equal(neighbourId(keys, 'b'), 'c');
  assert.equal(neighbourId(keys, 'e'), 'd');
  assert.equal(neighbourId(['a'], 'a'), null);
  assert.equal(neighbourId(keys, 'zzz'), 'a');
  assert.equal(reviewKeyAction('KeyA'), 'approve');
  assert.equal(reviewKeyAction('KeyR'), 'rework');
  assert.equal(reviewKeyAction('Enter'), 'open');
  assert.equal(reviewKeyAction('ArrowUp'), 'move');
  assert.equal(reviewKeyAction('KeyW'), null, 'the studio keeps W / E');
});

test('view: filters combine build state, decision, faction, role and a text query; chips count the rest', () => {
  const items = [
    sample({ name: 'Knight', status: 'built', decision: { decision: 'approved', comment: '', game_unit: 'paladin' } }),
    sample({ name: 'Acolyte', role: 'caster', status: 'failed_qa', decision: { decision: 'rework', comment: 'cloak', game_unit: '' } }),
    sample({ name: 'Zombie', role: 'zombie', faction: 'undead', status: 'failed_forge' }),
    sample({ name: 'Skeleton archer', role: 'ranged', faction: 'undead', status: 'failed_qa' }),
    sample({ name: 'Ghost', role: 'skip', faction: 'undead', status: 'skipped', reviewable: false, assetId: null, file: 'g.jpg' }),
  ];
  assert.equal(filterItems(items, {}).length, 5);
  assert.deepEqual(filterItems(items, { build: 'built' }).map((i) => i.name), ['Knight', 'Acolyte', 'Skeleton archer']);
  assert.deepEqual(filterItems(items, { build: 'torn' }).map((i) => i.name), ['Acolyte', 'Skeleton archer']);
  assert.deepEqual(filterItems(items, { build: 'failed' }).map((i) => i.name), ['Zombie']);
  assert.deepEqual(filterItems(items, { decision: 'pending' }).map((i) => i.name), ['Zombie', 'Skeleton archer'], 'unreviewable rows are never «not reviewed»');
  assert.deepEqual(filterItems(items, { decision: 'rework' }).map((i) => i.name), ['Acolyte']);
  assert.deepEqual(filterItems(items, { faction: 'undead', build: 'torn' }).map((i) => i.name), ['Skeleton archer']);
  assert.deepEqual(filterItems(items, { role: 'caster' }).map((i) => i.name), ['Acolyte']);
  assert.deepEqual(filterItems(items, { query: 'PALADIN' }).map((i) => i.name), ['Knight'], 'the unit is searchable');
  assert.deepEqual(filterItems(items, { query: 'cloak' }).map((i) => i.name), ['Acolyte'], 'so is the comment');
  assert.deepEqual(filterItems(items, { query: 'undead' }).map((i) => i.name), ['Zombie', 'Skeleton archer', 'Ghost']);
  assert.deepEqual(reviewCounts(items), { total: 4, approved: 1, rework: 1, rejected: 0, pending: 2 });
  const chips = chipCounts(items, { decision: 'pending' }, 'build');
  assert.deepEqual(chips, { all: 2, built: 1, torn: 1, failed: 1 });
  assert.deepEqual(distinct(items, 'faction'), ['order', 'undead']);
  assert.equal(matchesBuild(items[4], 'built'), false);
  assert.equal(matchesBuild(items[4], 'all'), true);
  assert.equal(rowKey(items[4]), 'file:g.jpg');
  assert.equal(rowKey(items[0]), items[0].assetId);
});

test('view: decision patch, local copy of a row and percent format', () => {
  assert.deepEqual(decisionPatch('approved', { comment: ' ok ', unit: ' paladin ' }), { qa: { reviewed: true, decision: 'approved', comment: 'ok', game_unit: 'paladin' } });
  assert.deepEqual(decisionPatch('rework', { comment: 'clothing/cloak tears' }), { qa: { reviewed: true, decision: 'rework', comment: 'clothing/cloak tears', game_unit: '' } });
  assert.deepEqual(decisionPatch(null), { qa: { reviewed: false, decision: null } });
  const item = sample({});
  assert.deepEqual(withDecision(item, { decision: 'rejected', comment: 'reject', game_unit: '', at: 'now' }).decision, { decision: 'rejected', comment: 'reject', game_unit: '', at: 'now' });
  assert.equal(withDecision({ ...item, decision: { decision: 'approved' } }, { reviewed: false }).decision, null);
  assert.equal(percent(30.7), '30,7%');
  assert.equal(percent(1), '1%');
  assert.equal(percent(null), '—');
});
