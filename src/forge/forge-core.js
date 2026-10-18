// Pure helpers for the Forge wizard (src/forge). No DOM, no React, no network:
// everything here is covered by tests/forge-ui-core.test.mjs.
//
// Parameter names of Blender steps come from GET /api/forge/steps. The FALLBACK_PARAMS
// below are only used when the registry is unavailable or does not describe a parameter;
// the real names always win (see paramDefs / resolveParamName).
//
// User-visible labels come from the i18n dictionaries (src/i18n, keys core.*, step.*, param.*,
// enum.*): module-level tables expose them through getters, so a language switch applies on
// the next read.
import { t, tOr, getLang } from '../i18n/index.js';

/** Object with one getter per key: obj[key] -> t(prefix + key) at read time. */
function labelTable(keys, prefix) {
  const out = {};
  for (const key of keys) Object.defineProperty(out, key, { get: () => t(prefix + key), enumerable: true });
  return out;
}

/** Adds getters for the given fields that read `${prefix}.${field}`. */
function withLabels(obj, prefix, fields = ['title', 'hint']) {
  for (const f of fields) Object.defineProperty(obj, f, { get: () => t(`${prefix}.${f}`), enumerable: true });
  return obj;
}

// --------------------------------------------------------------------------- kinds & sources

export const KINDS = [
  { id: 'humanoid', apiKind: 'humanoid', height: 1.8 },
  { id: 'creature', apiKind: 'creature', height: null },
  { id: 'item', apiKind: 'item', height: null },
  { id: 'ship', apiKind: 'ship', height: null },
  { id: 'turret', apiKind: 'prop', height: null },
].map((k) => withLabels(k, `core.kind.${k.id}`, ['title', 'hint', 'defaultName']));

export function kindInfo(kind) {
  return KINDS.find((k) => k.id === kind) || null;
}

/** Wizard kind for a passport: prop passports made by turret_gen are shown as turrets. */
export function kindFromAsset(asset) {
  if (!asset) return null;
  if (asset.kind === 'prop') return 'turret';
  return KINDS.some((k) => k.id === asset.kind) ? asset.kind : null;
}

export const SOURCES = ['scene', 'job', 'upload', 'params'].map((id) => withLabels({ id }, `core.source.${id}`));

const KIND_SOURCES = {
  humanoid: ['scene', 'job', 'upload'],
  creature: ['scene', 'job', 'upload'],
  item: ['params', 'scene', 'job', 'upload'],
  ship: ['params', 'scene', 'job', 'upload'],
  turret: ['params'],
};

export function sourcesFor(kind) {
  const ids = KIND_SOURCES[kind] || [];
  return ids.map((id) => SOURCES.find((s) => s.id === id));
}

/** Source type stored in the passport -> wizard source id. */
export function sourceFromAsset(asset) {
  const type = asset?.source?.type;
  if (type === 'params') return 'params';
  if (type === 'job') return 'job';
  if (type === 'upload') return 'upload';
  if (type === 'asset') return 'scene';
  return null;
}

// --------------------------------------------------------------------------- wizard steps

export const WIZARD_STEPS = ['what', 'prepare', 'skeleton', 'motion', 'items', 'fx', 'export'].map((id) =>
  Object.defineProperty({ id }, 'title', { get: () => t(`core.wizard.${id}`), enumerable: true }));

const KIND_WIZARD = {
  humanoid: ['what', 'prepare', 'skeleton', 'motion', 'items', 'fx', 'export'],
  creature: ['what', 'prepare', 'skeleton', 'motion', 'fx', 'export'],
  item: ['what', 'prepare', 'fx', 'export'],
  ship: ['what', 'prepare', 'fx', 'export'],
  turret: ['what', 'prepare', 'fx', 'export'],
};

/** Visible wizard steps for a kind; generators rename step 2 to "Generator". */
export function wizardStepsFor(kind, source) {
  const ids = KIND_WIZARD[kind] || ['what'];
  return ids.map((id) => {
    const step = WIZARD_STEPS.find((s) => s.id === id);
    if (id === 'prepare' && source === 'params') return { ...step, title: t('core.wizard.generator') };
    return { ...step };
  });
}

const GENERATOR_STEP = { ship: 'ship_gen', turret: 'turret_gen', item: 'item_kit' };
const FROM_MESH_STEP = { ship: 'ship_from_mesh', item: 'item_from_mesh' };

export function generatorStep(kind) {
  return GENERATOR_STEP[kind] || null;
}

/** Blender steps run by wizard step 2 for this kind/source. */
export function prepareSteps(kind, source) {
  if (source === 'params') return GENERATOR_STEP[kind] ? [GENERATOR_STEP[kind]] : [];
  const steps = ['cleanup'];
  if (FROM_MESH_STEP[kind]) steps.push(FROM_MESH_STEP[kind]);
  return steps;
}

/** Default steps for a batch item when no preset is chosen. */
export function defaultBatchSteps(kind, source) {
  if (source === 'params') return prepareSteps(kind, 'params');
  if (kind === 'humanoid') return ['cleanup', 'rig_humanoid'];
  if (kind === 'creature') return ['cleanup', 'rig_creature'];
  return prepareSteps(kind, source);
}

const WIZARD_PIPELINE = {
  prepare: ['cleanup', 'convert', 'separate', 'ship_gen', 'turret_gen', 'item_kit', 'item_from_mesh', 'ship_from_mesh'],
  skeleton: ['rig_humanoid', 'rig_creature', 'import_mixamo_rig'],
  motion: ['retarget', 'anim_creature'],
  items: ['attach_prop'],
};

export function pipelineStepsOf(wizardStepId) {
  return WIZARD_PIPELINE[wizardStepId] || [];
}

const WIZARD_ORDER = ['prepare', 'skeleton', 'motion', 'items'];

/** Steps that change the model. Exports, inspect and prepare_mixamo keep the model as it is. */
export const MODEL_STEPS = Object.values(WIZARD_PIPELINE).flat();

function versionRecord(asset, name) {
  const list = Array.isArray(asset?.versions) ? asset.versions : [];
  return list.find((v) => v && v.version === name) || null;
}

const stepCall = (e) => ({ step: e.step, params: cloneValue(e.params || {}) });

/**
 * How to run a wizard step so that it continues from the state right after the earlier wizard
 * steps and never lands on top of its own previous result: {from, prefix, replayOf}.
 *
 * `from: 'vNNN'` means the FINAL state of that version (server _resolveBase). Presets and
 * batches put several wizard steps into one version (cleanup → rig → retarget), and a version
 * may have failed after its first steps. So:
 *  - step 2 (Preparation) always starts from the source;
 *  - E = the latest done entry of the nearest earlier wizard group;
 *  - if E's version V is completed and E is its last model-changing step, from = V;
 *  - otherwise the own steps of V up to E are replayed with their recorded params, starting
 *    from V's own base (V.base, or the source), and the caller appends its steps after them;
 *  - nothing done before this wizard step: from the source.
 */
export function runPlan(asset, wizardStepId) {
  const idx = WIZARD_ORDER.indexOf(wizardStepId);
  if (idx <= 0) return { from: 'source', prefix: [], replayOf: null };
  const pipeline = Array.isArray(asset?.pipeline) ? asset.pipeline : [];
  for (let i = idx - 1; i >= 0; i--) {
    const entry = latestPipelineEntry(asset, WIZARD_PIPELINE[WIZARD_ORDER[i]], { status: 'done' });
    if (!entry?.version) continue;
    const own = pipeline.filter((e) => e && e.version === entry.version);
    const pos = own.indexOf(entry);
    const v = versionRecord(asset, entry.version);
    const stepIndex = Number.isInteger(entry.index) && entry.index > 0 ? entry.index : pos + 1;
    const after = Array.isArray(v?.steps) ? v.steps.slice(stepIndex) : own.slice(pos + 1).map((e) => e.step);
    const changedAfter = after.some((s) => MODEL_STEPS.includes(typeof s === 'string' ? s : s?.step));
    const completed = !v || v.status === 'completed';
    if (completed && !changedAfter) return { from: entry.version, prefix: [], replayOf: null };
    if (v) {
      const prefix = own.slice(0, pos + 1).filter((e) => MODEL_STEPS.includes(e.step)).map(stepCall);
      return { from: v.base || 'source', prefix, replayOf: entry.version };
    }
    // No version record (old passport): replay the recorded chain up to E from the source.
    const chain = pipeline.slice(0, pipeline.indexOf(entry) + 1).filter((e) => normalizeStatus(e.status) === 'done' && MODEL_STEPS.includes(e.step));
    return { from: 'source', prefix: chain.map(stepCall), replayOf: entry.version };
  }
  return { from: 'source', prefix: [], replayOf: null };
}

/** runPlan + the steps of the wizard step: {from, steps, replayed:[step names]} for POST /run. */
export function planSteps(asset, wizardStepId, steps) {
  const plan = runPlan(asset, wizardStepId);
  return { from: plan.from, steps: [...plan.prefix, ...steps], replayed: plan.prefix.map((s) => s.step) };
}

/** " (replaying first: Cleanup and decimation)" for a plan that replays earlier steps. */
export function replayNote(plan, registry) {
  if (!plan?.replayed?.length) return '';
  return t('core.replayNote', { steps: plan.replayed.map((s) => stepTitle(s, registry)).join(', ') });
}

const HEIGHT_KEYS = ['height_m', 'height', 'target_height', 'height_meters'];

function heightOf(params, preferred) {
  for (const key of [preferred, ...HEIGHT_KEYS]) {
    const n = Number(params?.[key]);
    if (key && params && params[key] !== null && params[key] !== '' && Number.isFinite(n) && n > 0) return n;
  }
  return null;
}

/**
 * Starting values of the humanoid rig form. The params of the previous rig run are kept
 * (fingers, robe, …) except the markers, which only the marker editor sends. The height
 * follows step 2: the cleanup height wins when the cleanup ran after that rig or the rig had
 * none; the passport profile height is the last resort. rig_humanoid rescales the body to
 * height_m itself, so without it a 1.2 m dwarf would be blown up to the default 1.8 m.
 */
export function rigSeedValues(asset, { heightName = 'height_m', markersName = 'markers' } = {}) {
  const pipeline = Array.isArray(asset?.pipeline) ? asset.pipeline : [];
  const rig = latestPipelineEntry(asset, ['rig_humanoid']);
  const clean = latestPipelineEntry(asset, ['cleanup'], { status: 'done' });
  const out = rig?.params && typeof rig.params === 'object' ? cloneValue(rig.params) : {};
  delete out[markersName];
  const rigH = heightOf(out, heightName);
  const cleanH = heightOf(clean?.params, heightName);
  const profileH = heightOf(asset?.profile, 'height_m');
  const cleanupNewer = !!clean && (!rig || pipeline.lastIndexOf(clean) > pipeline.lastIndexOf(rig));
  if (cleanH && (cleanupNewer || !rigH)) out[heightName] = cleanH;
  else if (!rigH && profileH) out[heightName] = profileH;
  return out;
}

