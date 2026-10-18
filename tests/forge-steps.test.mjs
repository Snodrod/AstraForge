// Forge step registry and presets: shape, defaults within ranges, Russian titles, GPU flags.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  CREATURE_SIZE_RANGES, EXPORT_PROFILES, KINDS, applyProfileSize, blenderParams, creatureSizeWarning, defaultParams,
  getPreset, isGpuStep, listPresets, listSteps, normalizeParams, pathParams, normalizeSteps, stepNames, timeoutFor,
} from '../server/forge-steps.mjs';

const PLANNED = ['inspect', 'cleanup', 'convert', 'separate', 'rig_humanoid', 'prepare_mixamo', 'import_mixamo_rig', 'retarget',
  'clip_library', 'rig_creature', 'anim_creature', 'item_kit', 'item_from_mesh', 'attach_prop', 'ship_gen', 'turret_gen',
  'ship_from_mesh', 'render_sprites', 'godot_package', 'bone_kit', 'replace_part', 'kitbash', 'place_parts', 'motion_style', 'cloth_sim',
  'paint_from_concept', 'extract_props', 'assemble_parts'];
const cyrillic = /[Ѐ-ӿ]/;
const TYPES = ['number', 'integer', 'boolean', 'string', 'array', 'object', 'any'];
// params a step cannot run without and that have no sensible default (STEP_CHECKS require them)
const REQUIRED_SAMPLE = { place_parts: { points: [[0, 1.2]] } };

test('the registry lists every planned step with API-shaped entries', () => {
  assert.deepEqual([...stepNames()].sort(), [...PLANNED].sort());
  for (const s of listSteps()) {
    assert.match(s.step, /^[a-z][a-z0-9_]*$/);
    assert.doesNotMatch(s.title, cyrillic, `${s.step}: title in English`);
    assert.ok(Array.isArray(s.kinds) && s.kinds.every((k) => KINDS.includes(k)), `${s.step}: kinds`);
    assert.ok(Array.isArray(s.inputs.slots), `${s.step}: inputs.slots`);
    assert.ok([true, false, 'conditional'].includes(s.gpu), `${s.step}: gpu flag`);
    assert.ok(s.timeoutSec >= 60, `${s.step}: timeout`);
    for (const [name, spec] of Object.entries(s.params)) {
      assert.ok(TYPES.includes(spec.type), `${s.step}.${name}: type`);
      assert.equal(typeof spec.title, 'string', `${s.step}.${name}: title`);
      if (spec.min !== undefined && spec.max !== undefined) assert.ok(spec.min <= spec.max, `${s.step}.${name}: range`);
    }
    // defaults must validate against their own schema (plus the required params that have no default)
    assert.doesNotThrow(() => normalizeParams(s.step, { ...defaultParams(s.step), ...REQUIRED_SAMPLE[s.step] }), `${s.step}: defaults valid`);
  }
  assert.throws(() => normalizeParams('place_parts', defaultParams('place_parts')), /points is required/, 'points have no default: required');
});

test('GPU flags: render_sprites always, cleanup only for GPU bake after voxel remesh', () => {
  assert.equal(isGpuStep('render_sprites'), true);
  assert.equal(timeoutFor('render_sprites'), 3600);
  assert.equal(isGpuStep('cleanup'), false);
  assert.equal(isGpuStep('cleanup', { voxel_remesh_m: 0.01 }), false);
  assert.equal(isGpuStep('cleanup', { voxel_remesh_m: 0.01, bake_device: 'GPU' }), true);
  assert.equal(isGpuStep('cleanup', { voxel_remesh_m: null, bake_device: 'GPU' }), false);
  assert.equal(isGpuStep('rig_humanoid'), false);
  assert.equal(timeoutFor('inspect'), 900);
  assert.equal(listSteps().find((s) => s.step === 'cleanup').gpu, 'conditional');
});

test('params: defaults, ranges, enums and pass-through of unknown keys', () => {
  const { params, explicit, warnings } = normalizeParams('cleanup', { target_tris: 40000, height_m: 1.8, custom_flag: 1 });
  assert.deepEqual(explicit, { target_tris: 40000, height_m: 1.8, custom_flag: 1 }, 'explicit values only (sent to Blender)');
  assert.deepEqual(normalizeSteps([{ step: 'cleanup', params: { target_tris: 40000 } }]).steps[0].params, { target_tris: 40000 });
  assert.equal(params.target_tris, 40000);
  assert.equal(params.shade_auto_smooth_deg, 40);
  assert.equal(params.bake_size, 2048);
  assert.equal(params.recalc_normals, true);
  assert.equal(params.custom_flag, 1);
  assert.equal(warnings.length, 1);
  assert.throws(() => normalizeParams('cleanup', { remove_islands_below: 2 }), /at most 1/);
  assert.throws(() => normalizeParams('cleanup', { bake_device: 'TPU' }), /allowed values/);
  assert.throws(() => normalizeParams('rig_humanoid', { max_influences: 2.5 }), /an integer/);
  assert.throws(() => normalizeParams('anim_creature', { clips: ['moonwalk'] }), /allowed values/);
  assert.throws(() => normalizeParams('nope', {}), /Unknown step/);
  assert.equal(normalizeParams('cleanup', { symmetry: null }).params.symmetry, null);
  assert.equal(normalizeParams('rig_humanoid', { robe: true }).params.robe, true);
  assert.equal(normalizeParams('godot_package', {}).params.profile, 'godot');
  // server-only params are not sent to Blender
  assert.equal('prop_asset' in blenderParams('attach_prop', normalizeParams('attach_prop', { prop_asset: null }).params), false);
});

test('godot_package: godot is the only profile; game registry params are gone', () => {
  assert.throws(() => normalizeParams('godot_package', { profile: 'unity' }), /allowed values/);
  const params = listSteps().find((s) => s.step === 'godot_package').params;
  assert.deepEqual(params.profile.enum, ['godot']);
  for (const k of ['fit_units', 'min_units', 'comment']) assert.equal(k in params, false, k);
});

test('presets reference real steps with valid params', () => {
  const ids = listPresets().map((p) => p.id);
  for (const id of ['humanoid_from_mesh', 'humanoid_sprites', 'creature_auto', 'item_kit_sword', 'item_from_mesh', 'ship_procedural', 'ship_from_mesh', 'turret']) {
    assert.ok(ids.includes(id), id);
  }
  for (const p of listPresets()) {
    assert.ok(KINDS.includes(p.kind), p.id);
    assert.doesNotMatch(p.title, cyrillic);
    const { warnings } = normalizeSteps(p.steps);
    assert.deepEqual(warnings, [], `${p.id}: no unknown params`);
  }
  const human = getPreset('humanoid_from_mesh');
  assert.deepEqual(human.steps.map((s) => s.step), ['cleanup', 'rig_humanoid', 'retarget', 'godot_package']);
  assert.equal(human.steps[0].params.target_tris, 40000);
  assert.equal(human.steps[0].params.height_m, 1.8);
  const clips = human.steps[2].params.clips;
  assert.deepEqual(clips.map((c) => c.source), ['category:idle', 'category:walk', 'category:attack', 'category:hit', 'category:death']);
  assert.equal(clips[1].loop, true);
  assert.equal(clips[1].in_place, true);
  const spr = getPreset('humanoid_sprites').steps;
  assert.deepEqual(spr.map((s) => s.step), ['cleanup', 'rig_humanoid', 'retarget', 'render_sprites', 'godot_package'],
    'sprites are rendered before the Godot package so it includes them (inputs.sprites)');
  assert.deepEqual(spr.find((s) => s.step === 'render_sprites'), { step: 'render_sprites', params: { profile: 'iso8', in_place: 'drift' } }, 'sprites in place');
  assert.deepEqual(getPreset('creature_auto').steps[2].params.clips, ['idle-loop', 'walk-loop', 'run-loop', 'attack', 'hit', 'death']);
  const ship = getPreset('ship_procedural');
  assert.equal(ship.steps[0].params.class, 'fighter');
  assert.equal(ship.steps[1].params.profile, 'godot');
  assert.deepEqual(ship.steps[1].params.fx[0], { preset: 'engine_trail', socket: 'socket_thruster_*', params: {} });
  // presets are returned as copies
  human.steps.length = 0;
  assert.equal(getPreset('humanoid_from_mesh').steps.length, 4);
});

