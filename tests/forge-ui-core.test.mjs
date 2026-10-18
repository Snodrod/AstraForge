// Pure helpers of the Forge wizard (src/forge/forge-core.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  wizardStepsFor, prepareSteps, sourcesFor, kindFromAsset, wizardProgress, defaultBatchSteps, runPlan, planSteps, replayNote,
  paramDefs, resolveParamName, defaultsFor, coerceParam, buildParams, generatorParams, shownParams, stepTitle, enumLabel,
  normalizeMarkers, markerProjection, metresToPx, pxToMetres, clampPx, mirrorName, moveMarker, roundMarkers,
  canonicalMarkerName, markerLabel, approxMarkerBox, markersSymmetric, detectColorBlobs, matchPoints, fitProjection, calibrateProjection,
  clipList, clipCategories, filterClips, defaultClipOptions, clipsForPassport, retargetClips, creatureClipAvailable, isLocomotionClip,
  creatureClipsForPassport, creatureClipOptions, creatureNamesFromPassport,
  filterPresets, seedBatchItems, seedVariants, sourceBatchItems, batchCounts, batchItemsFrom, onlyGlb,
  relFromOutput, assetRelPath, findOutput, imageOutputs, imageLabel, latestPipelineEntry, collectSockets, beforeAfter,
  jobProgress, currentJobStep, jobError, humanizeError, tailLines, isActiveStatus,
  normalizeChecks, checksSummary, exportProfilesFor, socketsFor, suggestSocket, fxForPassport,
  normalizePoses, poseAt, projectJoint, poseBounds, sliderToBudget, budgetToSlider, niceBudget,
  rigSeedValues, markersForHeight, generatorSeedValues, exportPlan, draftNumber, commitNumber, mergeJobRecord, pollPlan,
  fileBatchChunks, frontTileWidth, niceStep, paramLabel, paramHint, categoryLabel, FALLBACK_PARAMS,
} from '../src/forge/forge-core.js';
import { listSteps } from '../server/forge-steps.mjs';
import { hasT, setLang } from '../src/i18n/index.js';

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

/** Run fn with the Russian UI, restoring English (the Node default) afterwards. */
function inRussian(fn) {
  setLang('ru');
  try {
    return fn();
  } finally {
    setLang('en');
  }
}

// numbers and axes are shown as they are
const RAW_ENUM = (v) => typeof v === 'number' || /^\d+$/.test(String(v)) || /^[+-]?[XYZ]$/.test(String(v)) || /^[+-][xyz]$/.test(String(v));

test('forge: every registry enum value has a dictionary label in both languages', () => {
  const values = new Set();
  for (const s of listSteps()) {
    for (const p of Object.values(s.params)) {
      for (const v of [...(p.enum || []), ...(p.items?.enum || []), ...(p.alt || []).flatMap((a) => a.enum || [])]) values.add(v);
    }
  }
  for (const v of values) {
    if (RAW_ENUM(v)) continue;
    assert.ok(hasT(`enum.${v}`), `enum.${v}`);
    assert.doesNotMatch(enumLabel(v), /[\u0400-\u04ff]/, `en: ${v}`);
    inRussian(() => assert.ok(enumLabel(v), `ru: ${v}`));
  }
  assert.equal(enumLabel(512), '512');
  assert.equal(enumLabel('+X'), '+X');
  assert.deepEqual(['none', 'delta', 'swept', 'forward', 'straight', 'x', 'stub', 'crescent'].map(enumLabel),
    ['None', 'Delta', 'Swept', 'Forward-swept', 'Straight', 'X-wing', 'Stub', 'Crescent']);
  inRussian(() => assert.deepEqual(['none', 'delta', 'swept', 'forward', 'straight', 'x', 'stub', 'crescent'].map(enumLabel),
    ['Нет', 'Дельта', 'Стреловидные', 'Обратная стреловидность', 'Прямые', 'X-образные', 'Короткие', 'Серповидные']));
});

test('forge: step titles and wizard param labels come from the dictionary in both languages', () => {
  for (const s of listSteps()) {
    assert.ok(hasT(`step.${s.step}`), `step.${s.step}`);
    assert.doesNotMatch(stepTitle(s.step, [s]), /[\u0400-\u04ff]/, s.step);
  }
  const wizard = ['cleanup', 'rig_humanoid', 'rig_creature', 'anim_creature', 'retarget', 'ship_gen', 'turret_gen', 'item_kit', 'attach_prop'];
  for (const s of listSteps().filter((x) => wizard.includes(x.step))) {
    for (const [name, def] of Object.entries(s.params)) {
      if (def.title) assert.doesNotMatch(paramLabel(s.step, name, def), /[\u0400-\u04ff]/, `${s.step}.${name}`);
      if (def.help) assert.doesNotMatch(paramHint(s.step, name, def), /[\u0400-\u04ff]/, `${s.step}.${name}.help`);
    }
  }
  // unknown params fall back to the server text, then to the name
  assert.equal(paramLabel('cleanup', 'brand_new', { title: 'Server title' }), 'Server title');
  assert.equal(paramLabel('cleanup', 'brand_new'), 'brand_new');
  assert.equal(paramHint('cleanup', 'brand_new'), '');
  assert.equal(paramLabel('cleanup', 'target_tris', { title: 'x' }), 'Triangles');
  assert.equal(FALLBACK_PARAMS.cleanup.target_tris.title, 'Triangles');
  assert.equal(paramDefs([], 'cleanup').target_tris.help, 'Target budget; empty: no decimation.');
  inRussian(() => {
    assert.equal(paramLabel('cleanup', 'target_tris'), 'Треугольников');
    assert.equal(FALLBACK_PARAMS.cleanup.target_tris.title, 'Треугольников');
    assert.equal(stepTitle('cleanup', []), 'Чистка и упрощение');
  });
});

test('forge: wizard steps per kind skip irrelevant steps', () => {
  assert.deepEqual(wizardStepsFor('humanoid').map((s) => s.id), ['what', 'prepare', 'skeleton', 'motion', 'items', 'fx', 'export']);
  assert.deepEqual(wizardStepsFor('creature').map((s) => s.id), ['what', 'prepare', 'skeleton', 'motion', 'fx', 'export']);
  for (const k of ['ship', 'turret', 'item']) assert.ok(!wizardStepsFor(k).some((s) => s.id === 'skeleton' || s.id === 'motion'));
  assert.equal(wizardStepsFor('ship', 'params')[1].title, 'Generator');
  assert.equal(wizardStepsFor('ship', 'scene')[1].title, 'Preparation');
  inRussian(() => assert.equal(wizardStepsFor('ship', 'params')[1].title, 'Генератор'));
  assert.deepEqual(prepareSteps('ship', 'params'), ['ship_gen']);
  assert.deepEqual(prepareSteps('ship', 'job'), ['cleanup', 'ship_from_mesh']);
  assert.deepEqual(prepareSteps('humanoid', 'upload'), ['cleanup']);
  assert.deepEqual(prepareSteps('turret', 'params'), ['turret_gen']);
  assert.deepEqual(defaultBatchSteps('humanoid', 'asset'), ['cleanup', 'rig_humanoid']);
  assert.deepEqual(sourcesFor('turret').map((s) => s.id), ['params']);
  assert.ok(!sourcesFor('humanoid').some((s) => s.id === 'params'));
});