/**
 * markers.json is written in the frame of its own height_m (rig_humanoid divides params.markers
 * by params.height_m). Re-scale the dots when the rebuild uses another height, so they keep
 * their place on the body.
 */
export function markersForHeight(markers, fromHeight, toHeight) {
  const a = Number(fromHeight);
  const b = Number(toHeight);
  if (!(a > 0) || !(b > 0) || Math.abs(a - b) < 1e-9) return { ...(markers || {}) };
  const k = b / a;
  const out = {};
  for (const [name, [x, z]] of Object.entries(markers || {})) out[name] = [x * k, z * k];
  return out;
}

/**
 * Starting values of a generator form: the params of the latest generator run (the server
 * already merged the passport source params into them), else the passport source params.
 */
export function generatorSeedValues(asset, step) {
  const entry = latestPipelineEntry(asset, [step]);
  if (entry?.params && typeof entry.params === 'object') return cloneValue(entry.params);
  return { ...(asset?.source?.params || {}) };
}

const SKELETON_STEPS = WIZARD_PIPELINE.skeleton;
const CLIP_STEPS = WIZARD_PIPELINE.motion;
const versionNumber = (v) => Number(String(v?.version ?? v ?? '').replace(/^v/i, '')) || 0;

/**
 * What POST /export will ship (the server exports from 'latest'): the latest completed version
 * and the model steps of its recipe, plus what is missing for a rigged kind
 * [{what:'skeleton'|'clips', older:'vNNN'|null}] (older = a completed version that had it).
 */
export function exportPlan(asset, kind) {
  const versions = (Array.isArray(asset?.versions) ? asset.versions : []).filter(Boolean);
  const completed = versions.filter((v) => v.status === 'completed').sort((a, b) => versionNumber(b) - versionNumber(a));
  const done = new Set(completed.map((v) => v.version));
  const recipe = (Array.isArray(asset?.pipeline) ? asset.pipeline : [])
    .filter((e) => e && normalizeStatus(e.status) === 'done' && (versions.length ? done.has(e.version) : true));
  const steps = recipe.map((e) => e.step).filter((s) => MODEL_STEPS.includes(s));
  const missing = [];
  if (kind === 'humanoid' || kind === 'creature') {
    for (const [what, group] of [['skeleton', SKELETON_STEPS], ['clips', CLIP_STEPS]]) {
      if (steps.some((s) => group.includes(s))) continue;
      const older = completed.find((v) => (v.steps || []).some((s) => group.includes(typeof s === 'string' ? s : s?.step)));
      missing.push({ what, older: older?.version || null });
    }
  }
  return { version: completed[0]?.version || null, steps, missing };
}

/** Map of wizard step id -> 'done' | 'failed' | 'pending' | null, from the passport. */
export function wizardProgress(asset) {
  const out = {};
  for (const step of WIZARD_STEPS) out[step.id] = null;
  if (!asset) return out;
  out.what = 'done';
  for (const [wizardId, steps] of Object.entries(WIZARD_PIPELINE)) {
    const entry = latestPipelineEntry(asset, steps);
    if (entry) out[wizardId] = normalizeStatus(entry.status);
  }
  if (Array.isArray(asset.fx) && asset.fx.length) out.fx = 'done';
  if (Array.isArray(asset.exports) && asset.exports.length) out.export = 'done';
  return out;
}

function normalizeStatus(status) {
  if (status === 'done' || status === 'completed' || status === 'ok') return 'done';
  if (status === 'failed' || status === 'error') return 'failed';
  return 'pending';
}

// --------------------------------------------------------------------------- step titles

const STEP_IDS = ['inspect', 'cleanup', 'convert', 'separate', 'rig_humanoid', 'prepare_mixamo', 'import_mixamo_rig', 'retarget',
  'clip_library', 'rig_creature', 'anim_creature', 'item_kit', 'item_from_mesh', 'attach_prop', 'ship_gen', 'turret_gen',
  'ship_from_mesh', 'render_sprites', 'godot_package'];

/** Client step titles (dictionary keys step.<id>); the dictionary also covers the other registry steps. */
export const STEP_TITLES = labelTable(STEP_IDS, 'step.');

/** Step title: client dictionary step.<id>, else the registry title (server), else the id. */
export function stepTitle(step, registry) {
  const own = tOr(`step.${step}`, null);
  if (own) return own;
  const def = findStepDef(registry, step);
  return def?.title || step;
}

// --------------------------------------------------------------------------- registry & params

/**
 * Fallback parameter descriptions, shown only while GET /steps is unavailable. They mirror
 * the featured parameters of server/forge-steps.mjs (10.10); the live registry always wins.
 * title / help are getters over the param.<step>.<name>[.help] dictionary keys.
 */
function withParamLabels(table) {
  for (const [step, params] of Object.entries(table)) {
    for (const [name, def] of Object.entries(params)) {
      Object.defineProperty(def, 'title', { get: () => paramLabel(step, name), enumerable: true });
      Object.defineProperty(def, 'help', { get: () => paramHint(step, name), enumerable: true });
    }
  }
  return table;
}

/** Param title: dictionary param.<step>.<name>, else the registry title (server), else the name. */
export function paramLabel(step, name, def) {
  return tOr(`param.${step}.${name}`, def?.title || name);
}

/** Param help: dictionary param.<step>.<name>.help, else the registry help (server), else ''. */
export function paramHint(step, name, def) {
  return tOr(`param.${step}.${name}.help`, def?.help || '');
}
export const FALLBACK_PARAMS = withParamLabels({
  cleanup: {
    target_tris: { type: 'integer', default: null, nullable: true, min: 500, max: 2000000 },
    height_m: { type: 'number', default: null, nullable: true, min: 0.01, max: 1000 },
  },
  rig_humanoid: {
    height_m: { type: 'number', default: 1.8, min: 0.3, max: 5 },
    markers: { type: 'object', default: null, nullable: true },
    fingers: { type: 'boolean', default: false },
  },
  rig_creature: {
    template: { type: 'any', enum: ['auto', 'quadruped', 'winged_quadruped', 'bird', 'hexapod', 'octopod', 'serpent', 'custom'], default: 'auto' },
  },
  anim_creature: {
    clips: { type: 'array', default: ['idle-loop', 'walk-loop', 'run-loop', 'attack', 'hit', 'death'], items: { type: 'string', enum: ['idle-loop', 'walk-loop', 'run-loop', 'fly-loop', 'glide-loop', 'attack', 'hit', 'death'] } },
  },
  retarget: {
    clips: { type: 'array', default: [] },
  },
  ship_gen: {
    class: { type: 'any', enum: ['fighter', 'interceptor', 'gunship', 'corvette', 'freighter', 'miner', 'frigate', 'carrier', 'transport'], default: 'fighter' },
    seed: { type: 'integer', default: 1, min: 0, max: 2147483647 },
    length_m: { type: 'number', default: null, nullable: true, min: 2, max: 2000 },
    style: { type: 'any', enum: ['industrial', 'military', 'sleek', 'alien'], default: 'military' },
    turret_count: { type: 'integer', default: null, nullable: true, min: 0, max: 32 },
    engine_count: { type: 'integer', default: null, nullable: true, min: 1, max: 16 },
    greeble_density: { type: 'number', default: 0.5, min: 0, max: 1 },
  },
  turret_gen: {
    size: { type: 'any', enum: ['S', 'M', 'L'], default: 'M' },
    barrels: { type: 'integer', default: 2, min: 1, max: 4 },
    seed: { type: 'integer', default: 1, min: 0, max: 2147483647 },
    style: { type: 'any', enum: ['industrial', 'military', 'sleek', 'alien'], default: 'military' },
  },
  item_kit: {
    type: { type: 'any', enum: ['sword', 'greatsword', 'dagger', 'axe', 'battle_axe', 'mace', 'warhammer', 'spear', 'halberd', 'staff', 'wand', 'bow', 'crossbow', 'shield_round', 'shield_kite', 'shield_tower', 'buckler', 'torch', 'potion', 'book', 'blaster_pistol', 'blaster_rifle'], default: 'sword' },
    style: { type: 'any', enum: ['medieval', 'dark', 'elven', 'scifi'], default: 'medieval' },
    seed: { type: 'integer', default: 1, min: 0, max: 2147483647 },
    ornament: { type: 'integer', default: 1, min: 0, max: 3 },
  },
  attach_prop: {
    hand: { type: 'any', enum: ['r', 'l'], default: 'r' },
    mode: { type: 'any', enum: ['bone_child', 'skin_rigid'], default: 'bone_child' },
  },
});

/** Alternative names the featured controls look for in the registry. */
export const PARAM_ALIASES = {
  triangles: ['target_tris', 'max_tris', 'tris', 'triangles', 'triangle_budget', 'budget', 'target_faces'],
  height: ['height_m', 'height', 'target_height', 'height_meters'],
  markers: ['markers'],
  template: ['template', 'creature_template', 'skeleton'],
  clips: ['clips', 'clip_ids'],
  hand: ['hand', 'side'],
  mode: ['mode', 'attach_mode'],
  propAsset: ['prop_asset', 'item_asset'],
  seed: ['seed'],
  fingers: ['fingers', 'with_fingers'],
  itemType: ['type', 'item_type', 'kind', 'weapon'],
  style: ['style'],
  ornament: ['ornament', 'ornaments', 'ornament_level'],
  shipClass: ['class', 'ship_class', 'hull_class'],
  length: ['length_m', 'length', 'size_m'],
  engines: ['engine_count', 'engines'],
  turrets: ['turret_count', 'turrets'],
  greebles: ['greeble_density', 'greebles', 'greeble', 'detail'],
};

export function findStepDef(registry, step) {
  const list = Array.isArray(registry) ? registry : Array.isArray(registry?.steps) ? registry.steps : [];
  return list.find((s) => s && s.step === step) || null;
}

/**
 * Parameter descriptions for a step. A step known to the registry is described only by the
 * registry (so no guessed names are ever sent to Blender); otherwise the fallback guesses.
 */
export function paramDefs(registry, step) {
  const def = findStepDef(registry, step);
  if (def) {
    const out = {};
    for (const [name, p] of Object.entries(def.params || {})) out[name] = { ...(p || {}) };
    return out;
  }
  const out = {};
  for (const [name, p] of Object.entries(FALLBACK_PARAMS[step] || {})) out[name] = { ...p, fallback: true };
  return out;
}

/** First alias that the step actually declares; otherwise the first alias. */
export function resolveParamName(defs, aliases) {
  const list = Array.isArray(aliases) ? aliases : PARAM_ALIASES[aliases] || [aliases];
  for (const name of list) if (defs && Object.prototype.hasOwnProperty.call(defs, name)) return name;
  return list[0];
}