test('export profiles map to export steps', () => {
  assert.equal(EXPORT_PROFILES.godot.step, 'godot_package');
  assert.deepEqual(Object.keys(EXPORT_PROFILES).sort(), ['godot', 'iso8']);
  assert.equal(EXPORT_PROFILES.iso8.params.profile, 'iso8');
  assert.throws(() => normalizeSteps([]), /non-empty/);
  assert.throws(() => normalizeSteps([{ params: {} }]), /name is not specified/);
});

test('empty values, path params and server-filled ids', () => {
  // null for a param whose default is null means "the module decides": it is not sent
  const n = normalizeParams('render_sprites', { engine: null, profile: 'custom' });
  assert.deepEqual(n.explicit, { profile: 'custom' });
  assert.equal(n.params.engine, null);
  assert.deepEqual(normalizeParams('cleanup', { target_tris: null, symmetry: 'X' }).explicit, { symmetry: 'X' });
  assert.throws(() => normalizeParams('cleanup', { recalc_normals: null }), /must not be empty/);
  // local-path params (only the CLI may set them) and the server-filled library folder
  assert.deepEqual(pathParams('import_mixamo_rig'), ['texture_source']);
  assert.deepEqual(pathParams('retarget'), ['library']);
  assert.deepEqual(pathParams('cleanup'), []);
  // ships get the same snake_case asset_id as their Godot package
  for (const step of ['ship_gen', 'turret_gen', 'ship_from_mesh', 'godot_package', 'render_sprites']) {
    assert.equal(listSteps().find((s) => s.step === step).params.asset_id.auto, 'asset_slug', step);
  }
  // module-native names: item_from_mesh.budget_tris, render_sprites engine by profile
  assert.ok(listSteps().find((s) => s.step === 'item_from_mesh').params.budget_tris);
  assert.equal(defaultParams('render_sprites').engine, null);
  assert.match(listSteps().find((s) => s.step === 'godot_package').params.fx.help, /mask/);
});

// ------------------------------------------------------------------ reconciliation with the step modules (10.10)

const stepDef = (name) => listSteps().find((s) => s.step === name);
const bad = (step, params, re) => assert.throws(() => normalizeParams(step, params), (e) => e.code === 'bad_params' && re.test(e.message));

test('core steps take inputs.mesh or inputs.blend; separate regions are metre boxes', () => {
  for (const step of ['cleanup', 'convert', 'separate']) {
    const slots = stepDef(step).inputs.slots;
    assert.ok(slots.includes('mesh') && slots.includes('blend'), `${step}: mesh and blend`);
  }
  assert.deepEqual(stepDef('cleanup').inputs.slots, ['mesh', 'blend'], 'the clean GLB is preferred over a .blend with helper objects');
  const regions = [{ name: 'staff', min: [-0.1, -0.1, 0.2], max: [0.1, 0.1, 1.9] }, { name: 'staff', min: [-0.1, -0.1, -0.05], max: [0.1, 0.1, 0.1] }];
  assert.deepEqual(normalizeParams('separate', { mode: 'regions', regions, height_m: 1.8 }).explicit.regions, regions);
  bad('separate', { mode: 'regions' }, /needs zones/);
  bad('separate', { mode: 'regions', regions: [{ name: 'x', min: [0, 0], max: [1, 1] }] }, /in meters/);
  bad('separate', { mode: 'regions', regions: [{ name: 'x', min: [0, 0, 0.5], max: [1, 1, 0.4] }] }, /greater than min/);
  bad('separate', { regions: [{ min: [0, 0, 0], max: [1, 1, 1] }] }, /no name/);
  for (const k of ['planar_angle_deg', 'decimate_stage_ratio', 'bake_normal', 'bake_samples', 'preview_size']) assert.ok(stepDef('cleanup').params[k], `cleanup.${k}`);
  assert.equal(stepDef('convert').params.ground.default, null, 'convert grounds only with height_m or mixamo');
});

test('retarget: root motion modes, in_place true|false|strict, locomotion, apply_transforms', () => {
  const p = stepDef('retarget').params;
  assert.match(p.root_motion.help, /walk\/run/);
  assert.match(p.root_motion.help, /root/);
  assert.equal(p.apply_transforms.default, false);
  assert.equal(p.keep_existing_actions.default, false);
  assert.equal(p.foot_pin.default, 'ball');
  const clips = [
    { source: 'category:walk', in_place: true, locomotion: true },
    { source: 'category:attack', in_place: 'strict' },
    { source: 'library:mixamo-death', in_place: false, trim: [1, 60], fps: 30, speed: 1.2, mirror: false },
    'category:idle',
  ];
  assert.equal(normalizeParams('retarget', { clips }).explicit.clips.length, 4);
  bad('retarget', { clips: [{ source: 'category:walk', in_place: 'yes' }] }, /true, false or "strict"/);
  bad('retarget', { clips: [{ source: 'category:walk', in_place: null }] }, /root_motion/);
  bad('retarget', { clips: [{ source: 'category:walk', locomotion: 'walk' }] }, /locomotion/);
  bad('retarget', { clips: [{ source: 'category:walk', trim: [60, 1] }] }, /trim/);
  bad('retarget', { clips: [{ name: 'walk' }] }, /no source/);
  bad('retarget', { clips: [{ source: 'category:walk', speed: 0 }] }, /speed/);
});

test('creatures: speed is metres per second (empty = step decides), tempo is the multiplier; head_dir labels or vectors', () => {
  const a = stepDef('anim_creature');
  assert.equal(a.params.speed.default, null);
  assert.match(a.params.speed.title, /m\/s/);
  assert.equal(a.params.tempo.default, 1);
  assert.equal(a.params.clips.default, null, 'every clip that fits the template');
  assert.deepEqual(a.inputs.slots, ['blend'], 'needs the rig_creature .blend');
  assert.deepEqual(normalizeParams('anim_creature', { speed: null }).explicit, {}, 'empty speed is not sent');
  assert.deepEqual(normalizeParams('anim_creature', { speed: { 'walk-loop': 1.4, 'run-loop': 4 } }).explicit.speed, { 'walk-loop': 1.4, 'run-loop': 4 });
  bad('anim_creature', { speed: 'fast' }, /a number/);
  const r = stepDef('rig_creature').params;
  assert.ok(r.template.enum.includes('octopod'));
  assert.deepEqual(r.weights.enum, ['auto', 'heat', 'distance']);
  for (const v of ['auto', '-Y', '+X', [0, -1], [1, 0, 0]]) assert.doesNotThrow(() => normalizeParams('rig_creature', { head_dir: v, facing: v }), JSON.stringify(v));
  bad('rig_creature', { head_dir: 'north' }, /allowed values/);
  bad('rig_creature', { head_dir: [1] }, /allowed values/);
  assert.doesNotThrow(() => normalizeParams('rig_creature', { tail: 3 }));
  bad('rig_creature', { tail: 'long' }, /allowed values/);
});