test('forge: passport helpers (kind, progress, pipeline lookup)', () => {
  assert.equal(kindFromAsset({ kind: 'prop', pipeline: [{ step: 'turret_gen' }] }), 'turret');
  assert.equal(kindFromAsset({ kind: 'humanoid' }), 'humanoid');
  assert.equal(kindFromAsset({ kind: 'weird' }), null);
  const asset = {
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001', result: { outputs: {} } },
      { step: 'rig_humanoid', status: 'failed', version: 'v002' },
      { step: 'rig_humanoid', status: 'done', version: 'v003' },
    ],
    fx: [{ preset: 'aura', socket: 'socket_fx_chest' }],
    exports: [],
  };
  const p = wizardProgress(asset);
  assert.equal(p.what, 'done');
  assert.equal(p.prepare, 'done');
  assert.equal(p.skeleton, 'done');
  assert.equal(p.motion, null);
  assert.equal(p.fx, 'done');
  assert.equal(p.export, null);
  assert.equal(latestPipelineEntry(asset, ['rig_humanoid']).version, 'v003');
  assert.equal(latestPipelineEntry(asset, 'rig_humanoid', { status: 'failed' }).version, 'v002');
  assert.equal(latestPipelineEntry(null, 'cleanup'), null);
});

test('forge: registry params override fallbacks and resolve aliases', () => {
  const registry = [
    { step: 'cleanup', title: 'Server cleanup', params: { max_tris: { type: 'int', default: 20000, min: 500, max: 100000 }, height: { type: 'float', default: 1.7 } } },
    { step: 'new_step', title: 'Server title' },
  ];
  const defs = paramDefs(registry, 'cleanup');
  assert.equal(resolveParamName(defs, 'triangles'), 'max_tris');
  assert.equal(resolveParamName(defs, 'height'), 'height');
  assert.equal(resolveParamName({}, 'triangles'), 'target_tris');
  assert.equal(stepTitle('cleanup', registry), 'Cleanup and decimation', 'the client dictionary wins over the server title');
  assert.equal(stepTitle('new_step', registry), 'Server title', 'no dictionary entry: the server title');
  assert.equal(stepTitle('ship_gen', []), 'Ship generator');
  assert.equal(stepTitle('unknown_step', []), 'unknown_step');
  inRussian(() => assert.equal(stepTitle('ship_gen', []), 'Генератор корабля'));
  const d = defaultsFor(defs);
  assert.equal(d.max_tris, 20000);
  assert.equal(d.height, 1.7);
  assert.equal(d.target_tris, undefined, 'guessed names are dropped once the registry knows the step');
  assert.equal(paramDefs([], 'cleanup').target_tris.fallback, true, 'fallback while the registry is empty');
  assert.deepEqual(Object.keys(paramDefs([{ step: 'separate' }], 'separate')), []);
  assert.equal(coerceParam(defs.max_tris, '250'), 500, 'clamped to min');
  assert.equal(coerceParam(defs.max_tris, '99999.6'), 100000);
  assert.equal(coerceParam({ type: 'enum', enum: ['a', 'b'], default: 'a' }, 'z'), 'a');
  assert.equal(coerceParam({ type: 'boolean' }, 'true'), true);
  assert.deepEqual(coerceParam({ type: 'array' }, 'idle, walk'), ['idle', 'walk']);
  const params = buildParams(defs, { max_tris: 12000, target_tris: 5, stale: 'x' }, { markers: { chin: [0, 1.5] } });
  assert.equal(params.max_tris, 12000);
  assert.equal(params.stale, undefined, 'undeclared form values are dropped');
  assert.equal(params.target_tris, undefined);
  assert.deepEqual(params.markers, { chin: [0, 1.5] }, 'extra params are always sent');
  assert.deepEqual(buildParams({}, { anything: 1 }), { anything: 1 }, 'nothing declared: values pass through');
  // defaults are copied, not shared
  const a = defaultsFor({ clips: { default: ['idle'] } });
  a.clips.push('walk');
  assert.deepEqual(defaultsFor({ clips: { default: ['idle'] } }).clips, ['idle']);
});

test('forge: marker projection block maps metres <-> pixels both ways', () => {
  const doc = { markers: { chin: [0, 1.55], wrist_l: [0.62, 1.05], knee_r: [-0.12, 0.48, 0] }, projection: { image: { width: 512, height: 512 }, center: [0, 0.9], meters_per_px: 0.004 } };
  const proj = markerProjection(doc, null);
  assert.equal(proj.source, 'projection');
  assert.deepEqual(proj.centerPx, [256, 256]);
  assert.deepEqual(metresToPx(proj, [0, 0.9]), [256, 256]);
  const [u, v] = metresToPx(proj, [0.62, 1.05]);
  close(u, 256 + 155);
  close(v, 256 - 37.5);
  const back = pxToMetres(proj, [u, v]);
  close(back[0], 0.62);
  close(back[1], 1.05);
  const m = normalizeMarkers(doc);
  assert.deepEqual(m.knee_r, [-0.12, 0], '[x,y,z] keeps x and z');
  // top-level projection keys and explicit centre pixel
  const p2 = markerProjection({ image: { width: 1024, height: 512 }, center: [0, 0, 1], meters_per_px: 0.01, center_px: [256, 256] }, null);
  assert.deepEqual(p2.center, [0, 1]);
  assert.deepEqual(metresToPx(p2, [1, 1]), [356, 256]);
  assert.deepEqual(clampPx(p2, [-5, 900]), [0, 512]);
});

test('forge: marker projection falls back to bbox + image size (render_views math)', () => {
  const doc = { markers: { chin: [0, 1.6] }, bbox_min: [-0.4, -0.2, 0], bbox_max: [0.4, 0.2, 1.8] };
  // contact sheet of 4 square tiles: front view is the first 384 px
  const proj = markerProjection(doc, { width: 1536, height: 384 });
  assert.equal(proj.source, 'bbox');
  const diag = Math.hypot(0.8, 0.4, 1.8);
  close(proj.mpp, (diag * 1.08) / 384);
  assert.deepEqual(proj.centerPx, [192, 192]);
  assert.deepEqual(proj.center, [0, 0.9]);
  const [u, v] = metresToPx(proj, [0, 0.9]);
  assert.deepEqual([u, v], [192, 192]);
  assert.equal(markerProjection({ markers: {} }, { width: 10, height: 10 }), null);
  assert.equal(markerProjection(doc, null), null, 'no image size yet');
  // bbox object form
  assert.equal(markerProjection({ bbox: { min: [-1, -1, 0], max: [1, 1, 2] } }, { width: 256, height: 256 }).source, 'bbox');
});

test('forge: marker names, symmetry and rounding', () => {
  assert.equal(canonicalMarkerName('LeftWrist'), 'wrist_l');
  assert.equal(canonicalMarkerName('elbow.R'), 'elbow_r');
  assert.equal(canonicalMarkerName('knee_left'), 'knee_l');
  assert.equal(markerLabel('wrist_l'), 'Left wrist');
  inRussian(() => assert.equal(markerLabel('wrist_l'), 'Левое запястье'));
  assert.equal(markerLabel('mystery'), 'mystery');
  assert.equal(mirrorName('wrist_l'), 'wrist_r');
  assert.equal(mirrorName('LeftKnee'), 'RightKnee');
  assert.equal(mirrorName('chin'), null);
  const m = { wrist_l: [0.6, 1], wrist_r: [-0.6, 1], chin: [0, 1.5] };
  const sym = moveMarker(m, 'wrist_l', [0.7, 1.1], { symmetry: true });
  assert.deepEqual(sym.wrist_r, [-0.7, 1.1]);
  assert.deepEqual(m.wrist_r, [-0.6, 1], 'input is not mutated');
  const free = moveMarker(m, 'wrist_l', [0.7, 1.1]);
  assert.deepEqual(free.wrist_r, [-0.6, 1]);
  assert.deepEqual(moveMarker(m, 'chin', [0.01, 1.4], { symmetry: true }).chin, [0.01, 1.4]);
  assert.deepEqual(roundMarkers({ a: [0.123456, 1.987654] }), { a: [0.1235, 1.9877] });
  assert.equal(markersSymmetric(m), true);
  assert.equal(markersSymmetric({ l_wrist: [0.51, 1.33], r_wrist: [-0.47, 0.97] }), false, 'staff in one hand');
  assert.equal(markersSymmetric({ chin: [0, 1.6] }), false, 'no pairs');
});