export function defaultsFor(defs) {
  const out = {};
  for (const [name, def] of Object.entries(defs || {})) {
    if (def && def.default !== undefined) out[name] = cloneValue(def.default);
  }
  return out;
}

function cloneValue(v) {
  if (Array.isArray(v)) return v.map(cloneValue);
  if (v && typeof v === 'object') return JSON.parse(JSON.stringify(v));
  return v;
}

function paramType(def) {
  if (!def) return 'any';
  if (Array.isArray(def.enum) && def.enum.length) return 'enum';
  const t = String(def.type || '').toLowerCase();
  if (t === 'int' || t === 'integer') return 'integer';
  if (t === 'float' || t === 'number' || t === 'double') return 'number';
  if (t === 'bool' || t === 'boolean') return 'boolean';
  if (t === 'array' || t === 'list') return 'array';
  if (t === 'object' || t === 'dict') return 'object';
  if (t === 'string' || t === 'str' || t === 'text') return 'string';
  if (typeof def.default === 'number') return Number.isInteger(def.default) ? 'integer' : 'number';
  if (typeof def.default === 'boolean') return 'boolean';
  return 'string';
}

export { paramType };

/** Normalise a raw form value to the declared type (clamped to min/max, enum-checked). */
export function coerceParam(def, raw) {
  const type = paramType(def);
  if (raw === undefined || raw === null || raw === '') return def?.default === undefined ? undefined : cloneValue(def.default);
  if (type === 'integer' || type === 'number') {
    let n = Number(raw);
    if (!Number.isFinite(n)) return def?.default;
    if (type === 'integer') n = Math.round(n);
    if (Number.isFinite(def?.min)) n = Math.max(def.min, n);
    if (Number.isFinite(def?.max)) n = Math.min(def.max, n);
    return n;
  }
  if (type === 'boolean') return raw === true || raw === 'true' || raw === 1 || raw === '1' || raw === 'on';
  if (type === 'enum') return def.enum.map(String).includes(String(raw)) ? def.enum.find((e) => String(e) === String(raw)) : def.default;
  if (type === 'array') return Array.isArray(raw) ? raw : String(raw).split(',').map((s) => s.trim()).filter(Boolean);
  return raw;
}

const isNullable = (def) => !!def && (def.nullable === true || def.default === null);

/**
 * Value of a number field while the user types: not clamped (clamping on every keystroke
 * turned "1", "5" into 25 for a field with min 2), comma accepted as the decimal mark, an
 * empty field clears the value (null for nullable params, so "by class" is sent explicitly).
 * {value, valid}: invalid drafts such as "-" or "1e" keep the previous value.
 */
export function draftNumber(def, raw) {
  const text = String(raw ?? '').trim().replace(',', '.');
  if (text === '') return { value: isNullable(def) ? null : undefined, valid: true };
  const n = Number(text);
  return Number.isFinite(n) ? { value: n, valid: true } : { value: undefined, valid: false };
}

/** Slider step rounded to 1/2/5 × 10^n (0.047 → 0.05), so round values such as 1.2 stay reachable. */
export function niceStep(raw) {
  const x = Math.abs(Number(raw));
  if (!Number.isFinite(x) || x === 0) return 0.01;
  const p = 10 ** Math.floor(Math.log10(x));
  const m = x / p;
  const nice = m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10;
  return Number((nice * p).toPrecision(6));
}

/** Value of a number field when it loses focus: rounded and clamped to the declared range. */
export function commitNumber(def, raw) {
  const draft = draftNumber(def, raw);
  if (!draft.valid || draft.value === null || draft.value === undefined) return draft.value;
  return coerceParam(def, draft.value);
}

/**
 * Params to send: only what the user set, coerced to the declared types. Defaults are NOT
 * written out (server/forge-steps.mjs: "only explicitly given params are sent to Blender",
 * `default` documents the module's own default). When the step declares parameters,
 * undeclared names in `values` are dropped, so stale form state never reaches Blender;
 * `extra` is always sent (contract-level params such as markers, clips, hand).
 */
export function buildParams(defs, values = {}, extra = {}) {
  const declared = defs && Object.keys(defs).length > 0;
  const out = {};
  for (const [name, value] of Object.entries(values || {})) {
    if (value === undefined) continue;
    if (declared && !defs[name]) continue;
    out[name] = defs && defs[name] ? coerceParam(defs[name], value) : value;
  }
  for (const [name, value] of Object.entries(extra || {})) if (value !== undefined) out[name] = value;
  for (const key of Object.keys(out)) if (out[key] === undefined) delete out[key];
  return out;
}

/**
 * Generators (ship_gen, turret_gen, item_kit) get their scalar defaults written out, so the
 * passport recipe reproduces the same asset even if a module default changes later. Nullable
 * params left empty ("by class") are sent as an explicit null: on from:'source' the server
 * merges the passport source params under the step params, and without the null an old value
 * from the source would silently come back. The server drops the nulls before Blender.
 */
export function generatorParams(defs, values = {}) {
  const base = {};
  for (const [name, def] of Object.entries(defs || {})) {
    if (!def || def.server || def.auto) continue;
    const v = def.default;
    if (isNullable(def) && paramType(def) !== 'object' && paramType(def) !== 'array') base[name] = null;
    else if (v !== null && v !== undefined && typeof v !== 'object') base[name] = v;
  }
  return buildParams(defs, { ...base, ...values });
}

/** Values to show in a form: declared defaults overlaid with what will be sent. */
export function shownParams(defs, values = {}) {
  return { ...defaultsFor(defs), ...buildParams(defs, values) };
}

// --------------------------------------------------------------------------- labels for enums

const ENUM_KEYS = [
  'auto', 'quadruped', 'winged_quadruped', 'bird', 'hexapod', 'octopod', 'serpent', 'custom', 'fighter',
  'interceptor', 'bomber', 'corvette', 'frigate', 'destroyer', 'cruiser', 'freighter', 'shuttle', 'station',
  'military', 'civil', 'industrial', 'pirate', 'alien', 'plain', 'barbarian', 'knight', 'elven', 'orcish', 'ornate',
  'sword', 'axe', 'mace', 'hammer', 'spear', 'dagger', 'staff', 'bow', 'shield', 'greatsword', 'halberd', 'wand',
  'gunship', 'miner', 'carrier', 'transport', 'sleek', 'medieval', 'dark', 'scifi', 'battle_axe', 'warhammer',
  'crossbow', 'shield_round', 'shield_kite', 'shield_tower', 'buckler', 'torch', 'potion', 'book', 'blaster_pistol',
  'blaster_rifle', 'r', 'l', 'bone_child', 'skin_rigid', 'parent', 'merge', 'S', 'M', 'L', 'loose', 'material',
  'regions', 'slit', 'none', 'delta', 'swept', 'forward', 'straight', 'x', 'stub', 'crescent', 'thrust', 'exhaust',
  'smart', 'box', 'rotary', 'common', 'fit', 'skeleton_hand', 'bone_staff', 'bone_toggle', 'clean',
  'aged', 'wrist', 'ground', 'open', 'relaxed', 'claw', 'fist', 'grip', 'rings', 'caps', 'icp', 'footprint',
  'center', 'hit', 'rest', 'feet', 'mesh', 'world', 'bottom', 'head', 'ball', 'ankle', 'frame', 'clips', 'lock',
  'keep', 'extract', 'unit', 'hero', 'random', 'bbox', 'axis', 'ship_s', 'ship_m', 'ship_l', 'front', 'back', 'left',
  'right', 'top', 'three_quarter', 'godot', 'iso8', 'true', 'false', 'null', 'CPU', 'GPU', 'glb', 'fbx', 'obj',
  'aabb', 'concave', 'through', 'warp', 'on', 'off', 'silhouette', 'base', 'part', 'never', 'always', 'error',
  'warning', 'heavy', 'armored', 'robed', 'light', 'skeleton', 'zombie', 'trunk', 'image', 'idle', 'walk', 'run',
  'attack', 'cast', 'other', 'library', 'fall_back', 'fall_forward', 'fall_left', 'fall_right', 'kneel_collapse',
  'spin_fall', 'slump_sit', 'crumble', 'scan', 'import', 'remove', 'preview', 'list', 'heat', 'distance',
  'idle-loop', 'walk-loop', 'run-loop', 'fly-loop', 'glide-loop', 'death', 'trot', 'gallop', 'hop', 'bite', 'lunge',
  'strike', 'blade', 'blunt', 'polearm', 'min', 'max', 'thumb', 'index', 'middle', 'ring', 'pinky', 'classify',
  'keep_texture', 'single', 'paint', 'input', 'faction', 'flats', 'protrusions', 'glow', 'geometry', 'procedural',
  'geometry_nodes', 'hunyuan3d', 'trellis', 'instantmesh', 'triposr', 'manual', 'mixed', 'external', 'drift',
  'first_frame', 'EEVEE', 'CYCLES', 'WORKBENCH', 'ship', 'turret', 'module', 'drone', 'bank', 'hull_module',
  'station_core', 'station_module', 'structure_kit', 'drone_kit', 'asteroid_kit', 'debris_kit',
];

/** Labels of the enum values used by the registry steps (dictionary keys enum.<value>). */
export const ENUM_LABELS = labelTable(ENUM_KEYS, 'enum.');

/** Dictionary label of an enum value; unknown values (numbers, axes) are shown as they are. */
export function enumLabel(value) {
  return tOr(`enum.${String(value)}`, String(value));
}

// --------------------------------------------------------------------------- markers

export const MARKER_LABELS = labelTable(['chin', 'groin', 'neck', 'head', 'head_top', 'chest', 'hips', 'wrist_l', 'wrist_r',
  'elbow_l', 'elbow_r', 'shoulder_l', 'shoulder_r', 'knee_l', 'knee_r', 'ankle_l', 'ankle_r', 'hip_l', 'hip_r', 'foot_l', 'foot_r'], 'core.marker.');

/** "LeftWrist", "wrist.L", "Wrist_Left", "l_wrist" -> "wrist_l". */
export function canonicalMarkerName(name) {
  let s = String(name || '').trim();
  let side = '';
  const prefix = s.match(/^(left|right)[_\s.-]?/i) || s.match(/^([lr])[_.-]/i);
  if (prefix) {
    side = prefix[1].toLowerCase()[0];
    s = s.slice(prefix[0].length);
  }
  const suffix = s.match(/[_\s.-](left|right|l|r)$/i);
  if (!side && suffix) {
    side = suffix[1].toLowerCase()[0];
    s = s.slice(0, -suffix[0].length);
  }
  s = s.replace(/([a-z])([A-Z])/g, '$1_$2').replace(/[\s.-]+/g, '_').toLowerCase();
  return side ? s + '_' + side : s;
}