test('items: preview_size vs size, forward, sheets, item_from_mesh and attach_prop params', () => {
  const k = stepDef('item_kit').params;
  assert.equal(k.preview_size.default, null);
  assert.match(k.size.help, /not the preview/);
  assert.deepEqual(k.forward.enum, ['-Y', '+Y']);
  assert.equal(k.forward.default, '-Y');
  for (const name of ['sheet_scale', 'sheet_cols', 'sheet_tile']) assert.ok(k[name], name);
  assert.match(k.batch.help, /lists/);
  const f = stepDef('item_from_mesh').params;
  for (const name of ['kind', 'length_m', 'crop', 'tip_end', 'edge', 'allow_over_budget', 'budget_tris']) assert.ok(f[name], name);
  assert.doesNotThrow(() => normalizeParams('item_from_mesh', { kind: 'staff', edge: [0, -1, 0], tip_end: 'max', crop: { min: [0, 0, 0], max: [1, 1, 1] } }));
  bad('item_from_mesh', { edge: [0, -1] }, /allowed values/);
  bad('item_from_mesh', { kind: 'gun' }, /allowed values/);
  const at = stepDef('attach_prop');
  for (const name of ['attachments', 'fit', 'standoff', 'thumb_hint', 'item_scale', 'replace', 'refit_shield_mount']) assert.ok(at.params[name], name);
  assert.ok(at.inputs.formats.includes('fbx'), 'FBX characters');
  for (const v of [{ item_scale: 'auto' }, { item_scale: 1.2 }, { standoff: 'auto' }, { standoff: 0.04 }, { thumb_hint: [0, -1, 0] }]) {
    assert.doesNotThrow(() => normalizeParams('attach_prop', v), JSON.stringify(v));
  }
  bad('attach_prop', { item_scale: 'big' }, /a number/);
  bad('attach_prop', { attachments: [{ hand: 'r' }] }, /asset/);
  bad('attach_prop', { attachments: [{ asset: '1a2b3c4d-0000-4000-8000-000000000000', hand: 'x' }] }, /hand/);
  assert.equal('clips' in blenderParams('attach_prop', { clips: ['category:idle'], hand: 'r' }), false, 'clips go to inputs.clips, not params');
});

test('ships and export: thruster frames, layouts, lod_strip, profile-dependent sprite defaults', () => {
  const g = stepDef('ship_gen').params;
  assert.deepEqual(g.thruster_frame.enum, ['thrust', 'exhaust']);
  assert.equal(g.thruster_frame.default, 'thrust');
  assert.equal(g.layout.default, 'auto');
  assert.equal(g.engine_count.max, 6);
  assert.equal(g.wing_style.default, 'auto');
  assert.equal(stepDef('ship_from_mesh').params.thruster_frame.default, 'thrust');
  assert.ok(stepDef('ship_from_mesh').outputs.includes('lod_strip'));
  assert.deepEqual(stepDef('ship_from_mesh').inputs.slots, ['mesh'], 'the step reads inputs.mesh only');
  const s = stepDef('render_sprites');
  assert.equal(s.inputs.manifest, true, 'manifest kind decides the forward axis');
  assert.equal(s.params.pixels_per_meter.default, null);
  assert.equal(s.params.elevation_deg.default, null, 'module default 30°');
  assert.deepEqual(s.params.profile.enum, ['iso8', 'custom']);
  for (const k of ['kind', 'adaptive_cell', 'event_frames']) assert.equal(k in s.params, false, k);
  assert.equal(s.params.fps.default, null);
  // generators get their scalar defaults written out by the wizard: they must equal the modules'
  assert.deepEqual(
    Object.fromEntries(['class', 'seed', 'style', 'wing_style', 'greeble_density', 'thruster_frame', 'tier', 'uv', 'preview_size'].map((n) => [n, g[n].default])),
    { class: 'fighter', seed: 1, style: 'military', wing_style: 'auto', greeble_density: 0.6, thruster_frame: 'thrust', tier: 1, uv: 'smart', preview_size: 300 });
  const t = stepDef('turret_gen').params;
  assert.deepEqual([t.size.default, t.barrels.default, t.seed.default, t.style.default, t.preview_size.default], ['M', 2, 1, 'military', 300]);
});

test('presets: Godot humanoids extract root motion, sprites keep lock, creatures do not force speed', () => {
  const retargetOf = (id) => getPreset(id).steps.find((s) => s.step === 'retarget').params;
  assert.equal(retargetOf('humanoid_from_mesh').root_motion, 'extract');
  assert.equal(retargetOf('humanoid_sprites').root_motion, 'lock');
  const anim = getPreset('creature_auto').steps.find((s) => s.step === 'anim_creature').params;
  assert.equal('speed' in anim, false);
  assert.equal('tempo' in anim, false);
  for (const p of listPresets()) {
    for (const s of p.steps) {
      const known = stepDef(s.step).params;
      for (const k of Object.keys(s.params || {})) assert.ok(known[k], `${p.id}: ${s.step}.${k} is a registry param`);
    }
  }
});

test('creature size: passport size fills rig_creature / creature cleanup; implausible native sizes per template', () => {
  const steps = () => normalizeSteps(getPreset('creature_auto').steps).steps;
  let st = steps();
  assert.deepEqual(applyProfileSize(st, { kind: 'creature', profile: { height_m: 1.7, length_m: 2.4 } }), []);
  assert.equal(st[0].params.height_m, 1.7);
  assert.equal(st[1].params.height_m, 1.7, 'height wins over length (rig_creature does the same)');
  assert.equal('length_m' in st[1].params, false);
  st = steps();
  applyProfileSize(st, { kind: 'creature', profile: { template: 'auto' } });
  assert.equal('height_m' in st[0].params || 'height_m' in st[1].params, false, 'no passport size: native size');
  st = steps();
  assert.match(applyProfileSize(st, { kind: 'creature', profile: { height_m: 500 } })[0], /outside the allowed range/);
  assert.equal('height_m' in st[1].params, false);
  st = [{ step: 'cleanup', params: {} }];
  applyProfileSize(st, { kind: 'item', profile: { height_m: 0.9 } });
  assert.equal('height_m' in st[0].params, false, 'only a creature cleanup takes the passport height');

  assert.equal(creatureSizeWarning('quadruped', [0.38, 1.0, 0.874], {}), null);
  assert.match(creatureSizeWarning('quadruped', [0.1, 0.3, 0.2], {}), /height 0\.2 m \(expected 0\.3–6 m\)/);
  assert.match(creatureSizeWarning('quadruped', [3, 12, 7.5], {}), /height 7.5 m/);
  assert.equal(creatureSizeWarning('quadruped', [0.1, 0.3, 0.2], { height_m: 0.2 }), null, 'an explicit size is the user\'s choice');
  assert.match(creatureSizeWarning('serpent', [0.1, 0.2, 0.05], {}), /length 0.2 m/);
  assert.equal(creatureSizeWarning('custom', [0.01, 0.01, 0.01], {}), null);
  assert.deepEqual(CREATURE_SIZE_RANGES.quadruped, { dim: 'height', min: 0.3, max: 6 });
});