test('forge: clip library filtering, categories and passport mapping', () => {
  const index = { clips: [
    { id: 'mixamo-sword-idle', name: 'Sword idle', category: 'idle', tags: ['sword'] },
    { id: 'mixamo-run', name: 'Running', category: 'run' },
    { id: 'mixamo-slash', name: 'Sword slash', category: 'attack', tags: ['sword', 'blade'] },
    { id: 'odd', name: 'Odd', category: 'dance' },
  ] };
  const clips = clipList(index);
  assert.equal(clipList([1, 2]).length, 2);
  assert.equal(clipList(null).length, 0);
  const chips = clipCategories(clips);
  assert.deepEqual(chips.map((c) => c.id), ['all', 'idle', 'run', 'attack', 'other']);
  assert.equal(chips[0].count, 4);
  assert.deepEqual(filterClips(clips, { query: 'sword' }).map((c) => c.id), ['mixamo-sword-idle', 'mixamo-slash']);
  assert.deepEqual(filterClips(clips, { category: 'attack', query: 'BLADE' }).map((c) => c.id), ['mixamo-slash']);
  assert.deepEqual(filterClips(clips, { query: 'odd other' }).map((c) => c.id), ['odd'], 'the category label is searchable');
  assert.equal(categoryLabel('all'), 'All');
  inRussian(() => {
    assert.deepEqual(filterClips(clips, { query: 'бег' }).map((c) => c.id), ['mixamo-run'], 'Russian category label is searchable');
    assert.equal(clipCategories(clips)[1].title, 'Покой', 'category labels follow the language');
  });
  assert.deepEqual(filterClips(clips, { category: 'other' }).map((c) => c.id), ['odd']);
  assert.deepEqual(defaultClipOptions(clips[0]), { loop: true, in_place: false }, 'a standing idle keeps its hips motion');
  assert.deepEqual(defaultClipOptions(clips[1]), { loop: true, in_place: true }, 'locomotion plays in place');
  assert.equal(defaultClipOptions(clips[2]).loop, false);
  assert.deepEqual(clipsForPassport({ 'mixamo-run': { loop: true, in_place: false } }), [{ id: 'mixamo-run', source: 'library:mixamo-run', loop: true, in_place: false }]);
  assert.equal(creatureClipAvailable('fly-loop', 'quadruped'), false);
  assert.equal(creatureClipAvailable('glide-loop', 'bird'), true);
  assert.equal(creatureClipAvailable('walk-loop', 'serpent'), true);
  assert.deepEqual(creatureClipsForPassport(['walk-loop', 'death']).map((c) => [c.id, c.source, c.loop]), [['walk', 'procedural:walk', true], ['death', 'procedural:death', false]]);
  const options = creatureClipOptions({ clips: { items: { enum: ['idle-loop', 'fly-loop', 'attack'] } } });
  assert.deepEqual(options.map((o) => [o.id, o.base, o.title, o.loop]), [['idle-loop', 'idle', 'Idle', true], ['fly-loop', 'fly', 'Fly', true], ['attack', 'attack', 'Attack', false]]);
  inRussian(() => assert.equal(creatureClipOptions({}).find((o) => o.base === 'fly').title, 'Полёт'));
  assert.equal(creatureClipOptions({}).length, 8, 'planned list without a registry');
  assert.deepEqual(creatureNamesFromPassport([{ source: 'procedural:fly' }, { source: 'library:x' }], options), ['fly-loop']);
  assert.deepEqual(retargetClips({ 'mixamo-run': { loop: true } }, clips), [{ source: 'library:mixamo-run', name: 'Running', loop: true, in_place: true }]);
  // in_place only for locomotion (contract §7, retarget root_motion): standing clips leave it unset
  const sel = {};
  for (const c of clips) sel[c.id] = defaultClipOptions(c);
  const sent = Object.fromEntries(retargetClips(sel, clips).map((c) => [c.source.slice(8), c]));
  assert.equal(sent['mixamo-run'].in_place, true);
  for (const id of ['mixamo-sword-idle', 'mixamo-slash', 'odd']) assert.equal('in_place' in sent[id], false, id);
  // old selections without in_place follow the same rule
  assert.equal('in_place' in retargetClips({ 'mixamo-slash': { loop: false } }, clips)[0], false);
  // explicit choices win: an unticked walk travels, a ticked attack is pinned, 'strict' passes through
  assert.equal(retargetClips({ 'mixamo-run': { loop: true, in_place: false } }, clips)[0].in_place, false);
  assert.equal(retargetClips({ 'mixamo-slash': { loop: false, in_place: true } }, clips)[0].in_place, true);
  assert.equal(retargetClips({ 'mixamo-run': { in_place: 'strict' } }, clips)[0].in_place, 'strict');
  // locomotion as the retarget step sees it: walk/run, the tag or an explicit flag
  assert.equal(isLocomotionClip({ category: 'walk' }), true);
  assert.equal(isLocomotionClip({ category: 'other', tags: ['Locomotion'] }), true);
  assert.equal(isLocomotionClip({ category: 'run', locomotion: false }), false);
  assert.equal(isLocomotionClip({ category: 'attack' }), false);
  assert.equal(isLocomotionClip(undefined), false);
  const unknown = retargetClips({ 'not-in-index': { loop: false } }, clips)[0];
  assert.deepEqual(unknown, { source: 'library:not-in-index', name: 'not-in-index', loop: false });
});

test('forge: presets and batch items', () => {
  const presets = [{ id: 'a', kind: 'ship' }, { id: 'b', kind: 'humanoid' }, { id: 'c' }, { id: 'd', kind: 'prop' }];
  assert.deepEqual(filterPresets(presets, 'ship').map((p) => p.id), ['a', 'c']);
  assert.deepEqual(filterPresets({ presets }, 'turret').map((p) => p.id), ['c', 'd']);
  const items = seedBatchItems({ kind: 'ship', from: 1, to: 20, namePrefix: 'Ship', params: { class: 'fighter' } });
  assert.equal(items.length, 20);
  assert.equal(items[0].name, 'Ship #1');
  assert.equal(seedBatchItems({ kind: 'ship', from: 2, to: 2 })[0].name, 'New ship #2', 'kind default name');
  assert.equal(items[19].source.params.seed, 20);
  assert.deepEqual(items[4].steps, [{ step: 'ship_gen', params: { class: 'fighter', seed: 5 } }]);
  assert.equal(items[0].kind, 'ship');
  const turret = seedBatchItems({ kind: 'turret', from: 3, to: 2, preset: 'p1' });
  assert.deepEqual(turret.map((i) => i.source.params.seed), [2, 3]);
  assert.equal(turret[0].kind, 'prop');
  assert.equal(turret[0].preset, 'p1');
  assert.equal(turret[0].steps, undefined);
  assert.equal(seedBatchItems({ kind: 'ship', from: 0, to: 1000, limit: 50 }).length, 50);
  assert.deepEqual(seedVariants(10, 6), [11, 12, 13, 14, 15, 16]);
  const src = sourceBatchItems({ kind: 'humanoid', sources: [{ name: 'Orc', source: { type: 'asset', assetId: 'x' } }], steps: ['cleanup', 'rig_humanoid'] });
  assert.deepEqual(src[0], { name: 'Orc', kind: 'humanoid', source: { type: 'asset', assetId: 'x' }, steps: [{ step: 'cleanup', params: {} }, { step: 'rig_humanoid', params: {} }] });
  const counts = batchCounts([{ status: 'completed' }, { status: 'failed' }, { jobId: 'j' }, {}], { j: { status: 'running' } });
  assert.deepEqual(counts, { total: 4, done: 1, failed: 1, running: 1, queued: 1 });
  const merged = batchItemsFrom({ items: [{ assetId: 'a1', status: 'running' }, { assetId: 'a3', status: 'queued' }] }, [{ assetId: 'a1', jobId: 'j1', name: 'One' }, { assetId: 'a2', jobId: 'j2', name: 'Two' }]);
  assert.equal(merged.length, 3);
  assert.deepEqual(merged[0], { assetId: 'a1', jobId: 'j1', name: 'One', status: 'running' });
  const files = onlyGlb([{ name: 'b.GLB' }, { name: 'a.fbx' }, { name: 'a.glb' }]);
  assert.deepEqual(files.map((f) => f.name), ['a.glb', 'b.GLB']);
});