export function markerLabel(name) {
  const key = canonicalMarkerName(name);
  return (Object.prototype.hasOwnProperty.call(MARKER_LABELS, key) && MARKER_LABELS[key]) || String(name);
}

/** {markers:{name:[x,z] | [x,y,z] | {x,z}}} -> {name:[x,z]} (metres, Blender X right / Z up). */
export function normalizeMarkers(json) {
  const src = json?.markers && typeof json.markers === 'object' ? json.markers : {};
  const out = {};
  for (const [name, value] of Object.entries(src)) {
    let x;
    let z;
    if (Array.isArray(value)) {
      if (value.length >= 3) [x, , z] = value;
      else [x, z] = value;
    } else if (value && typeof value === 'object') {
      x = value.x;
      z = value.z ?? value.y;
    }
    x = Number(x);
    z = Number(z);
    if (Number.isFinite(x) && Number.isFinite(z)) out[name] = [x, z];
  }
  return out;
}

function vec(v) {
  if (Array.isArray(v)) return v.map(Number);
  if (v && typeof v === 'object') return [Number(v.x), Number(v.y), Number(v.z)].filter((n) => Number.isFinite(n));
  return null;
}

function bboxFrom(json) {
  const candidates = [
    json?.bbox && [json.bbox.min, json.bbox.max],
    json?.bbox && Array.isArray(json.bbox) && json.bbox.length === 2 && [json.bbox[0], json.bbox[1]],
    [json?.bbox_min, json?.bbox_max],
    json?.stats && [json.stats.bbox_min, json.stats.bbox_max],
  ];
  for (const pair of candidates) {
    if (!pair || !pair[0] || !pair[1]) continue;
    const lo = vec(pair[0]);
    const hi = vec(pair[1]);
    if (lo && hi && lo.length === hi.length && lo.length >= 2 && lo.every(Number.isFinite) && hi.every(Number.isFinite)) return { lo, hi };
  }
  return null;
}

/** Width of the front tile of a markers sheet: square tiles side by side, front view first. */
export function frontTileWidth(width, height) {
  return width > height * 1.5 ? height : Math.min(width, height);
}

/**
 * Projection of the front view used by markers.png, in pixels of the real image.
 * Preferred: markers.json carries {image:{width,height}, center:[x,z], meters_per_px} either at
 * the top level or under "projection" (optional center_px:[u,v]). Agreed meaning: image = the
 * whole PNG, center = metres at the centre of the front view, center_px = that point in the
 * PNG. Without center_px the centre of the front tile is used (NOT the middle of a two-tile
 * sheet). When the block describes another size than the loaded PNG (a downscaled sheet, or
 * only the front tile), its pixels are scaled by the height ratio: tiles sit side by side, so
 * both readings map by height.
 * Fallback: bbox from markers.json + the natural image size, reproducing af_common.render_views
 * (square orthographic tiles, ortho_scale = |bbox diagonal| * margin, front view is the first tile).
 */
export function markerProjection(json, imageSize, { margin = 1.08 } = {}) {
  const block = json?.projection && typeof json.projection === 'object' ? json.projection : json || {};
  const mpp = Number(block.meters_per_px ?? block.metres_per_px ?? block.mpp);
  const center = vec(block.center);
  const natW = Number(imageSize?.width);
  const natH = Number(imageSize?.height);
  const hasNatural = natW > 0 && natH > 0;
  const blockW = Number(block.image?.width) || (hasNatural ? natW : NaN);
  const blockH = Number(block.image?.height) || (hasNatural ? natH : NaN);
  if (Number.isFinite(mpp) && mpp > 0 && center && center.length >= 2 && blockW > 0 && blockH > 0) {
    const width = hasNatural ? natW : blockW;
    const height = hasNatural ? natH : blockH;
    const k = height / blockH;
    const cpx = vec(block.center_px);
    const [cx, cz] = center.length >= 3 ? [center[0], center[2]] : center;
    return {
      width,
      height,
      center: [cx, cz],
      mpp: mpp / k,
      centerPx: cpx && cpx.length >= 2 && cpx.every(Number.isFinite) ? [cpx[0] * k, cpx[1] * k] : [frontTileWidth(width, height) / 2, height / 2],
      source: 'projection',
    };
  }
  const exact = bboxFrom(json);
  const box = exact || approxMarkerBox(json);
  const w = Number(imageSize?.width);
  const h = Number(imageSize?.height);
  if (!box || !(w > 0) || !(h > 0)) return null;
  const { lo, hi } = box;
  const three = lo.length >= 3;
  const diag = Math.hypot(...hi.map((v, i) => v - lo[i]));
  const extent = Math.max(diag, 1e-3);
  const tile = frontTileWidth(w, h);
  const cx = (lo[0] + hi[0]) / 2;
  const cz = three ? (lo[2] + hi[2]) / 2 : (lo[1] + hi[1]) / 2;
  return {
    width: w,
    height: h,
    center: [cx, cz],
    mpp: (extent * margin) / tile,
    centerPx: [tile / 2, h / 2],
    source: exact ? 'bbox' : 'joints',
  };
}

/**
 * Rough bbox of what rig_humanoid draws into the front tile of markers.png (body plus joint
 * spheres placed 8 % of the height in front of the body), estimated from markers.json
 * `joints` + `height_m`. Only used when markers.json has neither a projection block nor a
 * bbox; the editor then labels the overlay as approximate.
 */
export function approxMarkerBox(json) {
  const pts = json?.joints && typeof json.joints === 'object'
    ? Object.values(json.joints).map(vec).filter((p) => p && p.length >= 3 && p.every(Number.isFinite))
    : [];
  if (pts.length < 4) return null;
  const H = Number(json.height_m) > 0 ? Number(json.height_m) : Math.max(...pts.map((p) => p[2]));
  const lo = [0, 1, 2].map((i) => Math.min(...pts.map((p) => p[i])));
  const hi = [0, 1, 2].map((i) => Math.max(...pts.map((p) => p[i])));
  const flesh = 0.04 * H;
  const sphere = 0.012 * H * 1.4;
  lo[0] -= flesh;
  hi[0] += flesh;
  lo[2] = Math.min(0, lo[2] - sphere);
  hi[2] = Math.max(H, hi[2] + sphere);
  const bodyFront = lo[1] - 0.1 * H;
  hi[1] += 0.1 * H;
  lo[1] = bodyFront - 0.08 * H - sphere;
  return { lo, hi };
}

/**
 * Saturated colour blobs in an RGBA buffer (the joint spheres rig_humanoid draws into
 * markers.png: green/yellow/red by confidence, blue for derived joints). Only x < maxX is
 * scanned (the front tile). Returns [{x, y, n, k}] centroids (k = colour class).
 */
export function detectColorBlobs(data, width, height, { maxX = width, minArea = 25, maxArea = 2500 } = {}) {
  const colour = (i) => {
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    if (data[i + 3] < 200) return 0;
    const mx = Math.max(r, g, b);
    const mn = Math.min(r, g, b);
    if (mx < 90 || mx - mn < 80) return 0;
    if (r > 150 && g > 150 && b < 110) return 2;
    if (g >= r && g >= b) return 1;
    if (r >= g && r >= b) return 3;
    return 4;
  };
  const seen = new Uint8Array(width * height);
  const stack = new Int32Array(width * height);
  const blobs = [];
  const limit = Math.min(width, Math.max(1, Math.round(maxX)));
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < limit; x++) {
      const start = y * width + x;
      if (seen[start]) continue;
      const k = colour(start * 4);
      if (!k) continue;
      let top = 0;
      stack[top++] = start;
      seen[start] = 1;
      let n = 0;
      let sx = 0;
      let sy = 0;
      while (top) {
        const p = stack[--top];
        const px = p % width;
        const py = (p - px) / width;
        n++;
        sx += px;
        sy += py;
        const around = [px > 0 ? p - 1 : -1, px < limit - 1 ? p + 1 : -1, py > 0 ? p - width : -1, py < height - 1 ? p + width : -1];
        for (const q of around) {
          if (q < 0 || seen[q] || colour(q * 4) !== k) continue;
          seen[q] = 1;
          stack[top++] = q;
        }
      }
      if (n >= minArea && n <= maxArea) blobs.push({ x: sx / n, y: sy / n, n, k });
    }
  }
  return blobs;
}

/** Greedy nearest unique matching of predicted points [{key,u,v}] to blobs within maxDist px. */
export function matchPoints(predicted, blobs, maxDist) {
  const pairs = [];
  for (const p of predicted) {
    for (const b of blobs) {
      const d = Math.hypot(b.x - p.u, b.y - p.v);
      if (d <= maxDist) pairs.push({ p, b, d });
    }
  }
  pairs.sort((a, b) => a.d - b.d);
  const usedP = new Set();
  const usedB = new Set();
  const out = [];
  for (const { p, b, d } of pairs) {
    if (usedP.has(p) || usedB.has(b)) continue;
    usedP.add(p);
    usedB.add(b);
    out.push({ ...p, bx: b.x, by: b.y, d });
  }
  return out;
}

/**
 * Least-squares orthographic front projection from metre/pixel pairs [{x, z, u, v}]:
 * u = u0 + s·x, v = v0 − s·z (one scale for both axes). Null when underdetermined.
 */
export function fitProjection(pairs, { width, height } = {}) {
  const n = pairs.length;
  if (n < 3) return null;
  let sx = 0, sz = 0, su = 0, sv = 0, sxx = 0, szz = 0, sxu = 0, szv = 0;
  for (const { x, z, u, v } of pairs) {
    sx += x; sz += z; su += u; sv += v;
    sxx += x * x; szz += z * z; sxu += x * u; szv += z * v;
  }
  const den = sxx - (sx * sx) / n + (szz - (sz * sz) / n);
  if (!(den > 1e-9)) return null;
  const s = (sxu - (sx * su) / n - (szv - (sz * sv) / n)) / den;
  if (!(s > 0)) return null;
  const u0 = (su - s * sx) / n;
  const v0 = (sv + s * sz) / n;
  let err = 0;
  for (const { x, z, u, v } of pairs) err += (u0 + s * x - u) ** 2 + (v0 - s * z - v) ** 2;
  return { width, height, center: [0, 0], centerPx: [u0, v0], mpp: 1 / s, rms: Math.sqrt(err / n), points: n, source: 'spheres' };
}

/**
 * Refine an approximate projection with the joint spheres visible in markers.png:
 * predict every markers.json joint, match blobs (wide, then narrow), fit. Null if unreliable.
 */