test('bone_kit and replace_part: registered with the params of their run(req)', () => {
  const bk = stepDef('bone_kit');
  assert.deepEqual(bk.inputs.slots, [], 'generator');
  assert.deepEqual(bk.params.part.enum, ['skeleton_hand', 'bone_staff', 'bone_toggle']);
  for (const k of ['side', 'pose', 'curl', 'grip_radius_m', 'rings', 'grip_fraction', 'segments', 'talons', 'crystal_height_m', 'gold', 'loop', 'batch', 'palette']) assert.ok(bk.params[k], `bone_kit.${k}`);
  assert.deepEqual(normalizeParams('bone_kit', { part: 'skeleton_hand', side: 'l', pose: 'grip', rings: ['index', 'middle'], style: 'aged' }).warnings, []);
  assert.equal(normalizeParams('bone_kit', { part: 'bone_staff', length_m: 2.05, origin: 'axis', talons: 3 }).explicit.talons, 3);
  bad('bone_kit', {}, /part is required/);
  bad('bone_kit', { part: 'bone_staff', origin: 'wrist' }, /origin for bone_staff/);
  bad('bone_kit', { part: 'bone_toggle', length_m: 1.2 }, /length_m for bone_toggle/);
  bad('bone_kit', { part: 'skeleton_hand', pose: 'wave' }, /allowed values/);
  bad('bone_kit', { part: 'skeleton_hand', rings: ['toe'] }, /allowed values/);
  bad('bone_kit', { part: 'skeleton_hand', batch: [{ part: 'bone_staff' }, { origin: 'axis' }] }, /variant #2: origin for skeleton_hand/);
  assert.doesNotThrow(() => normalizeParams('bone_kit', { part: 'skeleton_hand', batch: [{ pose: 'fist' }, { part: 'bone_staff', origin: 'axis' }] }));

  const rp = stepDef('replace_part');
  assert.deepEqual(rp.inputs.slots, ['mesh', 'blend', 'part']);
  assert.equal(rp.params.part_asset.server, true, 'part_asset is consumed by the server');
  assert.equal(rp.params.part_file.path, true, 'part_file is a local path');
  for (const k of ['region', 'cut_plane', 'align', 'texture', 'eyes', 'height_m', 'part_tris', 'seam_gap']) assert.ok(rp.params[k], `replace_part.${k}`);
  const region = { min: [-0.2, -0.2, 1.45], max: [0.2, 0.2, 1.85] };
  const n = normalizeParams('replace_part', { part_asset: '00000000-0000-4000-8000-000000000001', region, cut_plane: { axis: 'z', value: 1.5 }, eyes: { auto: true } });
  assert.deepEqual(n.warnings, []);
  assert.deepEqual(Object.keys(blenderParams('replace_part', n.explicit)).sort(), ['cut_plane', 'eyes', 'region'], 'part_asset / part_file are resolved by the server into inputs.part');
  assert.ok(pathParams('replace_part').includes('part_file'), 'part_file is a local path: CLI only');
  bad('replace_part', { region: { min: [0, 0, 1], max: [1, 1, 0.5] } }, /region.max/);
  bad('replace_part', { region: { min: [0, 0], max: [1, 1] } }, /region/);
  bad('replace_part', { cut_plane: { axis: 'w', value: 1 } }, /cut_plane/);
  bad('replace_part', { part_asset: 'skull' }, /part_asset/);
  bad('replace_part', { align: 'nearest' }, /allowed values/);
});

test('kitbash, ship_from_mesh paint / armour / decals / palette and rotary turrets are registered and checked', () => {
  const kb = stepDef('kitbash');
  assert.deepEqual(kb.inputs.slots, ['blend', 'mesh']);
  assert.ok(kb.outputs.includes('glb') && kb.outputs.includes('lod2') && kb.outputs.includes('manifest'));
  const ok = normalizeParams('kitbash', { parts: [
    { kind: 'gatling', at: 'socket_turret_*', length: 5, remove: { radius: 0.8 } },
    { kind: 'nozzle', at: 'socket_thruster_main_port', diameter: 5.4 },
    { kind: 'piping', points: [[0, 0, 0], [1, 2, 3]] },
    { kind: 'antenna', at: [0.9, 9.8], height: 4, socket: 'socket_utility_mast_01' },
    { kind: 'nav_light', at: [[1, 2, 3], [4, 5, 6]] },
    { kind: 'remove', regions: [{ box: { min: [0, 0, 0], max: [1, 1, 1] } }] }] });
  assert.equal(ok.explicit.parts.length, 6);
  assert.throws(() => normalizeParams('kitbash', { parts: [{ kind: 'laser_disco', at: 'socket_x' }] }), /kind/);
  assert.throws(() => normalizeParams('kitbash', { parts: [{ kind: 'piping', points: [[0, 0, 0]] }] }), /points/);
  assert.throws(() => normalizeParams('kitbash', { parts: [{ kind: 'gatling' }] }), /at/);
  assert.throws(() => normalizeParams('kitbash', { parts: [{ kind: 'remove', regions: [{ cube: 1 }] }] }), /regions/);
  assert.throws(() => normalizeParams('kitbash', { parts: [{ kind: 'gatling', at: 'socket_turret_*', remove: { size: 3 } }] }), /remove/);
  const s = stepDef('ship_from_mesh').params;
  for (const k of ['palette', 'livery', 'paint', 'armor_texture', 'decals', 'turret_sockets', 'lod_targets', 'thruster_cue']) assert.ok(s[k], k);
  assert.deepEqual(s.palette.enum, ['input', 'faction']);
  assert.ok(s.materials.enum.includes('paint'));
  normalizeParams('ship_from_mesh', { palette: 'input', paint: { image: 'x.png', regions: [{ box: [0.1, 0.2, 0.3, 0.4], label: 'glass' }] },
    decals: [{ text: 'AF-03', side: 'both', position: [-13.6, 2.7], size: [11.2, 4.8] }], armor_texture: 'armor_hex' });
  assert.throws(() => normalizeParams('ship_from_mesh', { palette: 'rainbow' }), /palette/);
  assert.throws(() => normalizeParams('ship_from_mesh', { paint: { camera: 'fit' } }), /paint/);
  assert.throws(() => normalizeParams('ship_from_mesh', { paint: { asset: 'not-a-uuid' } }), /paint\.asset/);
  assert.throws(() => normalizeParams('ship_from_mesh', { decals: [{ text: 'X', position: [1, 2] }] }), /size/);
  assert.deepEqual(stepDef('turret_gen').params.barrel_style.enum, ['plain', 'rotary']);
  assert.equal(stepDef('turret_gen').params.barrel_style.default, 'plain');
});

// ------------------------------------------------------------------ v2 capabilities (10.10 night)

const UUID1 = '00000000-0000-4000-8000-000000000001';
const UUID2 = '00000000-0000-4000-8000-000000000002';
/** Registry defaults that must equal the module's own default (p.get(key, default) in the .py). */
const moduleDefaults = (step, table) => {
  const p = stepDef(step).params;
  for (const [k, v] of Object.entries(table)) assert.deepEqual(p[k]?.default, v, `${step}.${k}: module default`);
};

test('place_parts: registered with the params of place_parts.py; points required and checked', () => {
  const pp = stepDef('place_parts');
  assert.equal(pp.module, 'core');
  assert.equal(pp.title, 'Parts placement');
  assert.deepEqual(pp.inputs.slots, ['mesh', 'blend', 'part'], 'target from the chain + inputs.part like replace_part');
  assert.ok(pp.inputs.formats.includes('blend') && pp.inputs.formats.includes('glb'));
  assert.deepEqual(pp.outputs, ['glb', 'blend', 'preview']);
  // scripts/blender/steps/place_parts.py run(req): p.get(key, default)
  moduleDefaults('place_parts', {
    ray: '+Y', contact: 'footprint', normal: 'fit', footprint_grid: [5, 3], footprint_fill: 0.8, rest_percentile: 100,
    normal_patch: 1, max_tilt_deg: 45, up: [0, 0, 1], join: true, part_sockets: true, rotate_z_deg: 0, ground: false,
    center: false, preview: true, preview_size: 512,
    // "empty — the module decides": not sent
    search_m: null, name: null, height_m: null, target_objects: null, points: null,
  });
  assert.deepEqual(pp.params.ray.enum, ['+Y', '-Y', '+X', '-X', '+Z', '-Z']);
  assert.deepEqual(pp.params.contact.enum, ['footprint', 'center']);
  assert.deepEqual(pp.params.normal.enum, ['fit', 'hit', 'axis']);
  assert.equal(pp.params.part_asset.server, true);
  assert.equal(pp.params.part_file.server, true);
  assert.equal(pp.params.part_file.path, true);
  assert.deepEqual(pathParams('place_parts'), ['part_file'], 'part_file is a local path: CLI only');

  const points = [
    [-0.08, 1.32],
    [0.08, -0.12, 1.32],
    { at: [0.1, 1.1], asset: UUID2, name: 'clasp_l', scale: 1.5, roll_deg: 10, standoff_m: 0.002, sockets: false, rest_percentile: 80, normal_patch: 2, max_tilt_deg: 30 },
    { at: [0.3, -0.05, 1.0], snap: 'none', rot_deg: [0, 0, 90], part: 'C:/parts/hand_r.glb' },
    { at: [-0.3, -0.05, 1.0], snap: 'NONE', matrix: [[1, 0, 0], [0, 1, 0], [0, 0, 1]] },
  ];
  const n = normalizeParams('place_parts', { part_asset: UUID1, part_file: 'C:/parts/toggle.glb', points, ray: '-X', contact: 'center', normal: 'hit',
    footprint_grid: [7, 3], up: [0, 1, 0], join: false, target_objects: ['body'], search_m: 0.3, ground: true, height_m: 1.8 });
  assert.deepEqual(n.warnings, []);
  assert.deepEqual(n.explicit.points, points);
  const sent = blenderParams('place_parts', n.explicit);
  assert.equal('part_asset' in sent || 'part_file' in sent, false, 'the part goes to inputs.part, not params');
  assert.deepEqual(sent.points, points);

  bad('place_parts', {}, /points is required/);
  bad('place_parts', { points: [] }, /points/);
  bad('place_parts', { points: [[1]] }, /point #1: at/);
  bad('place_parts', { points: [{ at: [0, 1], snap: 'stick' }] }, /snap must be surface or none/);
  bad('place_parts', { points: [{ at: [0, 1], snap: 'none' }] }, /snap none/);
  bad('place_parts', { points: [{ at: [0, 1], scale: 0 }] }, /scale/);
  bad('place_parts', { points: [{ at: [0, 0, 1], snap: 'none', matrix: [[-1, 0, 0], [0, 1, 0], [0, 0, 1]] }] }, /matrix/);
  bad('place_parts', { points: [{ at: [0, 1], asset: 'skull' }] }, /asset must be the UUID/);
  bad('place_parts', { points: [{ at: [0, 1], asset: UUID2, part: 'x.glb' }] }, /either asset/);
  bad('place_parts', { points: [{ at: [0, 1], roll: 10 }] }, /unknown keys roll/);
  bad('place_parts', { points: [{ at: [0, 1], rest_percentile: 120 }] }, /rest_percentile/);
  bad('place_parts', { points: [[0, 1]], part_asset: 'toggle' }, /part_asset/);
  bad('place_parts', { points: [[0, 1]], ray: '+W' }, /allowed values/);
  bad('place_parts', { points: [[0, 1]], footprint_grid: [0, 3] }, /footprint_grid/);
  bad('place_parts', { points: [[0, 1]], footprint_grid: [5] }, /at least 2/);
  bad('place_parts', { points: [[0, 1]], up: [0, 0, 0] }, /up/);
  bad('place_parts', { points: [[0, 1]], max_tilt_deg: 120 }, /at most 90/);
});

test('replace_part: align none, part_transform, keep_materials, cut_part', () => {
  const p = stepDef('replace_part').params;
  assert.deepEqual(p.align.enum, ['icp', 'bbox', 'none']);
  moduleDefaults('replace_part', { align: 'icp', keep_materials: false, cut_part: true, part_transform: null });
  const region = { min: [-0.2, -0.2, 1.45], max: [0.2, 0.2, 1.85] };
  for (const t of [{ location: [0.31, -0.02, 1.0], rot_deg: [0, 0, 90], scale: 1.1 }, { matrix: [[0, -1, 0], [1, 0, 0], [0, 0, 1]] }, { location: [0, 0, 1] }]) {
    const n = normalizeParams('replace_part', { region, align: 'none', part_transform: t, keep_materials: true, cut_part: false });
    assert.deepEqual(n.warnings, [], JSON.stringify(t));
    assert.deepEqual(n.explicit.part_transform, t);
  }
  bad('replace_part', { part_transform: { loc: [0, 0, 1] } }, /part_transform must be \{location/);
  bad('replace_part', { part_transform: { location: [0, 1] } }, /part_transform.location/);
  bad('replace_part', { part_transform: { scale: 0 } }, /part_transform.scale/);
  bad('replace_part', { part_transform: { matrix: [[1, 0, 0], [0, 1, 0], [0, 0, -1]] } }, /part_transform.matrix/);
});

test('rig_humanoid: grip_dirs, input sockets, small islands, surface-aware pruning, arm bleed', () => {
  // rig_humanoid.py / _humanoid_lib.skin: params.get(key, default)
  moduleDefaults('rig_humanoid', {
    keep_input_sockets: true, island_max_frac: 0.01, surface_aware: true, surface_margin_h: 0.05, arm_bleed: [0.1, 0.35],
    shield_mounts: true, replace_sockets: [], grip_dirs: null, socket_bones: null,
  });
  const n = normalizeParams('rig_humanoid', { grip_dirs: { l: 'up', R: [0, -1, 0.2] }, keep_input_sockets: true, socket_bones: { socket_fx_eye_l: 'Head' },
    replace_sockets: ['socket_mount_hand_r'], island_max_frac: 0.02, surface_aware: false, surface_margin_h: 0.08, arm_bleed: [0.05, 0.3] });
  assert.deepEqual(n.warnings, []);
  assert.equal(normalizeParams('rig_humanoid', { arm_bleed: null }).explicit.arm_bleed, null, 'null is sent: the module turns the bleed off');
  assert.doesNotThrow(() => normalizeParams('rig_humanoid', { arm_bleed: false }));
  bad('rig_humanoid', { grip_dirs: { x: 'up' } }, /l or r/);
  bad('rig_humanoid', { grip_dirs: { l: 'sideways' } }, /grip_dirs.l/);
  bad('rig_humanoid', { grip_dirs: { l: [0, 0, 0] } }, /non-zero/);
  bad('rig_humanoid', { socket_bones: { socket_fx_eye_l: 3 } }, /socket_bones/);
  bad('rig_humanoid', { arm_bleed: [0.5, 0.2] }, /arm_bleed/);
  bad('rig_humanoid', { arm_bleed: [0.1] }, /at least 2/);
});

test('attach_prop: refit_socket and grip_dir, also per attachment', () => {
  moduleDefaults('attach_prop', { refit_socket: false, grip_dir: null });
  for (const v of [{ refit_socket: true, grip_dir: 'up' }, { grip_dir: [0, 0, 1] }, { grip_dir: 'OUT' },
    { attachments: [{ asset: UUID1, hand: 'l', refit_socket: true, grip_dir: 'forward' }, { asset: UUID2, grip_dir: [0, -1, 0] }] }]) {
    assert.deepEqual(normalizeParams('attach_prop', v).warnings, [], JSON.stringify(v));
  }
  bad('attach_prop', { grip_dir: 'north' }, /grip_dir/);
  bad('attach_prop', { grip_dir: [0, 0, 0] }, /grip_dir/);
  bad('attach_prop', { attachments: [{ asset: UUID1, refit_socket: 'yes' }] }, /refit_socket/);
  bad('attach_prop', { attachments: [{ asset: UUID1, grip_dir: 'left' }] }, /item #1: grip_dir/);
});

test('retarget: facing, hips, floors and ground — global and per clip', () => {
  // retarget.py: P.get(key, default)
  moduleDefaults('retarget', {
    facing: 'keep', facing_bone: null, hips_drop: 1, hips_height_m: null, floor: true, hand_floor: null, ground: 'feet',
    ground_tolerance_m: 0, ground_samples: 2000, floor_tolerance_m: 0.02, floor_samples: 16,
  });
  assert.deepEqual(stepDef('retarget').params.facing.enum, ['keep', 'rest']);
  assert.deepEqual(stepDef('retarget').params.ground.enum, ['feet', 'mesh']);
  const clips = [
    { source: 'category:idle', facing: 'rest', facing_bone: 'Spine2', hips_drop: 0, hand_floor: { l: 0.03 } },
    { source: 'category:death', ground: 'mesh', hips_drop: 1, facing: 'KEEP' },
    'category:walk',
  ];
  const n = normalizeParams('retarget', { clips, facing: 'rest', hips_drop: 0, hips_height_m: 0.95, floor: true, hand_floor: { l: 0.03, right: 0.05 },
    ground: 'mesh', ground_tolerance_m: 0.01, ground_samples: 3000, floor_tolerance_m: 0.03, floor_samples: 24 });
  assert.deepEqual(n.warnings, []);
  assert.deepEqual(n.explicit.clips, clips);
  bad('retarget', { clips: [{ source: 'category:idle', facing: 'left' }] }, /clip #1: facing/);
  bad('retarget', { clips: [{ source: 'category:idle', hips_drop: 2 }] }, /hips_drop/);
  bad('retarget', { clips: [{ source: 'category:idle', ground: 'water' }] }, /ground must be feet or mesh/);
  bad('retarget', { clips: [{ source: 'category:idle', hand_floor: { x: 0.1 } }] }, /hand_floor/);
  bad('retarget', { clips: [{ source: 'category:idle', facing_bone: '' }] }, /facing_bone/);
  bad('retarget', { hand_floor: { l: 'low' } }, /hand_floor/);
  bad('retarget', { facing: 'side' }, /allowed values/);
  bad('retarget', { hips_drop: 1.5 }, /at most 1/);
});

test('separate: slit mode, caps, slit gap, region kinds, facing and curved cut surfaces', () => {
  const p = stepDef('separate').params;
  assert.deepEqual(p.mode.enum, ['loose', 'material', 'regions', 'slit']);
  moduleDefaults('separate', { caps: true, slit_gap_m: null, mode: 'loose', rest_name: 'body', origin: 'world' });
  const slits = [
    { name: 'sleeve_r', min: [-0.35, -0.2, 0.84], max: [-0.15, 0.2, 1.16], curve: { axis: 'x', along: 'z', points: [[0.845, -0.22], [1.0, -0.235], [1.15, -0.25]] }, width_m: 0.012 },
    { name: 'sleeve_l', min: [0.15, -0.2, 1.03], max: [0.35, 0.2, 1.14], plane: { axis: 'x', value: 0.22 }, gap_m: 0.002 },
    { min: [0.15, -0.2, 0.9], max: [0.35, 0.2, 1.0], plane: { point: [0.2, 0, 0.95], normal: [1, 0, 0.1] }, caps: false },
  ];
  assert.deepEqual(normalizeParams('separate', { mode: 'slit', regions: slits, caps: true, slit_gap_m: 0.002 }).warnings, []);
  const mixed = [
    { name: 'staff_old', kind: 'delete', min: [0.2, -0.3, 0], max: [0.5, 0.3, 2] },
    { name: 'hem', kind: 'delete', caps: false, facing: [0, 0, -1], facing_min: 0.5, min: [-0.5, -0.5, -0.01], max: [0.5, 0.5, 0.05] },
    { kind: 'slit', min: [-0.35, -0.2, 0.84], max: [-0.15, 0.2, 1.16] },
    { name: 'staff', min: [-0.1, -0.1, 0.2], max: [0.1, 0.1, 1.9] },
  ];
  assert.deepEqual(normalizeParams('separate', { mode: 'regions', regions: mixed }).warnings, []);
  const box = { min: [0, 0, 0], max: [1, 1, 1] };
  bad('separate', { mode: 'slit' }, /mode = slit/);
  bad('separate', { mode: 'regions', regions: [{ name: 'x', kind: 'cut', ...box }] }, /kind must be part/);
  bad('separate', { mode: 'regions', regions: [{ kind: 'part', ...box }] }, /no name/);
  bad('separate', { mode: 'slit', regions: [{ ...box, plane: { axis: 'w', value: 1 } }] }, /plane must be \{axis/);
  bad('separate', { mode: 'slit', regions: [{ ...box, plane: { point: [0, 0, 0], normal: [0, 0, 0] } }] }, /non-zero normal/);
  bad('separate', { mode: 'slit', regions: [{ ...box, plane: { side: 1 } }] }, /plane/);
  bad('separate', { mode: 'slit', regions: [{ ...box, curve: { axis: 'x', along: 'z', points: [[0.9, 0.2]] } }] }, /curve.points/);
  bad('separate', { mode: 'slit', regions: [{ ...box, curve: { axis: 'x', along: 'x', points: [[0, 0], [1, 1]] } }] }, /two different axes/);
  bad('separate', { mode: 'slit', regions: [{ ...box, curve: { points: [[0.9, 0.2], [0.9, 0.3]] } }] }, /must not repeat/);
  bad('separate', { mode: 'slit', regions: [{ ...box, width_m: -0.01 }] }, /width_m/);
  bad('separate', { mode: 'regions', regions: [{ name: 'x', ...box, plane: { axis: 'x', value: 0 } }] }, /only applies to a zone of kind slit/);
  bad('separate', { mode: 'regions', regions: [{ name: 'x', ...box, facing: [0, 0, 0] }] }, /facing/);
  bad('separate', { mode: 'regions', regions: [{ name: 'x', ...box, caps: 'no' }] }, /caps must be true\/false/);
  bad('separate', { slit_gap_m: 0.5 }, /at most 0.1/);
});

// ------------------------------------------------------------------ motion_style (11.10)

test('motion_style: registered after retarget, knobs with the ranges of _style_lib, per-clip overrides checked', () => {
  const ms = stepDef('motion_style');
  assert.equal(ms.module, 'mixamo');
  assert.deepEqual(ms.kinds, ['humanoid']);
  assert.deepEqual(ms.inputs.slots, ['blend'], 'the animated .blend of retarget');
  assert.deepEqual(ms.inputs.formats, ['blend']);
  assert.deepEqual(ms.outputs, ['glb', 'blend', 'clips', 'preview']);
  assert.equal(isGpuStep('motion_style'), false);
  // _style_lib.RANGES
  const ranges = { tempo: [0.8, 1.25], stride: [0.7, 1.35], arm_swing: [0.2, 2], lean: [-1, 1], hunch: [0, 1], head_bob: [0.3, 2],
    sway: [0.2, 2.5], limp: [0, 1], swagger: [0, 1], stiffness: [0.5, 1.8] };
  for (const [k, [lo, hi]] of Object.entries(ranges)) {
    assert.equal(ms.params[k].min, lo, `${k}.min`);
    assert.equal(ms.params[k].max, hi, `${k}.max`);
    assert.equal(ms.params[k].default, null, `${k}: empty = from the seed (or neutral)`);
  }
  // motion_style.py: P.get(key, default)
  moduleDefaults('motion_style', { death: 'library', death_blend: 1, undead: false, hold_props: true, floor: true,
    floor_tolerance_m: 0.02, floor_samples: 16, ground_samples: 2000, preview: true, preview_cols: 6, preview_tile: 220 });
  assert.deepEqual(ms.params.death.enum, ['library', 'auto', 'fall_back', 'fall_forward', 'fall_left', 'fall_right',
    'kneel_collapse', 'spin_fall', 'slump_sit', 'crumble']);
  const ok = normalizeParams('motion_style', { seed: 'Knight_holding_sword_and_shield_2.jpg', build: 'armored', death: 'spin_fall',
    tempo: 1.1, limp: 0.4, limp_side: 'r', clips: { walk: { stride: 1.2, limp_side: 'l' }, attack: false, idle: { stiffness: 1.5 } },
    apply: ['idle', 'walk'] });
  assert.deepEqual(ok.warnings, []);
  assert.equal(ok.explicit.seed, 'Knight_holding_sword_and_shield_2.jpg');
  assert.doesNotThrow(() => normalizeParams('motion_style', { seed: 17 }));
  bad('motion_style', { tempo: 1.5 }, /at most 1.25/);
  bad('motion_style', { death: 'explode' }, /allowed values/);
  bad('motion_style', { build: 'giant' }, /allowed values/);
  bad('motion_style', { apply: ['death'] }, /allowed values/);
  bad('motion_style', { clips: { walk: { stride: 2 } } }, /stride must be a number from 0.7 to 1.35/);
  bad('motion_style', { clips: { walk: { speed: 1 } } }, /unknown knob "speed"/);
  bad('motion_style', { clips: { walk: { limp_side: 'x' } } }, /limp_side must be l or r/);
  bad('motion_style', { clips: { walk: 1 } }, /expected \{knob: value\} or false/);
  bad('motion_style', { clips: [1] }, /clips/);
  // a humanoid chain with motion_style after retarget validates as a whole
  const chain = normalizeSteps([
    { step: 'cleanup', params: { target_tris: 40000 } }, { step: 'rig_humanoid', params: {} },
    { step: 'retarget', params: { clips: [{ source: 'category:walk', name: 'walk', loop: true, in_place: true }] } },
    { step: 'motion_style', params: { seed: 'x', death: 'auto' } }, { step: 'render_sprites', params: { profile: 'iso8' } }]);
  assert.deepEqual(chain.steps.map((s) => s.step), ['cleanup', 'rig_humanoid', 'retarget', 'motion_style', 'render_sprites']);
});

// ------------------------------------------------------------------ production QA, robe deaths (11.10)

test('clip QA and robe settle: registered with the module defaults and checked', () => {
  // rig_humanoid.py: params.get("deform_gate", "error"); motion_style.py: P.get(...)
  moduleDefaults('rig_humanoid', { deform_gate: 'error', deform_test: true });
  moduleDefaults('motion_style', { robe_settle: true, deform_qa: true, deform_every: 2, deform_gate: 'error', deform_limits: null });
  assert.deepEqual(stepDef('rig_humanoid').params.deform_gate.enum, ['error', 'warning']);
  assert.deepEqual(stepDef('motion_style').params.deform_gate.enum, ['error', 'warning']);
  const ok = normalizeParams('motion_style', { seed: 'x', deform_gate: 'warning', deform_every: 3, deform_limits: { main: 0.02, arms_up: 0.08, robe: 0.04 } });
  assert.deepEqual(ok.warnings, []);
  assert.equal(ok.explicit.deform_gate, 'warning');
  bad('motion_style', { deform_gate: 'fail' }, /allowed values/);
  bad('motion_style', { deform_every: 0 }, /at least 1/);
  bad('rig_humanoid', { deform_gate: 'maybe' }, /allowed values/);
  // a production chain with clip QA validates
  const chain = normalizeSteps([
    { step: 'cleanup', params: { target_tris: 40000, height_m: 1.8 } }, { step: 'rig_humanoid', params: { height_m: 1.8, deform_gate: 'warning' } },
    { step: 'retarget', params: { clips: [{ source: 'category:walk', name: 'walk', loop: true, in_place: true }] } },
    { step: 'motion_style', params: { seed: 'x', death: 'auto', deform_gate: 'error' } },
    { step: 'render_sprites', params: { profile: 'iso8', in_place: 'drift' } }]);
  assert.equal(chain.steps.length, 5);
});

test('paint_from_concept: registered with the params of paint_from_concept.py; concept slot, server-only concept params, camera checked', () => {
  const s = listSteps().find((x) => x.step === 'paint_from_concept');
  assert.ok(s, 'registered');
  assert.deepEqual(s.inputs.slots, ['blend', 'mesh', 'concept']);
  assert.equal(s.gpu, false, 'CPU only (a batch runs it between generations)');
  for (const k of ['glb', 'blend', 'basecolor', 'emission', 'compare', 'camera']) assert.ok(s.outputs.includes(k), k);
  const d = defaultParams('paint_from_concept');
  assert.equal(d.size, 2048);
  assert.equal(d.uv, 'auto');
  assert.equal(d.camera, 'fit');
  assert.equal(d.mirror, 'auto');
  assert.equal(d.back_fill, 'through');
  assert.equal(d.emission, 'auto');
  assert.equal(d.yaw_range_deg, null, 'empty: 60, creatures 180 (the step decides)');
  assert.deepEqual(pathParams('paint_from_concept'), ['concept_file', 'concept_back_file']);
  assert.deepEqual(blenderParams('paint_from_concept', { concept_asset: 'x', concept_file: 'y', concept_back_asset: 'z', concept_back_file: 'w', size: 1024 }), { size: 1024 });
  for (const k of ['camera_back', 'qa_sheet', 'qa']) assert.ok(s.outputs.includes(k), k);
  assert.equal(d.align, 'none', 'non-rigid alignment is opt-in');
  assert.equal(d.flat, false);
  assert.equal(d.qa_vlm, 'auto');
  const cam = { yaw_deg: -23.1, pitch_deg: -7.8, px_per_m: 387.2, offset_px: [506.6, 800.1] };
  assert.deepEqual(normalizeParams('paint_from_concept', { camera: cam }).explicit.camera, cam);
  assert.doesNotThrow(() => normalizeParams('paint_from_concept', { camera: 'fit', size: 1024, uv: 'smart', mirror: 'x', emission: 'none' }));
  assert.throws(() => normalizeParams('paint_from_concept', { camera: { yaw_deg: 5 } }), /px_per_m/);
  assert.throws(() => normalizeParams('paint_from_concept', { camera: { px_per_m: 300, offset_px: [1] } }), /offset_px/);
  assert.throws(() => normalizeParams('paint_from_concept', { facing_min: 0.5 }), /facing_full/);
  assert.throws(() => normalizeParams('paint_from_concept', { size: 3000 }), /allowed values/);
  assert.throws(() => normalizeParams('paint_from_concept', { mirror: 'y' }), /allowed values/);
});

// ------------------------------------------------------------------ assemble_parts (13.10)
test('assemble_parts: registered with the defaults of assemble_parts.py; inputs, server-only and path params, checks', () => {
  const s = stepDef('assemble_parts');
  assert.ok(s, 'registered');
  assert.equal(s.module, 'parts');
  assert.match(s.title, /Assembly from parts/);
  assert.deepEqual(s.kinds, ['humanoid']);
  assert.deepEqual(s.inputs.slots, ['blend', 'mesh', 'concept', 'parts']);
  assert.equal(s.gpu, false, 'CPU only (Blender)');
  for (const k of ['glb', 'blend', 'preview', 'parts_fit', 'glb_mv']) assert.ok(s.outputs.includes(k), k);
  // scripts/blender/steps/assemble_parts.py run(req): p.get(key, default)
  moduleDefaults('assemble_parts', { height_m: 1.8, voxel_m: null, target_tris: null, refine: 'silhouette', depth_scale: 'base',
    trim_overlap: true, joint_overlap_m: null, island_min: 0.002, box_expand: 0.012, icp_iters: 40, icp_trim: 0.8, icp_samples: 15000,
    icp_max_rot_deg: 6, icp_scale_range: [0.8, 1.25], icp_warn_m: 0.012, try_flip: true, flip_rms_m: 0, fill_gaps: true,
    gap_min_tris: 1500, camera: 'fit', shade_auto_smooth_deg: 40, export_mv: false, preview: true, head_closeup: true });
  assert.deepEqual(pathParams('assemble_parts'), ['parts_file', 'concept_file'], 'local paths: CLI only');
  assert.deepEqual(blenderParams('assemble_parts', { parts_file: 'a', concept_file: 'b', concept_asset: 'c', refine: 'icp' }), { refine: 'icp' },
    'the server turns them into inputs.parts / inputs.concept');
  assert.doesNotThrow(() => normalizeParams('assemble_parts', { voxel_m: 0.003, target_tris: 60000, depth_scale: 1.1, refine: 'none', pick: { head: 11, legs: 'C:/m/legs.glb' }, camera: { px_per_m: 608.9, offset_px: [684, 676], centre: [0, 0, 0.9] } }));
  bad('assemble_parts', { refine: 'maybe' }, /allowed values/);
  bad('assemble_parts', { depth_scale: 'thick' }, /depth_scale/);
  bad('assemble_parts', { camera: { offset_px: [1, 2] } }, /px_per_m/);
  bad('assemble_parts', { camera: { px_per_m: 600, offset_px: [1] } }, /offset_px/);
  bad('assemble_parts', { icp_scale_range: [1.3, 0.9] }, /icp_scale_range/);
  bad('assemble_parts', { pick: ['head'] }, /pick/);
  bad('assemble_parts', { pick: { head: 1.5 } }, /pick\.head/);
  bad('assemble_parts', { voxel_m: 0 }, /at least/);
});

// ------------------------------------------------------------------ extract_props (12.10)
test('extract_props: a humanoid step before the rig with the defaults of extract_props.py; hints / gate checked', () => {
  const ex = stepDef('extract_props');
  assert.equal(ex.module, 'items');
  assert.match(ex.title, /Props/);
  assert.deepEqual(ex.kinds, ['humanoid']);
  assert.deepEqual(ex.inputs.slots, ['blend', 'mesh'], 'the painted .blend of the chain first');
  assert.ok(ex.inputs.formats.includes('blend'));
  for (const k of ['glb', 'blend', 'props_json', 'items', 'preview', 'detect', 'grips']) assert.ok(ex.outputs.includes(k), k);
  assert.equal(isGpuStep('extract_props'), false);
  // extract_props.py run(req): P.get(key, default)
  moduleDefaults('extract_props', { height_m: 1.8, front: 'auto', front_prior: null, fix_yaw: true, use_hints: true, replace: 'auto',
    bridge: true, kit_style: 'auto', kit_ornament: 1, min_item_vertices: 60, preview: true, preview_size: 320 });
  const ok = normalizeParams('extract_props', { hints: { hand_weapon: 'banner + staff', hand: 'both', shield: true, notes: 'x' },
    gate: { open_edge_frac: 0.05, min_tris: 200 }, front_prior: '-Y', replace: 'always', seed: 'Knight_3.jpg' });
  assert.deepEqual(ok.explicit.gate, { open_edge_frac: 0.05, min_tris: 200 });
  bad('extract_props', { replace: 'maybe' }, /allowed values/);
  bad('extract_props', { hints: { hand: 'x' } }, /hints.hand/);
  bad('extract_props', { hints: { shield: 'yes' } }, /hints.shield/);
  bad('extract_props', { hints: 'sword' }, /hints/);
  bad('extract_props', { gate: { open_edges: 1 } }, /unknown threshold/);
  bad('extract_props', { gate: { min_tris: -1 } }, /at least 0/);
});

test('attach_prop extracted: server-side flag, extracted_mode checked, not together with attachments; rig hard surface', () => {
  const ap = stepDef('attach_prop');
  assert.equal(ap.params.extracted.server, true, 'the server resolves inputs.extracted, Blender never sees the flag');
  assert.equal('extracted' in blenderParams('attach_prop', normalizeParams('attach_prop', { extracted: true, extracted_mode: 'skin_separate' }).params), false);
  assert.equal(blenderParams('attach_prop', normalizeParams('attach_prop', { extracted: true, extracted_mode: 'skin_separate' }).params).extracted_mode, 'skin_separate');
  bad('attach_prop', { extracted_mode: 'glue' }, /extracted_mode/);
  bad('attach_prop', { extracted: true, attachments: [{ item: 'C:/a/item.glb' }] }, /one or the other/);
  assert.doesNotThrow(() => normalizeParams('attach_prop', { attachments: [{ item: 'C:/a/item.glb', mode: 'skin_separate' }] }));
  const rig = stepDef('rig_humanoid').params;
  assert.equal(rig.hard_surface.default, 'auto');
  assert.deepEqual(rig.hard_surface.enum, ['auto', 'off', 'on']);
  assert.equal(rig.use_placement_grips.default, true);
  assert.equal(normalizeParams('rig_humanoid', { build: 'armored', front_prior: '-Y' }).explicit.build, 'armored');
  bad('rig_humanoid', { hard_surface: 'plate' }, /allowed values/);
});