test('forge: output paths become asset file references', () => {
  const id = '6f1c2b8e-1111-4222-8333-444455556666';
  const win = `C:\\Users\\x\\AstraForge\\data\\library\\assets\\${id}\\v003\\02_rig_humanoid\\markers.png`;
  assert.deepEqual(relFromOutput(win, id), { assetId: id, version: 'v003', stepDir: '02_rig_humanoid', file: 'markers.png' });
  const posix = `/srv/data/library/assets/${id}/v001/01_cleanup/_views/front.png`;
  assert.deepEqual(relFromOutput(posix), { assetId: id, version: 'v001', stepDir: '01_cleanup', file: '_views/front.png' });
  assert.equal(relFromOutput(posix, 'other-id'), null);
  assert.equal(relFromOutput('C:/tmp/out.png'), null);
  assert.equal(relFromOutput(`/a/assets/${id}/v001/x/../secret.txt`), null);
  assert.equal(relFromOutput(null), null);
  const outputs = { glb: '/a/m.glb', preview: '/a/preview.png', extra: { markers_json: '/a/markers.json', sheet: ['/a/markers.png', '/a/poses.png'] } };
  assert.equal(findOutput(outputs, { ext: '.glb' }), '/a/m.glb');
  assert.equal(findOutput(outputs, { ext: '.json', match: /marker/ }), '/a/markers.json');
  assert.equal(findOutput(outputs, { ext: '.png', match: /pose/ }), '/a/poses.png');
  assert.equal(findOutput(outputs, { ext: '.fbx' }), null);
  assert.deepEqual(imageOutputs(outputs).map((e) => e.path), ['/a/preview.png', '/a/markers.png', '/a/poses.png']);
  assert.equal(imageLabel({ key: 'preview_before', path: '/a/b.png' }), 'Before');
  assert.equal(imageLabel({ key: 'extra.sheet.1', path: '/a/poses.png' }), 'Deformation test');
  inRussian(() => assert.equal(imageLabel({ key: 'preview_before', path: '/a/b.png' }), 'До'));
  const ba = beforeAfter({ preview_before: '/a/before.png', preview_after: '/a/after.png' });
  assert.deepEqual(ba, { before: '/a/before.png', after: '/a/after.png' });
  assert.deepEqual(beforeAfter({ preview: '/a/p.png' }), { before: null, after: '/a/p.png' });
});

test('forge: sockets and effects', () => {
  const asset = { pipeline: [{ result: { stats: { sockets: ['socket_thruster_main_l', { name: 'socket_turret_top_01' }, 'not_a_socket'] } } }] };
  assert.deepEqual(collectSockets(asset), ['socket_thruster_main_l', 'socket_turret_top_01']);
  assert.deepEqual(socketsFor('ship', asset), ['socket_thruster_main_l', 'socket_turret_top_01']);
  assert.ok(socketsFor('ship', {}).includes('socket_muzzle_01'), 'guesses only without real sockets');
  assert.ok(socketsFor('humanoid', asset).includes('socket_mount_hand_r'), 'contract sockets stay for humans');
  assert.equal(suggestSocket('engine_trail', socketsFor('ship', asset)), 'socket_thruster_main_l');
  assert.equal(suggestSocket('magic_cast', socketsFor('humanoid', {})), 'socket_mount_hand_r');
  assert.equal(suggestSocket('unknown', ['socket_a']), 'socket_a');
  assert.deepEqual(fxForPassport([{ preset: 'aura', socket: 'socket_fx_chest' }, { preset: 'fire', socket: '' }]), [{ preset: 'aura', socket: 'socket_fx_chest', params: {} }]);
  assert.deepEqual(exportProfilesFor('ship').map((p) => p.id), ['godot', 'iso8']);
  assert.deepEqual(exportProfilesFor('humanoid').map((p) => p.id), ['godot', 'iso8']);
  assert.equal(exportProfilesFor('turret')[1].title, 'iso8 sprites');
});

test('forge: job progress, current step and plain-language errors', () => {
  const job = { status: 'running', steps: [{ step: 'cleanup', status: 'completed' }, { step: 'rig_humanoid', status: 'running', progress: 50 }, { step: 'retarget', status: 'queued' }] };
  assert.equal(jobProgress(job), 50);
  assert.equal(currentJobStep(job).step, 'rig_humanoid');
  assert.equal(jobProgress({ status: 'completed', steps: [] }), 100);
  assert.equal(jobError(job), null);
  const failed = { status: 'failed', steps: [{ step: 'cleanup', status: 'failed', result: { error: 'checks failed: unweighted_vertices, bone_count' } }] };
  assert.equal(jobError(failed), 'Quality checks failed: unweighted_vertices, bone_count.');
  assert.equal(humanizeError('TimeoutError: step timed out after 900 s'), 'The step ran out of time and was stopped.');
  assert.equal(humanizeError('RuntimeError: CUDA out of memory'), 'Not enough video memory. Wait for the ComfyUI generation to finish and try again.');
  inRussian(() => assert.equal(humanizeError('RuntimeError: CUDA out of memory'), 'Не хватило видеопамяти. Дождитесь окончания генерации в ComfyUI и повторите.'));
  assert.equal(humanizeError('Ассет не найден'), 'Ассет не найден');
  assert.match(humanizeError('ValueError: bad\ntrace'), /^The step failed: ValueError: bad$/);
  assert.equal(humanizeError(''), 'Unknown error.');
  assert.equal(jobError({ status: 'failed', steps: [] }), 'The step failed.');
  assert.equal(tailLines('a\n\nb\nc\nd', 2), 'c\nd');
  assert.equal(tailLines(['x', 'y'], 5), 'x\ny');
  assert.ok(isActiveStatus('queued') && isActiveStatus('running') && !isActiveStatus('completed'));
});

test('forge: checks from step results and Godot validation JSON', () => {
  const rows = normalizeChecks([{ name: 'unweighted_vertices', ok: true, value: 0, limit: 0 }, { name: 'bones', ok: false, severity: 'warning' }]);
  assert.equal(rows.length, 2);
  assert.deepEqual(checksSummary(rows), { total: 2, failed: 0, warn: 1, ok: true });
  assert.deepEqual(normalizeChecks({ checks: [{ id: 'scene_loads', passed: false }] }).map((r) => [r.name, r.ok]), [['scene_loads', false]]);
  assert.deepEqual(normalizeChecks({ scene_loads: true, animations: { ok: false, note: 'no -loop' } }).map((r) => [r.name, r.ok]), [['scene_loads', true], ['animations', false]]);
  const godot = normalizeChecks({ ok: false, errors: ['missing AnimationPlayer'], warnings: ['no collision'] });
  assert.equal(checksSummary(godot).failed, 1);
  assert.equal(checksSummary(godot).warn, 1);
  assert.deepEqual(normalizeChecks(null), []);
});