export function calibrateProjection(proj, json, blobs, { wide = 30, narrow = 10, maxRms = 4 } = {}) {
  if (!proj || !json?.joints || !blobs?.length) return null;
  const joints = Object.entries(json.joints)
    .map(([key, v]) => [key, vec(v)])
    .filter(([, p]) => p && p.length >= 3 && p.every(Number.isFinite))
    .map(([key, p]) => ({ key, x: p[0], z: p[2] }));
  const predict = (pr) => joints.map((j) => {
    const [u, v] = metresToPx(pr, [j.x, j.z]);
    return { ...j, u, v };
  });
  const toPairs = (matches) => matches.map((m) => ({ x: m.x, z: m.z, u: m.bx, v: m.by }));
  const first = fitProjection(toPairs(matchPoints(predict(proj), blobs, wide)), proj);
  if (!first) return null;
  const second = fitProjection(toPairs(matchPoints(predict(first), blobs, narrow)), proj) || first;
  if (second.points < 4 || second.rms > maxRms) return null;
  return { ...second, width: proj.width, height: proj.height };
}

export function metresToPx(proj, [x, z]) {
  return [proj.centerPx[0] + (x - proj.center[0]) / proj.mpp, proj.centerPx[1] - (z - proj.center[1]) / proj.mpp];
}

export function pxToMetres(proj, [u, v]) {
  return [proj.center[0] + (u - proj.centerPx[0]) * proj.mpp, proj.center[1] - (v - proj.centerPx[1]) * proj.mpp];
}

export function clampPx(proj, [u, v]) {
  return [Math.min(proj.width, Math.max(0, u)), Math.min(proj.height, Math.max(0, v))];
}

/** Name of the symmetric marker, or null. */
export function mirrorName(name) {
  const s = String(name);
  const rules = [
    [/_l$/, '_r'], [/_r$/, '_l'], [/_L$/, '_R'], [/_R$/, '_L'],
    [/\.l$/, '.r'], [/\.r$/, '.l'], [/\.L$/, '.R'], [/\.R$/, '.L'],
    [/^l_/, 'r_'], [/^r_/, 'l_'], [/^L_/, 'R_'], [/^R_/, 'L_'],
    [/^Left/, 'Right'], [/^Right/, 'Left'], [/^left/, 'right'], [/^right/, 'left'],
  ];
  for (const [re, rep] of rules) if (re.test(s)) return s.replace(re, rep);
  return null;
}

/** Move one marker; with symmetry the partner gets the mirrored X (around centerX) and same Z. */
export function moveMarker(markers, name, point, { symmetry = false, centerX = 0 } = {}) {
  const next = { ...markers, [name]: [point[0], point[1]] };
  if (symmetry) {
    const partner = mirrorName(name);
    if (partner && Object.prototype.hasOwnProperty.call(markers, partner)) next[partner] = [2 * centerX - point[0], point[1]];
  }
  return next;
}

/** True when every left/right pair mirrors around X = 0 within tol metres (T/A-pose, no prop). */
export function markersSymmetric(markers, tol = 0.06) {
  let pairs = 0;
  for (const [name, [x, z]] of Object.entries(markers || {})) {
    const partner = mirrorName(name);
    if (!partner || !markers[partner]) continue;
    pairs++;
    const [px, pz] = markers[partner];
    if (Math.abs(x + px) > tol || Math.abs(z - pz) > tol) return false;
  }
  return pairs > 0;
}

export function roundMarkers(markers, digits = 4) {
  const k = 10 ** digits;
  const out = {};
  for (const [name, [x, z]] of Object.entries(markers || {})) out[name] = [Math.round(x * k) / k, Math.round(z * k) / k];
  return out;
}

// --------------------------------------------------------------------------- clips

/** [[id, label]] pairs; the label (index 1) is a getter over core.clipCat.<id>. */
export const CLIP_CATEGORIES = ['idle', 'walk', 'run', 'attack', 'cast', 'hit', 'death', 'jump', 'emote', 'other'].map((id) => {
  const pair = [id];
  Object.defineProperty(pair, 1, { get: () => t(`core.clipCat.${id}`), enumerable: true });
  return pair;
});

export function categoryLabel(id) {
  if (id === 'all') return t('core.clipCat.all');
  return CLIP_CATEGORIES.find(([c]) => c === id)?.[1] || id;
}

export function clipList(index) {
  if (Array.isArray(index)) return index;
  if (Array.isArray(index?.clips)) return index.clips;
  return [];
}

/** Chips: [{id:'all'}, ...categories present], each with a count. */
export function clipCategories(clips) {
  const counts = new Map();
  for (const c of clips) {
    const cat = CLIP_CATEGORIES.some(([id]) => id === c.category) ? c.category : 'other';
    counts.set(cat, (counts.get(cat) || 0) + 1);
  }
  const out = [{ id: 'all', title: t('core.clipCat.all'), count: clips.length }];
  for (const [id, title] of CLIP_CATEGORIES) if (counts.get(id)) out.push({ id, title, count: counts.get(id) });
  return out;
}

const fold = (s) => String(s || '').toLowerCase().replace(/\u0451/g, '\u0435');

export function filterClips(clips, { category = 'all', query = '' } = {}) {
  const words = fold(query).split(/\s+/).filter(Boolean);
  return clips.filter((c) => {
    const cat = CLIP_CATEGORIES.some(([id]) => id === c.category) ? c.category : 'other';
    if (category && category !== 'all' && cat !== category) return false;
    if (!words.length) return true;
    const hay = fold([c.name, c.id, c.category, categoryLabel(cat), ...(c.tags || [])].join(' '));
    return words.every((w) => hay.includes(w));
  });
}

/**
 * Locomotion clip, the rule of the retarget step (_anim_lib.is_locomotion, contract §7): an explicit
 * `locomotion` flag, else the tag "locomotion", else category walk/run.
 */
export function isLocomotionClip(clip) {
  if (clip && typeof clip.locomotion === 'boolean') return clip.locomotion;
  if ((clip?.tags || []).map((t) => String(t).toLowerCase()).includes('locomotion')) return true;
  return ['walk', 'run'].includes(clip?.category);
}

export function defaultClipOptions(clip) {
  const looping = ['idle', 'walk', 'run'].includes(clip?.category) || /idle|walk|run|loop/i.test(clip?.name || clip?.id || '');
  // only locomotion plays in place by default: a standing clip (lunge, death) keeps its hips motion,
  // in place its planted feet would skate
  return { loop: looping, in_place: isLocomotionClip(clip) };
}

/** selection {clipId: {loop, in_place}} -> passport clips[] (contract §8). */
export function clipsForPassport(selection) {
  return Object.entries(selection || {}).map(([id, opt]) => ({
    id,
    source: 'library:' + id,
    loop: !!opt.loop,
    in_place: opt.in_place !== false,
  }));
}

const CREATURE_CLIP_BASES = ['idle', 'walk', 'run', 'fly', 'glide', 'attack', 'hit', 'death'];
/** Procedural clip names of anim_creature ("-loop" marks looping clips, contract §7). */
export const CREATURE_CLIP_NAMES = ['idle-loop', 'walk-loop', 'run-loop', 'fly-loop', 'glide-loop', 'attack', 'hit', 'death'];

export const creatureClipBase = (name) => String(name).replace(/-loop$/, '');

/** Checkbox options from the registry (params.clips.items.enum), fallback to the planned list. */
export function creatureClipOptions(defs) {
  const clipsDef = defs?.clips;
  const names = clipsDef?.items?.enum?.length ? clipsDef.items.enum : CREATURE_CLIP_NAMES;
  return names.map((name) => {
    const base = creatureClipBase(name);
    return { id: name, base, title: CREATURE_CLIP_BASES.includes(base) ? t(`core.creatureClip.${base}`) : base, loop: /-loop$/.test(name) };
  });
}

const WINGED = ['winged_quadruped', 'bird'];

/** Flying clips only make sense for winged templates ('auto' leaves the decision to Blender). */
export function creatureClipAvailable(name, template) {
  const base = creatureClipBase(name);
  if (base === 'fly' || base === 'glide') return !template || template === 'auto' || WINGED.includes(template);
  return true;
}

/** anim_creature clip names -> passport clips (id without "-loop", source procedural:<id>). */
export function creatureClipsForPassport(names) {
  return names.map((name) => {
    const base = creatureClipBase(name);
    return { id: base, source: 'procedural:' + base, loop: /-loop$/.test(name), in_place: true };
  });
}

/** Passport procedural clips -> anim_creature names known to the options. */
export function creatureNamesFromPassport(clips, options) {
  const bases = (clips || []).map((c) => String(c.source || '')).filter((s) => s.startsWith('procedural:')).map((s) => s.slice(11));
  return options.filter((o) => bases.includes(o.base) || bases.includes(o.id)).map((o) => o.id);
}

/**
 * Library selection -> retarget params.clips ({source, name, loop[, in_place]}). in_place follows the
 * retarget step's root_motion semantics (contract §7): a locomotion clip (walk/run, tag or flag
 * locomotion) is sent in_place: true unless unticked (then false); a standing clip gets no in_place, so
 * root_motion decides and its hips motion stays (in_place:true would make planted feet skate) - only
 * an explicit tick sends in_place: true for it.
 */
export function retargetClips(selection, clips = []) {
  const byId = new Map(clips.map((c) => [c.id, c]));
  return Object.entries(selection || {}).map(([id, opt]) => {
    const clip = byId.get(id);
    const loco = isLocomotionClip(clip);
    const inPlace = typeof opt?.in_place === 'boolean' || opt?.in_place === 'strict' ? opt.in_place : loco;
    const out = { source: 'library:' + id, name: String(clip?.name || id), loop: !!opt?.loop };
    if (loco || inPlace) out.in_place = inPlace;
    return out;
  });
}

// --------------------------------------------------------------------------- presets & batch

export function filterPresets(presets, kind) {
  const info = kindInfo(kind);
  const list = Array.isArray(presets) ? presets : Array.isArray(presets?.presets) ? presets.presets : [];
  return list.filter((p) => {
    if (!p) return false;
    if (!p.kind || p.kind === 'any' || p.kind === '*') return true;
    const kinds = Array.isArray(p.kind) ? p.kind : [p.kind];
    return kinds.includes(kind) || (info && kinds.includes(info.apiKind));
  });
}

/** Seeds 1..20 -> batch items for POST /batch. */
export function seedBatchItems({ kind, from = 1, to = 1, namePrefix, params = {}, preset, limit = 200 }) {
  const info = kindInfo(kind);
  const step = generatorStep(kind);
  const lo = Math.max(0, Math.round(Math.min(from, to)));
  const hi = Math.max(0, Math.round(Math.max(from, to)));
  const items = [];
  for (let seed = lo; seed <= hi && items.length < limit; seed++) {
    const p = { ...params, seed };
    const item = {
      name: `${namePrefix || info?.defaultName || t('core.batch.asset')} #${seed}`,
      kind: info?.apiKind || kind,
      source: { type: 'params', params: p },
    };
    if (preset) item.preset = preset;
    else if (step) item.steps = [{ step, params: p }];
    items.push(item);
  }
  return items;
}

export function seedVariants(base, count = 6) {
  const b = Math.max(0, Math.round(Number(base) || 0));
  return Array.from({ length: count }, (_, i) => b + i + 1);
}

/** Batch items from sources ({type:'asset', assetId} / {type:'job', jobId}). */
export function sourceBatchItems({ kind, sources, preset, steps, namePrefix }) {
  const info = kindInfo(kind);
  return sources.map((src, i) => {
    const item = {
      name: src.name || `${namePrefix || info?.defaultName || t('core.batch.asset')} ${i + 1}`,
      kind: info?.apiKind || kind,
      source: src.source,
    };
    if (preset) item.preset = preset;
    else item.steps = (steps || []).map((step) => (typeof step === 'string' ? { step, params: {} } : step));
    return item;
  });
}

/**
 * GLB files -> multipart batch requests: [{files, items}] of at most `size` files each; every
 * item points at its file with source {type:'upload', fileIndex} (index inside its request).
 */
export function fileBatchChunks(files, { kind, preset, steps, size = 20 } = {}) {
  const list = [...(files || [])];
  const chunk = Math.max(1, Math.min(50, Math.round(size) || 20));
  const out = [];
  for (let start = 0; start < list.length; start += chunk) {
    const part = list.slice(start, start + chunk);
    const sources = part.map((f, i) => ({
      name: String(f?.name || t('core.batch.file', { n: start + i + 1 })).replace(/\.(glb|fbx|obj)$/i, ''),
      source: { type: 'upload', fileIndex: i },
    }));
    out.push({ files: part, items: sourceBatchItems({ kind, sources, preset, steps }) });
  }
  return out;
}

export function batchCounts(items, jobsById = {}) {
  const counts = { total: 0, done: 0, failed: 0, running: 0, queued: 0 };
  for (const item of items || []) {
    counts.total++;
    const status = item.status || jobsById[item.jobId]?.status || 'queued';
    if (status === 'completed' || status === 'done' || status === 'created') counts.done++;
    else if (status === 'failed' || status === 'cancelled' || status === 'partial') counts.failed++;
    else if (status === 'running') counts.running++;
    else counts.queued++;
  }
  return counts;
}

/** GLB files from a file list / folder selection, sorted by relative path. */
export function onlyGlb(files) {
  return [...(files || [])]
    .filter((f) => /\.glb$/i.test(f?.name || ''))
    .sort((a, b) => String(a.webkitRelativePath || a.name).localeCompare(String(b.webkitRelativePath || b.name)));
}

/** Merge GET /batch/:id summary with the items returned by POST /batch. */
export function batchItemsFrom(summary, known = []) {
  const fromServer = Array.isArray(summary?.items) ? summary.items : Array.isArray(summary) ? summary : [];
  const key = (it) => it?.assetId || it?.jobId;
  const byKey = new Map(known.map((it) => [key(it), { ...it }]));
  for (const it of fromServer) {
    const k = key(it);
    byKey.set(k, { ...(byKey.get(k) || {}), ...it, status: it.status || it.job?.status || byKey.get(k)?.status });
  }
  return [...byKey.values()];
}

// --------------------------------------------------------------------------- outputs & files

/** Absolute output path -> {assetId, version, stepDir, file} under data/library/assets/<id>/vNNN/<stepDir>/. */
export function relFromOutput(absPath, assetId) {
  if (typeof absPath !== 'string' || !absPath) return null;
  const p = absPath.replace(/\\/g, '/');
  const re = /(?:^|\/)assets\/([^/]+)\/(v\d+)\/([^/]+)\/(.+)$/;
  const m = p.match(re);
  if (!m) return null;
  if (assetId && m[1] !== assetId) return null;
  if (m[4].split('/').some((part) => part === '..' || part === '')) return null;
  return { assetId: m[1], version: m[2], stepDir: m[3], file: m[4] };
}

/**
 * Any absolute path inside data/library/assets/<id>/ -> "v003/02_rig/markers.png",
 * "exports/godot-v004.zip", "source/source.glb" (the server's /files/<rel> form). Null outside.
 */
export function assetRelPath(absPath, assetId) {
  if (typeof absPath !== 'string' || !absPath || !assetId) return null;
  const p = absPath.replace(/\\/g, '/');
  const marker = '/assets/' + assetId + '/';
  const at = p.lastIndexOf(marker);
  const rel = at >= 0 ? p.slice(at + marker.length) : p.startsWith('assets/' + assetId + '/') ? p.slice(('assets/' + assetId + '/').length) : null;
  if (!rel) return null;
  const parts = rel.split('/');
  if (parts.length < 2 || parts.some((part) => part === '..' || part === '.' || part === '')) return null;
  return rel;
}

function flattenOutputs(outputs, prefix = '') {
  const out = [];
  if (!outputs) return out;
  if (typeof outputs === 'string') return [{ key: prefix || 'file', path: outputs }];
  if (Array.isArray(outputs)) {
    outputs.forEach((v, i) => out.push(...flattenOutputs(v, prefix ? `${prefix}.${i}` : String(i))));
    return out;
  }
  if (typeof outputs === 'object') {
    for (const [k, v] of Object.entries(outputs)) out.push(...flattenOutputs(v, prefix ? `${prefix}.${k}` : k));
  }
  return out;
}

export function outputEntries(outputs) {
  return flattenOutputs(outputs).filter((e) => typeof e.path === 'string');
}

export function extOf(p) {
  const m = String(p || '').match(/\.([a-z0-9]+)$/i);
  return m ? '.' + m[1].toLowerCase() : '';
}

export function fileNameOf(p) {
  return String(p || '').replace(/\\/g, '/').split('/').pop();
}

export function dirOf(p) {
  const s = String(p || '');
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  return i > 0 ? s.slice(0, i) : s;
}

/** First output matching extension(s) and a key/file-name pattern. */
export function findOutput(outputs, { ext, match } = {}) {
  const exts = ext ? (Array.isArray(ext) ? ext : [ext]).map((e) => e.toLowerCase()) : null;
  const entries = outputEntries(outputs).filter((e) => !exts || exts.includes(extOf(e.path)));
  if (!match) return entries[0]?.path || null;
  const re = match instanceof RegExp ? match : new RegExp(match, 'i');
  const hit = entries.find((e) => re.test(e.key)) || entries.find((e) => re.test(fileNameOf(e.path)));
  return hit?.path || null;
}

const IMAGE_EXT = ['.png', '.jpg', '.jpeg', '.webp'];

export function imageOutputs(outputs) {
  return outputEntries(outputs).filter((e) => IMAGE_EXT.includes(extOf(e.path)));
}

/** Label for a preview image by its key / file name. */
export function imageLabel(entry) {
  const s = (entry.key + ' ' + fileNameOf(entry.path)).toLowerCase();
  if (/before|src|source|\u0438\u0441\u0445\u043e\u0434/.test(s)) return t('core.image.before');
  if (/after/.test(s)) return t('core.image.after');
  if (/marker/.test(s)) return t('core.image.markers');
  if (/pose/.test(s)) return t('core.image.pose');
  if (/sheet|sprite/.test(s)) return t('core.image.sprites');
  if (/thumb/.test(s)) return t('core.image.thumb');
  return t('core.image.preview');
}

/** Before/after pair for the cleanup preview. */
export function beforeAfter(outputs, previousOutputs) {
  const before = findOutput(outputs, { ext: IMAGE_EXT, match: /before|source|src/ }) || findOutput(previousOutputs, { ext: IMAGE_EXT, match: /preview|after/ });
  const after = findOutput(outputs, { ext: IMAGE_EXT, match: /after/ }) || findOutput(outputs, { ext: IMAGE_EXT, match: /preview/ });
  return { before: before && before !== after ? before : null, after };
}

/** Latest passport pipeline entry for one of the given steps. */
export function latestPipelineEntry(asset, steps, { status } = {}) {
  const wanted = Array.isArray(steps) ? steps : [steps];
  const list = Array.isArray(asset?.pipeline) ? asset.pipeline : [];
  for (let i = list.length - 1; i >= 0; i--) {
    const entry = list[i];
    if (!entry || !wanted.includes(entry.step)) continue;
    if (status && normalizeStatus(entry.status) !== status) continue;
    return entry;
  }
  return null;
}

/** Sockets declared by step results (stats.sockets / outputs.sockets), names only. */
export function collectSockets(asset) {
  const names = new Set();
  for (const entry of asset?.pipeline || []) {
    const lists = [entry?.result?.stats?.sockets, entry?.result?.sockets, entry?.result?.outputs?.sockets];
    for (const list of lists) {
      if (!Array.isArray(list)) continue;
      for (const s of list) {
        const name = typeof s === 'string' ? s : s?.name || s?.game_socket;
        if (typeof name === 'string' && name.startsWith('socket_')) names.add(name);
      }
    }
  }
  return [...names];
}

// --------------------------------------------------------------------------- jobs

export const STATUS_LABELS = labelTable(['queued', 'pending', 'running', 'completed', 'done', 'failed', 'cancelled', 'skipped', 'created', 'partial'], 'core.status.');

export function statusLabel(status) {
  return (Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) && STATUS_LABELS[status]) || status || '—';
}

export function isActiveStatus(status) {
  return status === 'queued' || status === 'running' || status === 'pending';
}

export function jobProgress(job) {
  if (!job) return 0;
  if (job.status === 'completed') return 100;
  const steps = Array.isArray(job.steps) ? job.steps : [];
  if (!steps.length) return Math.max(0, Math.min(100, Number(job.progress) || 0));
  let sum = 0;
  for (const s of steps) {
    if (s.status === 'completed' || s.status === 'done') sum += 100;
    else if (s.status === 'running') sum += Math.max(0, Math.min(100, Number(s.progress) || 0));
  }
  return Math.round(sum / steps.length);
}

const STEP_KEEP = ['result', 'logTail', 'error', 'message', 'params', 'outDir', 'dir', 'startedAt', 'finishedAt', 'warnings', 'logFile'];

/**
 * Merge a job record into what the UI already knows. GET /jobs list items carry steps as
 * {step, status, progress} only, so step results, log tails and errors loaded earlier from
 * GET /jobs/:id are kept; local fields (_meta) survive too.
 */
export function mergeJobRecord(prev, incoming) {
  if (!incoming) return prev;
  if (!prev) return { ...incoming };
  const next = { ...prev, ...incoming };
  const oldSteps = Array.isArray(prev.steps) ? prev.steps : [];
  const newSteps = Array.isArray(incoming.steps) ? incoming.steps : null;
  if (!newSteps || (!newSteps.length && oldSteps.length)) {
    next.steps = oldSteps;
  } else {
    next.steps = newSteps.map((s, i) => {
      const old = oldSteps[i];
      if (!old || (old.step && s?.step && old.step !== s.step)) return s;
      const merged = { ...old, ...s };
      for (const key of STEP_KEEP) {
        if ((s?.[key] === undefined || s?.[key] === null) && old[key] !== undefined && old[key] !== null) merged[key] = old[key];
      }
      return merged;
    });
  }
  return next;
}