test('forge: stick-figure poses from several JSON shapes', () => {
  const bones = ['mixamorig:Hips', 'mixamorig:Spine', 'mixamorig:Head', 'mixamorig:LeftUpLeg'];
  const frames = [[[0, 0, 1], [0, 0, 1.2], [0, 0, 1.7], [0.1, 0, 0.9]], [[0, 0.1, 1], [0, 0.1, 1.2], [0, 0.1, 1.7], [0.1, 0.1, 0.9]]];
  const a = normalizePoses({ fps: 24, bones, frames });
  assert.equal(a.fps, 24);
  assert.equal(a.up, 'Z');
  assert.deepEqual(a.edges, [[0, 1], [0, 3]], 'Mixamo hierarchy used when parents are absent (Head has no Neck here)');
  const b = normalizePoses({ frames: [{ Hips: [0, 1, 0], Spine: [0, 1.2, 0] }], parents: { Spine: 'Hips' } });
  assert.equal(b.up, 'Y', 'flat Z with height in Y is detected as Y-up');
  assert.deepEqual(b.edges, [[0, 1]]);
  const c = normalizePoses({ bones: { root: { frames: [[0, 0, 0]] }, tip: { parent: 'root', frames: [[0, 0, 1]] } } });
  assert.deepEqual(c.edges, [[0, 1]]);
  assert.equal(c.fps, 30);
  const d = normalizePoses({ bones: ['a', 'b'], parents: [-1, 0], frames: [[[0, 0, 0], [1, 0, 0]]] });
  assert.deepEqual(d.edges, [[0, 1]]);
  assert.equal(normalizePoses({ nothing: true }), null);
  assert.equal(normalizePoses(null), null);
  assert.deepEqual(projectJoint([1, 2, 3], { up: 'Z', yaw: 0 }), [1, 3]);
  const side = projectJoint([1, 2, 3], { up: 'Z', yaw: Math.PI / 2 });
  close(side[0], 2);
  const bounds = poseBounds(a, { yaw: 0 });
  assert.deepEqual(bounds, { minX: 0, maxX: 0.1, minY: 0.9, maxY: 1.7 });
});

test('forge: logarithmic triangle budget slider', () => {
  assert.equal(sliderToBudget(0, 1000, 300000), 1000);
  assert.equal(sliderToBudget(1000, 1000, 300000), 300000);
  const mid = sliderToBudget(500, 1000, 300000);
  assert.ok(mid > 15000 && mid < 20000, String(mid));
  for (const v of [5000, 30000, 120000]) {
    const back = sliderToBudget(budgetToSlider(v, 1000, 300000), 1000, 300000);
    assert.ok(Math.abs(back - v) / v < 0.02, `${v} -> ${back}`);
  }
  assert.equal(niceBudget(12345), 12000);
  assert.equal(niceBudget(1234), 1250);
  assert.equal(niceBudget(123456), 125000);
});

test('forge: rig_humanoid markers.json (astraforge.humanoid.markers/1) without a projection block', () => {
  // Shape written by scripts/blender/steps/_humanoid_lib.py markers_json(): markers [x, z],
  // joints [x, y, z], height_m; markers.png = front + left tiles of 640 px.
  const doc = {
    format: 'astraforge.humanoid.markers/1',
    height_m: 1.8,
    markers: { chin: [0, 1.62], groin: [0, 0.86], l_wrist: [0.7, 1.4], r_wrist: [-0.7, 1.4], l_elbow: [0.45, 1.42], r_elbow: [-0.45, 1.42], l_knee: [0.1, 0.48], r_knee: [-0.1, 0.48] },
    joints: { Hips: [0, 0, 0.95], HeadTop_End: [0, 0.02, 1.8], LeftHandTip: [0.82, 0, 1.4], RightHandTip: [-0.82, 0, 1.4], LeftToe_End: [0.1, -0.2, 0], RightToe_End: [-0.1, -0.2, 0] },
  };
  assert.equal(canonicalMarkerName('l_wrist'), 'wrist_l');
  assert.equal(markerLabel('r_knee'), 'Right knee');
  assert.equal(mirrorName('l_elbow'), 'r_elbow');
  const sym = moveMarker(normalizeMarkers(doc), 'l_wrist', [0.66, 1.3], { symmetry: true });
  assert.deepEqual(sym.r_wrist, [-0.66, 1.3]);
  const box = approxMarkerBox(doc);
  assert.ok(box.lo[0] < -0.82 && box.hi[0] > 0.82, 'hands included');
  assert.ok(box.lo[2] <= 0 && box.hi[2] >= 1.8);
  assert.ok(box.lo[1] < -0.2 - 0.08 * 1.8, 'spheres sit in front of the body');
  const proj = markerProjection(doc, { width: 1280, height: 640 });
  assert.equal(proj.source, 'joints', 'flagged as approximate');
  assert.deepEqual(proj.centerPx, [320, 320], 'front tile is the left half');
  const chin = metresToPx(proj, doc.markers.chin);
  assert.ok(chin[0] > 300 && chin[0] < 340 && chin[1] > 0 && chin[1] < 320, String(chin));
  const back = pxToMetres(proj, chin);
  close(back[1], 1.62, 1e-9);
  assert.equal(approxMarkerBox({ joints: { a: [0, 0, 0] } }), null);
});

test('forge: clip_library poses.json (joints/sticks/samples, glTF Y-up) and interpolation', () => {
  const json = {
    id: 'mixamo-walk', fps: 30, frames: 30, axes: 'gltf_y_up_face_+z', units: 'm',
    joints: ['Hips', 'Spine', 'Head', 'LeftUpLeg'],
    sticks: [['Hips', 'Spine'], ['Spine', 'Head'], ['Hips', 'LeftUpLeg'], ['Hips', 'Missing']],
    samples: [
      { frame: 1, t: 0, p: [[0, 1, 0], [0, 1.2, 0], [0, 1.7, 0], [0.1, 0.9, 0]] },
      { frame: 16, t: 0.5, p: [[0, 1.1, 0], [0, 1.3, 0], [0, 1.8, 0], null] },
    ],
  };
  const poses = normalizePoses(json);
  assert.equal(poses.up, 'Y');
  assert.deepEqual(poses.edges, [[0, 1], [1, 2], [0, 3]]);
  assert.deepEqual(poses.times, [0, 0.5]);
  close(poses.duration, 1);
  const mid = poseAt(poses, 0.25);
  close(mid[0][1], 1.05);
  assert.deepEqual(mid[3], [0.1, 0.9, 0], 'missing sample keeps the known point');
  const wrap = poseAt(poses, 0.75);
  close(wrap[0][1], 1.05, 1e-9);
  close(poseAt(poses, 1.25)[0][1], 1.05, 1e-9);
  assert.deepEqual(projectJoint([0.1, 1.7, 0.3], { up: 'Y', yaw: 0 }), [0.1, 1.7]);
});

test('forge: asset-relative file paths, generator params and labels', () => {
  const id = '6f1c2b8e-1111-4222-8333-444455556666';
  assert.equal(assetRelPath(['D:', 'x', 'data', 'library', 'assets', id, 'exports', 'godot-v004.zip'].join('\\'), id), 'exports/godot-v004.zip');
  assert.equal(assetRelPath(`/d/assets/${id}/v002/01_cleanup/clean.glb`, id), 'v002/01_cleanup/clean.glb');
  assert.equal(assetRelPath(`/d/assets/${id}/x.glb`, id), null, 'needs a folder');
  assert.equal(assetRelPath(`/d/assets/${id}/v1/../../secret`, id), null);
  assert.equal(assetRelPath('/d/assets/other/v1/a.png', id), null);
  const defs = paramDefs([{ step: 'ship_gen', params: {
    class: { type: 'any', enum: ['fighter', 'miner'], default: 'fighter' },
    seed: { type: 'integer', default: 1 },
    length_m: { type: 'number', default: null, nullable: true },
    livery: { type: 'any', default: null },
    dims: { type: 'object', default: { a: 1 } },
  } }], 'ship_gen');
  assert.deepEqual(generatorParams(defs, { seed: 7 }), { class: 'fighter', seed: 7, length_m: null, livery: null }, 'scalar defaults written, empty nullables sent as null, objects left to the module');
  assert.equal(generatorParams(defs, { length_m: 40 }).length_m, 40);
  assert.deepEqual(shownParams(defs, { length_m: 40 }).length_m, 40);
  assert.equal(shownParams(defs, {}).class, 'fighter');
  assert.equal(enumLabel(null), '—');
  assert.equal(enumLabel(true), 'Yes');
  assert.equal(enumLabel('bone_child'), 'Child of the bone');
  inRussian(() => assert.equal(enumLabel(true), 'Да'));
  assert.deepEqual(batchCounts([{ status: 'created' }, { status: 'partial' }]), { total: 2, done: 1, failed: 1, running: 0, queued: 0 });
});