/**
 * Polling plan for active jobs: running jobs and the ones started from this view (_meta) are
 * fetched one by one every second; queued ones only through GET /jobs every few seconds, so a
 * page reload during a 200-item batch does not fire dozens of requests per second.
 */
export function pollPlan(jobs) {
  const active = Object.values(jobs || {}).filter((j) => j && j.id && isActiveStatus(j.status));
  const each = active.filter((j) => j.status === 'running' || j._meta).map((j) => j.id);
  return { each, list: active.length > each.length };
}

export function currentJobStep(job) {
  const steps = Array.isArray(job?.steps) ? job.steps : [];
  return steps.find((s) => s.status === 'running') || steps.find((s) => s.status === 'failed') || steps.find((s) => isActiveStatus(s.status)) || steps[steps.length - 1] || null;
}

/** Plain-language error of a failed job (or null). */
export function jobError(job) {
  if (!job) return null;
  const failed = (job.steps || []).find((s) => s.status === 'failed');
  const raw = failed?.result?.error || failed?.error || job.error || (job.status === 'failed' ? job.message : null);
  if (!raw && job.status !== 'failed') return null;
  return raw ? humanizeError(raw) : t('core.error.stepFailed');
}

const HAS_CYRILLIC = /[\u0430-\u044f\u0451]/i;

/**
 * Rules for English Blender/Python/Node messages -> dictionary keys. Russian text is already
 * written for people (server messages such as "the graphics card is busy with another process
 * for more than 3600 s" or "a job is running on the asset") and is returned unchanged:
 * rewriting it by keywords produced false messages.
 */
const ERROR_RULES = [
  [/blender.*not found|ENOENT.*blender|ASTRA_BLENDER/i, 'core.error.blender'],
  [/godot.*not found/i, 'core.error.godot'],
  [/\btimed? ?out\b|\btimeout\b/i, 'core.error.timeout'],
  [/\bcancel+ed\b|\bAbortError\b/i, 'core.error.cancelled'],
  [/out of memory|\bVRAM\b/i, 'core.error.vram'],
  [/gpu[_ ]?lock|gpu_timeout/i, 'core.error.gpuLock'],
  [/\bEBUSY\b|resource busy or locked/i, 'core.error.busy'],
  [/ECONNREFUSED|Failed to fetch|NetworkError/i, 'core.error.offline'],
  [/unsupported input format/i, 'core.error.format'],
  [/no (mesh|geometry)|has no faces/i, 'core.error.noGeometry'],
  [/no armature/i, 'core.error.noArmature'],
  [/request\.out_dir/i, 'core.error.badRequest'],
];

export function humanizeError(text) {
  const s = String(text ?? '').trim();
  if (!s) return t('core.error.unknown');
  // The quality-check marker of af_run is exact, so it is translated even inside a Russian frame.
  const checks = s.match(/checks failed:\s*(.+)$/im);
  if (checks) return t('core.error.checks', { list: checks[1].trim().replace(/\.$/, '') });
  if (HAS_CYRILLIC.test(s)) return s;
  const rig = s.match(/rig not built:\s*(.+)$/im);
  if (rig) return t('core.error.rig', { reason: rig[1].trim().replace(/\.$/, '') });
  for (const [re, key] of ERROR_RULES) if (re.test(s)) return t(key);
  const first = s.split(/\r?\n/).find((line) => line.trim()) || s;
  return t('core.error.generic', { text: first.slice(0, 240) });
}

/** Last n lines of a log tail (string or array). */
export function tailLines(log, n = 14) {
  const lines = Array.isArray(log) ? log.map(String) : String(log || '').split(/\r?\n/);
  const clean = lines.filter((l) => l.trim().length);
  return clean.slice(-n).join('\n');
}

// --------------------------------------------------------------------------- checks

const baseName = (p) => String(p || '').split('/').pop();

/**
 * godot_check.json of godot_package ({result:{ok, godot, errors, warnings, scenes:[…],
 * fx_scenes:[…]}}) -> readable rows: scene loads, sockets found, effects, animations.
 */
export function godotCheckRows(json) {
  const root = json?.result && typeof json.result === 'object' ? json.result : json;
  const rows = [];
  if (!root || typeof root !== 'object') return rows;
  if ('ok' in root) rows.push({ name: t('core.checks.godotOpens', { version: (root.godot || '').replace(/\s*\(.*\)$/, '') }), ok: !!root.ok, severity: 'error' });
  for (const e of root.errors || []) rows.push({ name: typeof e === 'string' ? e : e?.message || JSON.stringify(e), ok: false, severity: 'error' });
  for (const w of root.warnings || []) rows.push({ name: typeof w === 'string' ? w : w?.message || JSON.stringify(w), ok: false, severity: 'warning' });
  for (const sc of root.scenes || []) {
    const file = baseName(sc.path);
    rows.push({ name: t('core.checks.scene', { file }), ok: !!sc.loaded && sc.instanced !== false, severity: 'error' });
    const sockets = sc.sockets && typeof sc.sockets === 'object' ? Object.values(sc.sockets) : [];
    if (sockets.length) {
      const found = sockets.filter((s) => s?.found).length;
      rows.push({ name: t('core.checks.sockets'), ok: found === sockets.length, value: found, limit: sockets.length, severity: 'error' });
    }
    if ((sc.script_missing_sockets || []).length) rows.push({ name: t('core.checks.scriptSockets'), ok: false, note: sc.script_missing_sockets.join(', '), severity: 'error' });
    for (const fx of sc.fx || []) rows.push({ name: t('core.checks.fx', { name: fx.name || '', socket: fx.socket || '—' }), ok: fx.ok !== false, severity: 'error' });
    for (const a of sc.animation_checks || []) rows.push({ name: t('core.checks.clip', { name: a.name || a.animation || '' }).trim(), ok: a.ok !== false, note: a.note || a.error, severity: 'error' });
  }
  for (const fx of root.fx_scenes || []) rows.push({ name: t('core.checks.fxScene', { file: baseName(fx.path) }), ok: fx.ok !== false && fx.loaded !== false, severity: 'error' });
  return rows;
}

/** Any checks container -> [{name, ok, value, limit, severity, note}]. */
export function normalizeChecks(src) {
  if (!src) return [];
  if (!Array.isArray(src) && typeof src === 'object') {
    const root = src.result && typeof src.result === 'object' ? src.result : src;
    if (Array.isArray(root.scenes) || Array.isArray(root.fx_scenes)) return godotCheckRows(src);
  }
  if (Array.isArray(src)) {
    return src
      .filter(Boolean)
      .map((c, i) => (typeof c === 'object'
        ? {
            name: String(c.name ?? c.id ?? c.check ?? `check_${i + 1}`),
            ok: c.ok === undefined ? c.passed !== false && c.status !== 'fail' && c.status !== 'failed' : !!c.ok,
            value: c.value,
            limit: c.limit,
            severity: c.severity || 'error',
            note: c.note || c.message || c.error || undefined,
          }
        : { name: String(c), ok: true, severity: 'info' }));
  }
  if (typeof src !== 'object') return [];
  if (Array.isArray(src.checks)) return normalizeChecks(src.checks);
  if (Array.isArray(src.results)) return normalizeChecks(src.results);
  if (Array.isArray(src.errors) || Array.isArray(src.warnings)) {
    const rows = [];
    for (const e of src.errors || []) rows.push({ name: typeof e === 'string' ? e : e?.name || 'error', ok: false, severity: 'error', note: typeof e === 'object' ? e?.message : undefined });
    for (const w of src.warnings || []) rows.push({ name: typeof w === 'string' ? w : w?.name || 'warning', ok: false, severity: 'warning' });
    if (!rows.length && src.ok !== undefined) rows.push({ name: 'validation', ok: !!src.ok, severity: 'error' });
    return rows;
  }
  const rows = [];
  for (const [name, v] of Object.entries(src)) {
    if (typeof v === 'boolean') rows.push({ name, ok: v, severity: 'error' });
    else if (v && typeof v === 'object' && 'ok' in v) rows.push({ name, ok: !!v.ok, value: v.value, limit: v.limit, severity: v.severity || 'error', note: v.note });
  }
  return rows;
}

export function checksSummary(rows) {
  const failed = rows.filter((r) => !r.ok && r.severity !== 'warning' && r.severity !== 'info').length;
  const warn = rows.filter((r) => !r.ok && r.severity === 'warning').length;
  return { total: rows.length, failed, warn, ok: failed === 0 };
}

// --------------------------------------------------------------------------- export profiles

const ALL = ['humanoid', 'creature', 'item', 'ship', 'turret'];

export const EXPORT_PROFILES = [
  { id: 'godot', kinds: ALL },
  { id: 'iso8', kinds: ALL },
].map((p) => withLabels(p, `core.export.${p.id}`));

export function exportProfilesFor(kind) {
  return EXPORT_PROFILES.filter((p) => p.kinds.includes(kind));
}

// --------------------------------------------------------------------------- effects

/** Planned list; to be replaced by a server list when one appears. */
export const FX_PRESETS = [
  { id: 'engine_trail', prefer: /thruster|engine/ },
  { id: 'thruster_glow', prefer: /thruster|engine/ },
  { id: 'aura', prefer: /chest|fx_body|hull/ },
  { id: 'magic_cast', prefer: /hand_r|hand|tip/ },
  { id: 'fire', prefer: /tip|mouth|head/ },
  { id: 'smoke', prefer: /thruster|tip|back/ },
  { id: 'sparks', prefer: /tip|muzzle|hand/ },
  { id: 'ice', prefer: /tip|hand|mouth/ },
  { id: 'poison', prefer: /tip|mouth|hand/ },
  { id: 'heal', prefer: /chest|hand/ },
  { id: 'muzzle_flash', prefer: /muzzle|tip/ },
  { id: 'nav_light', prefer: /nav|fx/ },
].map((p) => Object.defineProperty(p, 'title', { get: () => t(`core.fx.${p.id}`), enumerable: true }));

export const KIND_SOCKETS = {
  humanoid: ['socket_mount_hand_r', 'socket_mount_hand_l', 'socket_mount_back', 'socket_mount_hip_l', 'socket_mount_hip_r', 'socket_fx_head', 'socket_fx_chest'],
  creature: ['socket_fx_head', 'socket_fx_mouth', 'socket_mount_back', 'socket_fx_tail'],
  item: ['socket_grip_main', 'socket_grip_off', 'socket_fx_tip'],
  ship: ['socket_thruster_main_l', 'socket_thruster_main_r', 'socket_fx_nav_l', 'socket_fx_nav_r', 'socket_muzzle_01'],
  turret: ['socket_mount', 'socket_muzzle_01', 'socket_muzzle_02'],
};

/** Sockets found in step results plus the contract list (ship/turret guesses only when nothing was found). */
export function socketsFor(kind, asset) {
  const found = collectSockets(asset);
  const base = KIND_SOCKETS[kind] || [];
  const guessOnly = kind === 'ship' || kind === 'turret';
  return [...new Set([...found, ...(guessOnly && found.length ? [] : base)])];
}

export function fxPreset(id) {
  return FX_PRESETS.find((p) => p.id === id) || { id, title: id, prefer: null };
}

export function suggestSocket(presetId, sockets) {
  const preset = fxPreset(presetId);
  if (preset.prefer) {
    const hit = sockets.find((s) => preset.prefer.test(s));
    if (hit) return hit;
  }
  return sockets[0] || '';
}

export function fxForPassport(rows) {
  return rows.filter((r) => r.preset && r.socket).map((r) => ({ preset: r.preset, socket: r.socket, params: r.params || {} }));
}

// --------------------------------------------------------------------------- stick-figure poses

/** Mixamo parent map for the 25 core bones (contract §5); prefix "mixamorig:" stripped. */
export const MIXAMO_PARENTS = {
  Spine: 'Hips', Spine1: 'Spine', Spine2: 'Spine1', Neck: 'Spine2', Head: 'Neck', HeadTop_End: 'Head',
  LeftShoulder: 'Spine2', LeftArm: 'LeftShoulder', LeftForeArm: 'LeftArm', LeftHand: 'LeftForeArm',
  RightShoulder: 'Spine2', RightArm: 'RightShoulder', RightForeArm: 'RightArm', RightHand: 'RightForeArm',
  LeftUpLeg: 'Hips', LeftLeg: 'LeftUpLeg', LeftFoot: 'LeftLeg', LeftToeBase: 'LeftFoot', LeftToe_End: 'LeftToeBase',
  RightUpLeg: 'Hips', RightLeg: 'RightUpLeg', RightFoot: 'RightLeg', RightToeBase: 'RightFoot', RightToe_End: 'RightToeBase',
};

const stripMixamo = (n) => String(n).replace(/^mixamorig\d*:/, '');

function point3(v) {
  if (Array.isArray(v) && v.length >= 3) return [Number(v[0]), Number(v[1]), Number(v[2])];
  if (Array.isArray(v) && v.length === 2) return [Number(v[0]), 0, Number(v[1])];
  if (v && typeof v === 'object' && 'x' in v) return [Number(v.x), Number(v.y) || 0, Number(v.z) || 0];
  return null;
}

/**
 * Tolerant reader of <clip>.poses.json. Accepts
 *  clip_library format: {fps, frames:<count>, axes:'gltf_y_up_face_+z', joints:[names],
 *                        sticks:[[a,b]], samples:[{t, p:[[x,y,z]|null per joint]}]}
 *  {fps, bones:[names], parents:[idx|-1]|{name:parent}, frames:[[ [x,y,z] per bone ]]}
 *  {fps, frames:[{bone:[x,y,z]}]}
 *  {fps, bones:{name:{parent, frames:[[x,y,z]]}}}
 * Returns {fps, up, bones, edges:[[i,j]], frames:[[ [x,y,z]|null ]], times:[s], duration} or null.
 */
export function normalizePoses(json) {
  if (!json || typeof json !== 'object') return null;
  const fps = Number(json.fps) > 0 ? Number(json.fps) : 30;
  const declaredUp = json.up || json.up_axis || (/y_up/i.test(String(json.axes || '')) ? 'Y' : /z_up/i.test(String(json.axes || '')) ? 'Z' : null);
  let up = String(declaredUp || 'Z').toUpperCase() === 'Y' ? 'Y' : 'Z';
  let bones = [];
  let frames = [];
  let times = null;
  let parentOf = {};
  if (Array.isArray(json.joints) && Array.isArray(json.samples)) {
    bones = json.joints.map(String);
    frames = json.samples.map((smp) => bones.map((_, i) => point3(smp?.p?.[i])));
    times = json.samples.map((smp, i) => (Number.isFinite(Number(smp?.t)) ? Number(smp.t) : i / fps));
  } else if (Array.isArray(json.bones) && Array.isArray(json.frames)) {
    bones = json.bones.map(String);
    frames = json.frames.map((f) => (Array.isArray(f) ? bones.map((_, i) => point3(f[i])) : bones.map((b) => point3(f?.[b]))));
    if (Array.isArray(json.parents)) {
      json.parents.forEach((p, i) => {
        if (Number.isInteger(p) && p >= 0) parentOf[bones[i]] = bones[p];
        else if (typeof p === 'string') parentOf[bones[i]] = p;
      });
    } else if (json.parents && typeof json.parents === 'object') parentOf = { ...json.parents };
  } else if (Array.isArray(json.frames) && json.frames.length && typeof json.frames[0] === 'object' && !Array.isArray(json.frames[0])) {
    const names = new Set();
    for (const f of json.frames) for (const k of Object.keys(f || {})) names.add(k);
    bones = [...names];
    frames = json.frames.map((f) => bones.map((b) => point3(f?.[b])));
    if (json.parents && typeof json.parents === 'object' && !Array.isArray(json.parents)) parentOf = { ...json.parents };
  } else if (json.bones && typeof json.bones === 'object' && !Array.isArray(json.bones)) {
    bones = Object.keys(json.bones);
    const count = Math.max(0, ...bones.map((b) => json.bones[b]?.frames?.length || 0));
    frames = Array.from({ length: count }, (_, fi) => bones.map((b) => point3(json.bones[b]?.frames?.[fi])));
    for (const b of bones) if (json.bones[b]?.parent) parentOf[b] = json.bones[b].parent;
  } else {
    return null;
  }
  if (!bones.length || !frames.length) return null;
  if (!times) times = frames.map((_, i) => i / fps);
  const index = new Map(bones.map((b, i) => [b, i]));
  const strippedIndex = new Map(bones.map((b, i) => [stripMixamo(b), i]));
  const edges = [];
  const edgeSpec = Array.isArray(json.edges) ? json.edges : Array.isArray(json.sticks) ? json.sticks : null;
  if (edgeSpec) {
    for (const pair of edgeSpec) {
      if (!Array.isArray(pair)) continue;
      const [a, b] = pair;
      const ia = typeof a === 'number' ? a : index.get(a);
      const ib = typeof b === 'number' ? b : index.get(b);
      if (Number.isInteger(ia) && Number.isInteger(ib)) edges.push([ia, ib]);
    }
  } else {
    bones.forEach((b, i) => {
      const parent = parentOf[b];
      let pi = parent !== undefined ? index.get(parent) ?? strippedIndex.get(stripMixamo(parent)) : undefined;
      if (pi === undefined && !Object.keys(parentOf).length) {
        const mp = MIXAMO_PARENTS[stripMixamo(b)];
        pi = mp ? strippedIndex.get(mp) : undefined;
      }
      if (pi !== undefined) edges.push([pi, i]);
    });
  }
  if (!declaredUp) {
    const range = (axis) => {
      let lo = Infinity;
      let hi = -Infinity;
      for (const f of frames) for (const p of f) if (p) { lo = Math.min(lo, p[axis]); hi = Math.max(hi, p[axis]); }
      return hi - lo;
    };
    if (range(2) < 1e-6 && range(1) > 1e-6) up = 'Y';
  }
  const step = times.length > 1 ? (times[times.length - 1] - times[0]) / (times.length - 1) : 1 / fps;
  const byFrames = Number(json.frames) > 0 && !Array.isArray(json.frames) ? Number(json.frames) / fps : 0;
  const duration = Math.max(byFrames, times[times.length - 1] - times[0] + step, 1 / fps);
  return { fps, up, bones, edges, frames, times, duration };
}

/** Joint positions at time t (seconds, looped), linearly interpolated between samples. */
export function poseAt(poses, t) {
  const { frames, times, duration } = poses;
  if (frames.length === 1) return frames[0];
  const t0 = times[0];
  const local = (((t % duration) + duration) % duration) + t0;
  let i = 0;
  while (i < times.length - 1 && times[i + 1] <= local) i++;
  const j = (i + 1) % frames.length;
  const span = j === 0 ? t0 + duration - times[i] : times[j] - times[i];
  const k = span > 0 ? Math.min(1, Math.max(0, (local - times[i]) / span)) : 0;
  return frames[i].map((a, n) => {
    const b = frames[j][n];
    if (!a || !b) return a || null;
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
  });
}

/** 2D screen projection (x right, y up) of a 3D joint; yaw in radians around the up axis. */
export function projectJoint(p, { up = 'Z', yaw = 0 } = {}) {
  const [x, y, z] = p;
  const height = up === 'Y' ? y : z;
  const depth = up === 'Y' ? -z : y;
  return [x * Math.cos(yaw) + depth * Math.sin(yaw), height];
}

/** Bounds of all projected joints of all frames (stable framing during playback). */
export function poseBounds(poses, opts) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const f of poses?.frames || []) {
    for (const p of f) {
      if (!p || !p.every(Number.isFinite)) continue;
      const [sx, sy] = projectJoint(p, { up: poses.up, ...opts });
      minX = Math.min(minX, sx);
      maxX = Math.max(maxX, sx);
      minY = Math.min(minY, sy);
      maxY = Math.max(maxY, sy);
    }
  }
  if (!Number.isFinite(minX)) return null;
  return { minX, maxX, minY, maxY };
}

// --------------------------------------------------------------------------- triangle budget slider

/** Round a triangle budget to a friendly step (250 / 1000 / 5000). */
export function niceBudget(v) {
  const n = Math.max(0, Number(v) || 0);
  const step = n < 10000 ? 250 : n < 50000 ? 1000 : 5000;
  return Math.round(n / step) * step;
}

/** Logarithmic slider position 0..1000 -> budget in [min, max]. */
export function sliderToBudget(pos, min, max) {
  const lo = Math.max(1, min);
  const t = Math.max(0, Math.min(1, Number(pos) / 1000));
  return Math.min(max, Math.max(lo, niceBudget(lo * (max / lo) ** t)));
}

export function budgetToSlider(value, min, max) {
  const lo = Math.max(1, min);
  const v = Math.min(max, Math.max(lo, Number(value) || lo));
  return Math.round((Math.log(v / lo) / Math.log(max / lo)) * 1000);
}

// --------------------------------------------------------------------------- misc

const uiLocale = () => (getLang() === 'ru' ? 'ru-RU' : 'en-US');

export function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(uiLocale(), { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export function formatCount(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return Math.round(v).toLocaleString(uiLocale());
}

export function randomSeed() {
  return Math.floor(Math.random() * 99999) + 1;
}