test('forge: marker projection self-calibrates on the joint spheres of markers.png', () => {
  const W = 640;
  const H = 320;
  const truth = { width: W, height: H, center: [0, 0], centerPx: [150, 290], mpp: 0.0075 };
  const joints = {
    Head: [0, 0, 1.6], LeftHand: [0.5, 0, 1.3], RightHand: [-0.45, 0, 1.0], LeftForeArm: [0.4, 0, 1.15],
    RightForeArm: [-0.35, 0, 1.15], LeftLeg: [0.11, 0, 0.5], RightLeg: [-0.11, 0, 0.5], Hips: [0, 0, 0.95],
  };
  const data = new Uint8ClampedArray(W * H * 4);
  const disk = (cx, cy, r, rgb) => {
    for (let y = Math.floor(cy - r); y <= cy + r; y++) for (let x = Math.floor(cx - r); x <= cx + r; x++) {
      if ((x - cx) ** 2 + (y - cy) ** 2 > r * r) continue;
      const i = (y * W + x) * 4;
      data.set([...rgb, 255], i);
    }
  };
  for (const [x, , z] of Object.values(joints)) {
    const [u, v] = metresToPx(truth, [x, z]);
    disk(u, v, 5, [220, 60, 50]);
  }
  disk(480, 200, 6, [40, 200, 60]); // a sphere in the side tile must be ignored
  const blobs = detectColorBlobs(data, W, H, { maxX: W / 2 });
  assert.equal(blobs.length, 8);
  // approximation 12 px off and 5 % too large, as measured on a real model
  const approx = { ...truth, centerPx: [162, 286], mpp: truth.mpp * 1.05, source: 'joints' };
  const fit = calibrateProjection(approx, { joints }, blobs);
  assert.equal(fit.source, 'spheres');
  assert.equal(fit.points, 8);
  assert.ok(fit.rms < 0.5, String(fit.rms));
  close(fit.mpp, truth.mpp, 1e-4);
  close(fit.centerPx[0], 150, 0.5);
  close(fit.centerPx[1], 290, 0.5);
  assert.equal(calibrateProjection(approx, { joints }, []), null);
  assert.equal(fitProjection([{ x: 0, z: 0, u: 1, v: 1 }, { x: 0, z: 0, u: 1, v: 1 }]), null, 'underdetermined');
  const m = matchPoints([{ key: 'a', u: 10, v: 10 }, { key: 'b', u: 12, v: 10 }], [{ x: 11, y: 10 }], 5);
  assert.equal(m.length, 1, 'each blob is used once');
});

test('forge: godot_check.json of godot_package becomes readable rows', () => {
  const json = { result: {
    ok: true, godot: '4.5.1-stable (official)', errors: [], warnings: ['fit_units defaulted'],
    fx_scenes: [{ ok: true, loaded: true, path: 'res://packages/ship/fx/engine_trail.tscn' }],
    scenes: [{ path: 'res://packages/ship/ship.tscn', loaded: true, instanced: true,
      fx: [{ name: 'fx_engine_trail', ok: true, socket: 'socket_thruster_main_port' }],
      sockets: { a: { found: true }, b: { found: false } }, script_missing_sockets: [], animation_checks: [{ name: 'idle-loop', ok: true }] }],
  } };
  const rows = normalizeChecks(json);
  const names = rows.map((r) => r.name);
  assert.ok(names.includes('Godot 4.5.1-stable: package opens'));
  assert.ok(names.includes('Scene ship.tscn loads'));
  assert.ok(names.includes('Effect fx_engine_trail on socket_thruster_main_port'));
  assert.ok(names.includes('FX scene engine_trail.tscn'));
  assert.ok(names.includes('Clip idle-loop'));
  const sockets = rows.find((r) => r.name === 'Sockets found');
  assert.deepEqual([sockets.ok, sockets.value, sockets.limit], [false, 1, 2]);
  assert.deepEqual(checksSummary(rows), { total: rows.length, failed: 1, warn: 1, ok: false });
});

// ------------------------------------------------------------------ fix round 10.10

const cleanupParams = { target_tris: 40000, height_m: 1.2 };

test('forge: run plan continues from the earlier wizard step (one step per version)', () => {
  const asset = {
    versions: [
      { version: 'v001', status: 'completed', base: null, steps: ['cleanup'] },
      { version: 'v002', status: 'completed', base: 'v001', steps: ['rig_humanoid'] },
      { version: 'v003', status: 'failed', base: 'v002', steps: ['retarget'] },
    ],
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001', index: 1, params: cleanupParams },
      { step: 'rig_humanoid', status: 'done', version: 'v002', index: 1, params: { height_m: 1.2 } },
      { step: 'retarget', status: 'failed', version: 'v003', index: 1 },
    ],
  };
  assert.deepEqual(runPlan(asset, 'prepare'), { from: 'source', prefix: [], replayOf: null });
  assert.deepEqual(runPlan(asset, 'skeleton'), { from: 'v001', prefix: [], replayOf: null }, 're-rig starts from the cleaned mesh');
  assert.equal(runPlan(asset, 'motion').from, 'v002');
  assert.equal(runPlan(asset, 'items').from, 'v002', 'no done motion: continues after the skeleton');
  assert.deepEqual(runPlan({ pipeline: [] }, 'skeleton'), { from: 'source', prefix: [], replayOf: null }, 'nothing earlier: from the source');
  assert.equal(runPlan({ pipeline: [{ step: 'cleanup', status: 'done', version: 'v004' }] }, 'skeleton').from, 'v004', 'no versions list: trust the entry');
});

test('forge: run plan replays earlier steps when a version holds several wizard steps (batch/preset)', () => {
  // default batch steps for a humanoid: cleanup + rig_humanoid in ONE version
  const batch = {
    versions: [{ version: 'v001', status: 'completed', base: null, steps: ['cleanup', 'rig_humanoid'] }],
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001', index: 1, params: cleanupParams },
      { step: 'rig_humanoid', status: 'done', version: 'v001', index: 2, params: { height_m: 1.2 } },
    ],
  };
  const plan = runPlan(batch, 'skeleton');
  assert.equal(plan.from, 'source', 'v001 ends rigged: never rig on top of it');
  assert.deepEqual(plan.prefix, [{ step: 'cleanup', params: cleanupParams }], 'cleanup replayed with its recorded params');
  assert.notEqual(plan.prefix[0].params, cleanupParams, 'params are copied');
  assert.equal(plan.replayOf, 'v001');
  assert.deepEqual(runPlan(batch, 'motion'), { from: 'v001', prefix: [], replayOf: null }, 'rig is the last model step of v001');
  const p = planSteps(batch, 'skeleton', [{ step: 'rig_humanoid', params: { markers: { chin: [0, 1.1] } } }]);
  assert.deepEqual(p.steps.map((s) => s.step), ['cleanup', 'rig_humanoid']);
  assert.deepEqual(p.replayed, ['cleanup']);
  assert.equal(replayNote(p, []), ' (replaying first: Cleanup and decimation)');
  inRussian(() => assert.equal(replayNote(p, []), ' (сначала повторю: Чистка и упрощение)'));
  assert.equal(replayNote(planSteps(batch, 'motion', []), []), '');

  // preset humanoid_from_mesh: cleanup → rig → retarget → godot_package in one version
  const preset = {
    versions: [
      { version: 'v001', status: 'completed', base: null, steps: ['cleanup', 'rig_humanoid', 'retarget', 'godot_package'] },
      { version: 'v002', status: 'completed', base: 'v001', from: 'latest', type: 'export', steps: ['godot_package'] },
    ],
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001', index: 1, params: cleanupParams },
      { step: 'rig_humanoid', status: 'done', version: 'v001', index: 2, params: { height_m: 1.8 } },
      { step: 'retarget', status: 'done', version: 'v001', index: 3, params: { clips: ['library:idle'] } },
      { step: 'godot_package', status: 'done', version: 'v001', index: 4, params: {} },
      { step: 'godot_package', status: 'done', version: 'v002', index: 1, params: {} },
    ],
  };
  const motion = runPlan(preset, 'motion');
  assert.equal(motion.from, 'source');
  assert.deepEqual(motion.prefix.map((s) => s.step), ['cleanup', 'rig_humanoid'], 'retarget is not stacked on retarget; exports are never replayed');
  assert.deepEqual(runPlan(preset, 'items'), { from: 'v001', prefix: [], replayOf: null }, 'only an export follows the retarget');
});

test('forge: run plan after a failed batch version replays from that version base', () => {
  // auto-rig failed in the batch version: cleanup is done there, no completed version exists
  const failed = {
    versions: [{ version: 'v001', status: 'failed', base: null, steps: ['cleanup', 'rig_humanoid'] }],
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001', index: 1, params: cleanupParams },
      { step: 'rig_humanoid', status: 'failed', version: 'v001', index: 2, params: { height_m: 1.2 } },
    ],
  };
  const plan = planSteps(failed, 'skeleton', [{ step: 'rig_humanoid', params: { height_m: 1.2, markers: { chin: [0, 1.1] } } }]);
  assert.equal(plan.from, 'source', 'not "latest": that would rig the raw, unscaled source');
  assert.deepEqual(plan.steps.map((s) => s.step), ['cleanup', 'rig_humanoid']);
  assert.deepEqual(plan.steps[0].params, cleanupParams);
  // a failed version that started from a completed base replays its own steps from that base
  const chained = {
    versions: [
      { version: 'v001', status: 'completed', base: null, steps: ['cleanup'] },
      { version: 'v002', status: 'failed', base: 'v001', steps: ['separate', 'rig_humanoid'] },
    ],
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001', index: 1, params: cleanupParams },
      { step: 'separate', status: 'done', version: 'v002', index: 1, params: { mode: 'loose' } },
      { step: 'rig_humanoid', status: 'failed', version: 'v002', index: 2 },
    ],
  };
  assert.deepEqual(runPlan(chained, 'skeleton'), { from: 'v001', prefix: [{ step: 'separate', params: { mode: 'loose' } }], replayOf: 'v002' });
  // a still running version is not a base either
  const running = { versions: [{ version: 'v004', status: 'running', base: null, steps: ['cleanup', 'rig_humanoid'] }], pipeline: [{ step: 'cleanup', status: 'done', version: 'v004', index: 1, params: {} }] };
  assert.deepEqual(runPlan(running, 'skeleton').prefix, [{ step: 'cleanup', params: {} }]);
});

test('forge: rig form seeds the height from step 2 and keeps the previous rig params', () => {
  const cleanup = { step: 'cleanup', status: 'done', version: 'v001', params: { target_tris: 30000, height_m: 1.2 } };
  assert.deepEqual(rigSeedValues({ pipeline: [cleanup], profile: { height_m: 1.8 } }), { height_m: 1.2 }, 'dwarf: 1.2 m from the cleanup, not the 1.8 m default');
  assert.deepEqual(rigSeedValues({ pipeline: [], profile: { height_m: 1.65 } }), { height_m: 1.65 }, 'profile as the last resort');
  assert.deepEqual(rigSeedValues({ pipeline: [{ step: 'cleanup', status: 'done', params: { target_tris: 5000, height_m: null } }], profile: { height_m: 1.8 } }), { height_m: 1.8 });
  const rig = { step: 'rig_humanoid', status: 'failed', version: 'v002', params: { height_m: 1.2, fingers: true, robe: 'true', markers: { chin: [0, 1] } } };
  assert.deepEqual(rigSeedValues({ pipeline: [cleanup, rig] }), { height_m: 1.2, fingers: true, robe: 'true' }, 'previous params without the markers');
  const newerCleanup = { step: 'cleanup', status: 'done', version: 'v003', params: { height_m: 1.4 } };
  assert.equal(rigSeedValues({ pipeline: [cleanup, rig, newerCleanup] }).height_m, 1.4, 'a cleanup run after the rig wins');
  assert.equal(rigSeedValues({ pipeline: [cleanup, { ...rig, params: { fingers: true } }] }).height_m, 1.2, 'rig without a height takes the cleanup one');
  assert.deepEqual(rigSeedValues(null), {});
  const m = markersForHeight({ chin: [0, 1.5], l_wrist: [0.6, 1.2] }, 1.8, 1.2);
  close(m.chin[1], 1.0);
  close(m.l_wrist[0], 0.4);
  assert.deepEqual(markersForHeight({ chin: [0, 1.5] }, 1.8, 1.8), { chin: [0, 1.5] });
  assert.deepEqual(markersForHeight({ chin: [0, 1.5] }, null, 1.2), { chin: [0, 1.5] }, 'unknown frame: unchanged');
});

test('forge: number fields clamp on blur, not on every keystroke', () => {
  const length = { type: 'number', default: null, nullable: true, min: 2, max: 2000 };
  assert.deepEqual(draftNumber(length, '1'), { value: 1, valid: true }, 'typing "1" towards 15 is not clamped to 2');
  assert.deepEqual(draftNumber(length, '15'), { value: 15, valid: true });
  assert.equal(commitNumber(length, '15'), 15);
  assert.equal(commitNumber(length, '1'), 2, 'clamped when the field loses focus');
  assert.equal(commitNumber(length, '5000'), 2000);
  assert.deepEqual(draftNumber(length, ''), { value: null, valid: true }, 'cleared nullable is an explicit null ("by class")');
  assert.deepEqual(draftNumber({ type: 'integer', default: 8, min: 8 }, ''), { value: undefined, valid: true }, 'cleared non-nullable: back to the default');
  assert.deepEqual(draftNumber(length, '1,5'), { value: 1.5, valid: true }, 'comma as the decimal mark');
  assert.equal(draftNumber(length, '-').valid, false);
  assert.equal(commitNumber({ type: 'integer', min: 100, max: 1e6 }, '250.6'), 251);
  // rig height 0.3…5 m: slider step 0.047 snapped 1.2 to 1.193; the rounded step keeps 1.2 reachable
  assert.equal(niceStep((5 - 0.3) / 100), 0.05);
  close((1.2 - 0.3) / niceStep(0.047), 18, 1e-9);
  assert.equal(niceStep(0), 0.01);
});

test('forge: Russian server errors are shown as they are', () => {
  const ru = [
    'Видеокарта занята другим процессом дольше 3600 с.',
    'Файлы ассета заняты другой программой. Закройте их и повторите.',
    'По ассету идёт задача. Отмените её или дождитесь завершения.',
    'Шаг «rig_humanoid» превысил лимит времени (30 мин) и был остановлен.',
    'Отменено пользователем',
  ];
  for (const text of ru) assert.equal(humanizeError(text), text);
  assert.equal(jobError({ status: 'failed', steps: [{ step: 'cleanup', status: 'failed', result: { error: ru[0] } }] }), ru[0]);
  assert.equal(humanizeError('Шаг завершился: checks failed: deform_torn_area'), 'Quality checks failed: deform_torn_area.');
  assert.match(humanizeError('Error: gpu_timeout while waiting'), /busy with another job for too long, the step was stopped/);
  assert.equal(humanizeError('EBUSY: resource busy or locked, rename'), 'Files are locked by another program. Close it and try again.');
  assert.equal(humanizeError('rig not built: low landmark confidence'), 'Skeleton not built: low landmark confidence. Adjust the joint points and rebuild.');
  assert.equal(humanizeError('Job was cancelled'), 'Job cancelled.');
});

test('forge: job merge keeps step results; polling plan spares queued jobs', () => {
  const full = { id: 'j', status: 'failed', steps: [{ step: 'cleanup', status: 'completed', result: { ok: true, outputs: { glb: '/a.glb' } }, logTail: ['a', 'b'] }, { step: 'rig_humanoid', status: 'failed', result: { ok: false, error: 'x' }, logTail: ['c'] }], _meta: { batchId: 'b' } };
  const listItem = { id: 'j', status: 'failed', error: 'x', steps: [{ step: 'cleanup', status: 'completed', progress: 100 }, { step: 'rig_humanoid', status: 'failed', progress: 40 }] };
  const merged = mergeJobRecord(full, listItem);
  assert.deepEqual(merged.steps[0].result, full.steps[0].result, 'list refresh does not drop results');
  assert.deepEqual(merged.steps[1].logTail, ['c']);
  assert.equal(merged.steps[1].progress, 40);
  assert.deepEqual(merged._meta, { batchId: 'b' });
  assert.deepEqual(mergeJobRecord({ id: 'j', steps: [{ step: 'cleanup', status: 'queued' }] }, { id: 'j', status: 'queued', steps: [] }).steps, [{ step: 'cleanup', status: 'queued' }]);
  assert.deepEqual(mergeJobRecord(undefined, listItem), listItem);
  const jobs = {
    a: { id: 'a', status: 'running' },
    b: { id: 'b', status: 'queued', _meta: { assetId: 'x' } },
    c: { id: 'c', status: 'queued' },
    d: { id: 'd', status: 'completed' },
  };
  for (let i = 0; i < 200; i++) jobs['q' + i] = { id: 'q' + i, status: 'queued' };
  const plan = pollPlan(jobs);
  assert.deepEqual(plan.each.sort(), ['a', 'b'], 'running + started here, not 200 queued');
  assert.equal(plan.list, true);
  assert.deepEqual(pollPlan({ a: { id: 'a', status: 'running' } }), { each: ['a'], list: false });
});

test('forge: markers.json projection block on a two-tile sheet', () => {
  // full sheet 1280×640 (front + side), no center_px: the front-tile centre, not the sheet middle
  const doc = { projection: { image: { width: 1280, height: 640 }, center: [0, 0.9], meters_per_px: 0.004 } };
  const proj = markerProjection(doc, { width: 1280, height: 640 });
  assert.equal(proj.source, 'projection');
  assert.deepEqual(proj.centerPx, [320, 320]);
  assert.deepEqual([proj.width, proj.height], [1280, 640]);
  // block describing only the front tile: same pixels on the real sheet
  const front = markerProjection({ projection: { image: { width: 640, height: 640 }, center: [0, 0.9], meters_per_px: 0.004 } }, { width: 1280, height: 640 });
  assert.deepEqual(front.centerPx, [320, 320]);
  assert.equal(front.mpp, 0.004);
  assert.equal(front.width, 1280, 'stage follows the real PNG');
  // block written for a 2560×1280 render, PNG downscaled to 1280×640: pixels scale by 0.5
  const big = markerProjection({ projection: { image: { width: 2560, height: 1280 }, center: [0, 0.9], meters_per_px: 0.002, center_px: [640, 600] } }, { width: 1280, height: 640 });
  assert.deepEqual(big.centerPx, [320, 300]);
  close(big.mpp, 0.004);
  const [u, v] = metresToPx(big, [0.4, 0.9]);
  close(u, 420);
  close(v, 300);
  assert.equal(frontTileWidth(1536, 384), 384);
  assert.equal(frontTileWidth(512, 512), 512);
});

test('forge: generator form follows the latest run; export shows what will be shipped', () => {
  const asset = {
    source: { type: 'params', params: { class: 'fighter', seed: 3, length_m: 40 } },
    pipeline: [{ step: 'ship_gen', status: 'done', version: 'v002', params: { class: 'corvette', seed: 9 } }],
  };
  assert.deepEqual(generatorSeedValues(asset, 'ship_gen'), { class: 'corvette', seed: 9 }, 'a cleared length stays cleared');
  assert.deepEqual(generatorSeedValues({ source: asset.source, pipeline: [] }, 'ship_gen'), { class: 'fighter', seed: 3, length_m: 40 });

  // went back to step 2: the latest completed version has only the cleanup
  const back = {
    versions: [
      { version: 'v001', status: 'completed', steps: ['cleanup', 'rig_humanoid', 'retarget'] },
      { version: 'v002', status: 'completed', base: null, steps: ['cleanup'] },
    ],
    pipeline: [{ step: 'cleanup', status: 'done', version: 'v002' }],
  };
  const plan = exportPlan(back, 'humanoid');
  assert.equal(plan.version, 'v002');
  assert.deepEqual(plan.steps, ['cleanup']);
  assert.deepEqual(plan.missing, [{ what: 'skeleton', older: 'v001' }, { what: 'clips', older: 'v001' }]);
  const full = {
    versions: [{ version: 'v001', status: 'completed', steps: ['cleanup', 'rig_humanoid', 'retarget'] }, { version: 'v002', status: 'failed', steps: ['attach_prop'] }],
    pipeline: [
      { step: 'cleanup', status: 'done', version: 'v001' },
      { step: 'rig_humanoid', status: 'done', version: 'v001' },
      { step: 'retarget', status: 'done', version: 'v001' },
      { step: 'attach_prop', status: 'failed', version: 'v002' },
    ],
  };
  assert.deepEqual(exportPlan(full, 'humanoid'), { version: 'v001', steps: ['cleanup', 'rig_humanoid', 'retarget'], missing: [] });
  assert.deepEqual(exportPlan({ versions: [], pipeline: [] }, 'ship'), { version: null, steps: [], missing: [] });
});

test('forge: GLB batches go to the forge as multipart parts', () => {
  const files = Array.from({ length: 45 }, (_, i) => ({ name: `orc_${String(i + 1).padStart(2, '0')}.glb` }));
  const chunks = fileBatchChunks(files, { kind: 'humanoid', steps: ['cleanup', 'rig_humanoid'] });
  assert.deepEqual(chunks.map((c) => c.files.length), [20, 20, 5]);
  assert.deepEqual(chunks[1].items[0], { name: 'orc_21', kind: 'humanoid', source: { type: 'upload', fileIndex: 0 }, steps: [{ step: 'cleanup', params: {} }, { step: 'rig_humanoid', params: {} }] });
  assert.equal(chunks[2].items[4].source.fileIndex, 4, 'index inside its own request');
  assert.equal(fileBatchChunks(files.slice(0, 2), { kind: 'ship', preset: 'ship_basic' })[0].items[1].preset, 'ship_basic');
  assert.deepEqual(fileBatchChunks([], { kind: 'ship' }), []);
  assert.equal(enumLabel('random'), 'Random');
});
