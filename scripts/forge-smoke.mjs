#!/usr/bin/env node
// Forge end-to-end smoke (not part of npm test): every preset on real inputs with the real Blender
// 5.0.1 and Godot 4.5.1, through the same in-process pipeline as `node scripts/forge.mjs --direct`.
//
//   node scripts/forge-smoke.mjs [--inputs <dir>] [--root C:/afs] [--only id,id] [--md docs/forge/SMOKE.md] [--json docs/forge/SMOKE.json]
//
// --root   short work folder (Windows MAX_PATH 260: Blender and Godot fail on long paths). It gets
//          lib/ (forge library: assets, jobs, clip library) and rt/ (Blender logs); both are wiped at
//          the start of a run. A folder that exists without the .af-smoke marker is never touched.
// --only   run only these cases (plus the cases they need); the other rows of the existing JSON are kept.
// --inputs folder with your own test models (or env ASTRA_SMOKE_INPUTS); every file is optional and a case
//          whose input is missing is skipped (generator cases need no input):
//            humanoid.glb   a character for humanoid_from_mesh / humanoid_sprites / rig_humanoid
//            creature.glb   a quadruped for creature_auto (profile.height_m from creature.json {"height_m": n})
//            ship.glb       a generated ship mesh for ship_from_mesh
//            clips/         a folder of Mixamo FBX clips for the clip library
//            part.glb + part.json {"region": {min, max}, "cut_plane": {axis, value}}  for replace_part
//          Godot: env ASTRA_GODOT (console executable of Godot 4.5.1).
// Inputs are read-only: they are copied into the forge library as asset sources.
import os from 'node:os';
import path from 'node:path';
import { existsSync, readFileSync, promises as fs } from 'node:fs';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const flagValue = (name) => {
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === `--${name}` && i + 1 < argv.length) return argv[i + 1];
    if (argv[i].startsWith(`--${name}=`)) return argv[i].slice(name.length + 3);
  }
  return null;
};
const INPUTS = path.resolve(flagValue('inputs') || process.env.ASTRA_SMOKE_INPUTS || path.join(ROOT, 'tests', 'fixtures', 'smoke'));
const inp = (name) => path.join(INPUTS, name);
const readJson = (file) => { try { return JSON.parse(readFileSync(file, 'utf8')); } catch { return null; } };
const IN = {
  humanoid: inp('humanoid.glb'),
  creature: inp('creature.glb'),
  ship: inp('ship.glb'),
  clips: inp('clips'),
  part: inp('part.glb'),
};
const CREATURE = readJson(inp('creature.json')) || {};
const PART = readJson(inp('part.json')) || {};

// id, preset, asset kind/name, source; needs = cases that must run first; expect = honest expectation
const CASES = [
  { id: 'clips', title: 'clip_library import', input: IN.clips, library: true },
  { id: 'item_kit_sword', preset: 'item_kit_sword', kind: 'item', name: 'smoke sword', input: 'params {} (preset: sword, medieval, seed 1)', source: { type: 'params', params: {} } },
  { id: 'item_from_mesh', preset: 'item_from_mesh', kind: 'item', name: 'smoke kit item', input: 'item.glb of item_kit_sword', fromCase: 'item_kit_sword', needs: ['item_kit_sword'] },
  { id: 'ship_procedural-1', preset: 'ship_procedural', kind: 'ship', name: 'smoke ship 1', input: 'params {seed: 1}', source: { type: 'params', params: { seed: 1 } } },
  { id: 'ship_procedural-2', preset: 'ship_procedural', kind: 'ship', name: 'smoke ship 2', input: 'params {seed: 2}', source: { type: 'params', params: { seed: 2 } } },
  { id: 'ship_from_mesh', preset: 'ship_from_mesh', kind: 'ship', name: 'smoke ship mesh', file: IN.ship },
  { id: 'turret', preset: 'turret', kind: 'ship', name: 'smoke turret', input: 'preset params (M, 2 barrels)', source: { type: 'params', params: {} } },
  { id: 'creature_auto', preset: 'creature_auto', kind: 'creature', name: 'smoke creature', file: IN.creature, ...(CREATURE.height_m ? { profile: { height_m: CREATURE.height_m }, label: `profile.height_m ${CREATURE.height_m}` } : {}) },
  { id: 'humanoid_from_mesh', preset: 'humanoid_from_mesh', kind: 'humanoid', name: 'smoke humanoid', file: IN.humanoid, needs: ['clips'] },
  { id: 'humanoid_sprites', preset: 'humanoid_sprites', kind: 'humanoid', name: 'smoke humanoid sprites', file: IN.humanoid, needs: ['clips'] },
  // diagnostic: rig_humanoid alone on the raw input (no cleanup)
  {
    id: 'rig_humanoid-raw', preset: 'rig_humanoid', kind: 'humanoid', name: 'smoke humanoid raw', file: IN.humanoid,
    steps: [{ step: 'rig_humanoid', params: { height_m: 1.8 } }],
    label: 'diagnostic: rig_humanoid only, no cleanup',
  },
  // new steps registered 10.10 (evening): bone_kit generator and replace_part with inputs.part
  {
    id: 'bone_kit-hand', preset: 'bone_kit', kind: 'item', name: 'smoke bone hand', input: 'params {part: skeleton_hand, side: l, pose: grip, rings: [index, middle]}',
    source: { type: 'params', params: { part: 'skeleton_hand', side: 'l', pose: 'grip', rings: ['index', 'middle'] } },
    steps: [{ step: 'bone_kit' }, { step: 'godot_package', params: { profile: 'godot' } }], label: 'bone_kit → godot_package',
  },
  {
    id: 'bone_kit-staff', preset: 'bone_kit', kind: 'item', name: 'smoke bone staff', input: 'params {part: bone_staff, seed: 1}',
    source: { type: 'params', params: { part: 'bone_staff', seed: 1 } },
    steps: [{ step: 'bone_kit' }, { step: 'godot_package', params: { profile: 'godot' } }], label: 'bone_kit → godot_package',
  },
  {
    id: 'replace_part', preset: 'replace_part', kind: 'humanoid', name: 'smoke part', file: IN.humanoid, partFile: PART.region ? IN.part : [],
    steps: [{ step: 'replace_part', params: { height_m: 1.8, region: PART.region, cut_plane: PART.cut_plane || { axis: 'z', value: 1.5 }, part_tris: 20000, eyes: { auto: true } } }],
    label: 'part.glb + part.json',
  },
];

function parseArgs(argv) {
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (!a.startsWith('--')) continue;
    const eq = a.indexOf('=');
    if (eq > 0) flags[a.slice(2, eq)] = a.slice(eq + 1);
    else if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) flags[a.slice(2)] = argv[++i];
    else flags[a.slice(2)] = true;
  }
  return flags;
}

const secondsBetween = (a, b) => (a && b ? Math.round((Date.parse(b) - Date.parse(a)) / 100) / 10 : null);
const firstExisting = (p) => (Array.isArray(p) ? p.find((x) => existsSync(x)) || null : (existsSync(p) ? p : null));
const short = (p) => path.relative(INPUTS, String(p || '')).replace(/\\/g, '/') || String(p || '');

/** JSON chunk of a .glb (no Blender). */
async function readGlbJson(file) {
  const fd = await fs.open(file, 'r');
  try {
    const head = Buffer.alloc(20);
    await fd.read(head, 0, 20, 0);
    if (head.toString('latin1', 0, 4) !== 'glTF') return null;
    const len = head.readUInt32LE(12);
    const body = Buffer.alloc(len);
    await fd.read(body, 0, len, 20);
    return JSON.parse(body.toString('utf8'));
  } finally {
    await fd.close();
  }
}

async function glbFacts(file) {
  if (typeof file !== 'string' || !existsSync(file)) return null;
  const j = await readGlbJson(file).catch(() => null);
  if (!j) return null;
  const nodes = (j.nodes || []).map((n) => n?.name || '');
  return {
    animations: (j.animations || []).map((a) => a?.name || ''),
    joints: j.skins?.[0]?.joints?.length || 0,
    rootBone: (j.skins || []).some((s) => (s.joints || []).some((ji) => /^root$/i.test(nodes[ji] || ''))),
    sockets: (j.nodes || []).filter((n) => n?.extras?.game_socket || /^socket_/.test(n?.name || '')).map((n) => n.extras?.game_socket || n.name),
    pivots: nodes.filter((n) => /^pivot_/.test(n)),
    meshes: (j.meshes || []).length,
  };
}

const fmt = (v, d = 2) => (typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toFixed(d)) : String(v ?? '—'));

/** Short human facts per step (the full result stays in the JSON). */
async function stepFacts(s) {
  const r = s.result || {};
  const st = r.stats || {};
  const out = r.outputs || {};
  const checks = Array.isArray(r.checks) ? r.checks : [];
  const ck = (name) => checks.find((c) => c?.name === name);
  const f = [];
  switch (s.step) {
    case 'cleanup': f.push(`tris ${fmt(st.tris)}`, `h ${fmt(st.height_m, 3)} m`); break;
    case 'rig_humanoid': {
      const bones = ck('bones');
      if (bones) f.push(`bones ${fmt(bones.value)}`);
      const props = (st.rigid || []).filter((x) => x?.kind === 'prop').map((x) => `${String(x.bone || '').replace('mixamorig:', '')}`);
      f.push(`props ${props.length ? props.join(', ') : 'none'}`);
      if (st.pose_class) f.push(`pose ${st.pose_class}`);
      if (st.landmark_confidence !== undefined) f.push(`conf ${fmt(st.landmark_confidence)}`);
      const torn = ck('deform_torn_area');
      if (torn) f.push(`torn ${fmt(torn.value, 4)}/${fmt(torn.limit, 4)}`);
      break;
    }
    case 'retarget': {
      const g = await glbFacts(out.glb);
      if (g) f.push(`anims ${g.animations.join(', ')}`, `joints ${g.joints}`, g.rootBone ? 'root bone' : 'no root bone');
      const pc = st.per_clip || {};
      const skate = Object.values(pc).map((c) => c?.foot_skate_cm).filter((x) => typeof x === 'number');
      if (skate.length) f.push(`max skate ${fmt(Math.max(...skate), 1)} cm`);
      const modes = [...new Set(Object.values(pc).map((c) => c?.root_motion).filter(Boolean))];
      if (modes.length) f.push(`motion ${modes.join('/')}`);
      break;
    }
    case 'rig_creature': f.push(`template ${st.template}`, `size ${(st.size || []).map((v) => fmt(v)).join('×')} m`, `bones ${fmt(st.bones)}`, `weights ${st.weights_mode}`, `conf ${fmt(st.confidence)}`); break;
    case 'bone_kit': f.push(`${st.part || '?'} tris ${fmt(st.tris)}/${fmt(st.budget_tris)}`, `bones ${fmt(st.bones ?? (st.armature_bones || []).length)}`, `sockets ${(st.sockets || []).map((x) => x?.name || x).join(', ')}`); break;
    case 'replace_part': f.push(`tris ${fmt(st.tris_before)}→${fmt(st.tris_after)}`, `align rms ${fmt(st.alignment?.rms_rel, 4)}`, `seam ${st.seam?.bridged ? 'bridged' : 'open'} (${fmt(st.seam?.open_seam_edges)} open)`, `eyes ${st.eyes?.found ?? (st.eyes?.l ? 'found' : '—')}`); break;
    case 'anim_creature': f.push(`clips ${(st.clips || []).join(', ')}`, `slide ${fmt(st.max_foot_slide_cm)} cm`); break;
    case 'item_kit': f.push(`${st.type} tris ${fmt(st.tris)}/${fmt(st.budget_tris)}`, `sockets ${(st.sockets || []).join(', ')}`); break;
    case 'item_from_mesh': f.push(`kind ${st.kind}`, `tris ${fmt(st.tris)}/${fmt(st.budget_tris)}`); break;
    case 'ship_gen': f.push(`tris ${fmt(st.tris)}`, `layout ${st.layout?.family || '?'}`, `engines ${fmt(st.engines_main)}`, `sockets ${(st.sockets || []).length}`); break;
    case 'turret_gen': {
      const g = await glbFacts(out.glb);
      f.push(`tris ${fmt(st.tris)}`, `clips ${(st.clips || []).map((c) => c?.name || c).join(', ')}`);
      if (g) f.push(`pivots ${g.pivots.join(', ') || 'none'}`);
      break;
    }
    case 'ship_from_mesh': {
      const o = st.orientation || {};
      f.push(`tris ${fmt(st.input_tris)}→${fmt(st.tris)}`, `nose ${o.nose ?? o.nose_axis ?? o.nose_from ?? '?'} (${fmt(o.nose_confidence ?? o.confidence)})`, `sockets ${(st.sockets || []).length}`);
      const lods = Object.entries(st.lod_decimation || {}).map(([k, v]) => `${k} ${fmt(v.tris)}/${fmt(v.target)} ${v.method}`);
      if (lods.length) f.push(lods.join(', '));
      const sil = st.lod_silhouette_iou?.lod2;
      if (sil) f.push(`lod2 IoU ${fmt(sil.top, 3)}/${fmt(sil.side, 3)}`);
      if (out.lod_strip) f.push('lod_strip');
      break;
    }
    case 'render_sprites': f.push(`${st.engine || ''} ${fmt(st.frames_rendered)} frames`, `cell ${(st.cell || []).join('x')}`, `body ${fmt(st.body_px, 0)} px`, `forward ${st.forward}`); break;
    case 'godot_package': {
      const v = ck('godot_validation');
      f.push(`Godot ${st.godot_version || '?'} ${v ? (v.ok ? 'ok' : 'FAIL') : 'not run'}`, `sockets ${fmt(st.sockets)}`, `clips ${fmt(st.clips)}`);
      if (st.clip_renames && Object.keys(st.clip_renames).length) f.push(`renamed ${Object.entries(st.clip_renames).map(([a, b]) => `${a}→${b}`).join(', ')}`);
      if (st.sprites || s.request?.inputs?.sprites) f.push('sprites in package');
      if (st.fx) f.push(`fx ${st.fx}`);
      break;
    }
    case 'clip_library': {
      const idx = typeof out.index === 'string' && existsSync(out.index) ? JSON.parse(await fs.readFile(out.index, 'utf8')) : null;
      if (idx) f.push(`clips ${idx.clips.map((c) => `${c.id} (${c.category})`).join(', ')}`);
      break;
    }
    default: break;
  }
  return f.filter(Boolean);
}

/** Last steps/<file>.py frame of a Python trace: the suspected module. */
function suspectedFile(step, trace) {
  const frames = [...String(trace || '').matchAll(/File "([^"]*scripts[\\/]blender[\\/][^"]+\.py)", line (\d+)/g)];
  const hit = frames.reverse().find((m) => !/af_run\.py/.test(m[1]));
  if (hit) return `${hit[1].replace(/\\/g, '/').replace(/^.*?(scripts\/blender\/)/, '$1')}:${hit[2]}`;
  return `scripts/blender/steps/${step}.py`;
}

/** A case meets its expectation: ok, or (expect 'fail…') failed — at the expected check when expectMatch is given. */
function expectedOutcome(c, job, status) {
  if (!c.expect) return status === 'completed';
  if (!c.expect.startsWith('fail')) return status === 'completed';
  if (status !== 'failed') return false;
  if (!c.expectMatch) return true;
  const text = JSON.stringify((job?.steps || []).filter((s) => s.status === 'failed').map((s) => [s.result?.error, (s.result?.checks || []).filter((x) => x && x.ok === false).map((x) => x.name)]));
  return text.includes(c.expectMatch);
}

async function summarize(c, job, extra = {}) {
  const steps = [];
  for (const s of job?.steps || []) {
    const r = s.result || {};
    const checks = Array.isArray(r.checks) ? r.checks : [];
    steps.push({
      step: s.step,
      status: s.status,
      seconds: secondsBetween(s.startedAt, s.finishedAt),
      error: s.status === 'failed' ? (r.error || s.message || null) : null,
      af_error: r.af_error || null,
      suspected: s.status === 'failed' && s.step !== 'clip_library' ? suspectedFile(s.step, r.trace) : null,
      failedChecks: checks.filter((x) => x && x.ok === false && (x.severity || 'error') === 'error').map((x) => ({ name: x.name, value: x.value, limit: x.limit, note: x.note || null })),
      warnChecks: checks.filter((x) => x && x.ok === false && x.severity === 'warning').map((x) => x.name),
      warnings: (s.warnings || []).slice(0, 20),
      facts: ['completed', 'failed'].includes(s.status) ? await stepFacts(s) : [],
      outputs: r.outputs || {},
      logFile: s.logFile || null,
    });
  }
  const status = job?.status || 'not_run';
  return {
    id: c.id,
    preset: c.preset ? (c.label ? `${c.preset} (${c.label})` : c.preset) : (c.library ? 'clip_library' : null),
    input: c.inputLabel || c.input || null,
    expect: c.expect || null,
    status,
    asExpected: expectedOutcome(c, job, status),
    seconds: secondsBetween(job?.startedAt, job?.finishedAt),
    assetId: job?.assetId || null,
    jobId: job?.id || null,
    version: job?.version || null,
    error: job?.error || null,
    jobWarnings: job?.warnings || [],
    steps,
    ...extra,
  };
}

function keyChecks(row) {
  return row.steps.filter((s) => s.facts.length).map((s) => `${s.step}: ${s.facts.join('; ')}`).join('<br>') || '—';
}

function failureCell(row) {
  const parts = [];
  for (const s of row.steps) {
    if (s.status === 'failed') {
      const checks = s.failedChecks.map((c) => `${c.name}${c.value !== undefined && c.value !== null ? `=${typeof c.value === 'object' ? JSON.stringify(c.value).slice(0, 60) : fmt(c.value, 4)}` : ''}${c.limit !== undefined && c.limit !== null ? ` (≤ ${typeof c.limit === 'object' ? JSON.stringify(c.limit).slice(0, 40) : c.limit})` : ''}`);
      parts.push(`**${s.step}**: ${(s.af_error || s.error || '').replace(/\|/g, '\\|').slice(0, 300)}${checks.length ? ` — ${checks.join(', ')}` : ''}`);
    } else if (s.warnChecks.length) {
      parts.push(`${s.step} (warn): ${s.warnChecks.slice(0, 6).join(', ')}${s.warnChecks.length > 6 ? '…' : ''}`);
    }
  }
  if (!parts.length && row.error) parts.push(row.error.replace(/\|/g, '\\|').slice(0, 300));
  return parts.join('<br>') || '—';
}

function renderMd(summary) {
  const L = [];
  L.push('# Forge: smoke run of all presets (real Blender and Godot)');
  L.push('');
  L.push(`Script \`scripts/forge-smoke.mjs\` (outside \`npm test\`), the same pipeline engine as \`node scripts/forge.mjs --direct\`.`);
  L.push(`Run: ${summary.startedAt} → ${summary.finishedAt}. Blender ${summary.blender?.version || '?'} (${summary.blender?.path || '?'}), Godot ${summary.godot?.version || '?'} (${summary.godot?.path || '?'}).`);
  L.push(`Work folder (short path because of MAX_PATH 260): \`${summary.root}\` — \`lib/\` forge library, \`rt/\` Blender logs. JSON summary: \`${summary.jsonPath}\`.`);
  L.push('');
  L.push('| Preset | Input | Status | s | Key checks | Errors and warnings |');
  L.push('|---|---|---|---|---|---|');
  for (const r of summary.cases) {
    const st = r.status === 'completed' ? 'ok' : r.status;
    const mark = r.expect ? (r.asExpected ? ` (as expected: ${r.expect})` : ` (**not as expected**: ${r.expect})`) : '';
    L.push(`| ${r.id} | ${String(r.input || '').replace(/\|/g, '\\|')} | ${st}${mark} | ${fmt(r.seconds, 1)} | ${keyChecks(r)} | ${failureCell(r)} |`);
  }
  L.push('');
  const failed = summary.cases.filter((r) => r.status !== 'completed');
  if (failed.length) {
    L.push('## Failures: step, step error text, suspected file');
    L.push('');
    for (const r of failed) {
      for (const s of r.steps.filter((x) => x.status === 'failed')) {
        L.push(`- **${r.id} → ${s.step}** (${r.expect && r.asExpected ? 'expected' : 'unexpected'}): ${s.af_error || s.error}`);
        if (s.failedChecks.length) L.push(`  - failed checks: ${s.failedChecks.map((c) => `\`${c.name}\` = ${JSON.stringify(c.value)} (limit ${JSON.stringify(c.limit)})${c.note ? ` — ${String(c.note).slice(0, 240)}` : ''}`).join('; ')}`);
        if (s.suspected) L.push(`  - suspected file: \`${s.suspected}\``);
        if (s.logFile) L.push(`  - log: \`${s.logFile}\``);
      }
      if (!r.steps.some((x) => x.status === 'failed') && r.error) L.push(`- **${r.id}**: ${r.error}`);
    }
    L.push('');
  }
  if (summary.notes?.length) {
    L.push('## Notes');
    L.push('');
    for (const n of summary.notes) L.push(`- ${n}`);
    L.push('');
  }
  return L.join('\n');
}

async function prepareRoot(root) {
  const marker = path.join(root, '.af-smoke');
  if (existsSync(root) && !existsSync(marker)) {
    const entries = await fs.readdir(root);
    if (entries.length) throw new Error(`Folder ${root} exists and was not created by the smoke run (no .af-smoke) — choose another with --root.`);
  }
  await fs.mkdir(root, { recursive: true });
  await fs.writeFile(marker, 'AstraForge forge smoke work folder (scripts/forge-smoke.mjs); safe to delete.\n');
  for (const sub of ['lib', 'rt']) await fs.rm(path.join(root, sub), { recursive: true, force: true, maxRetries: 3 });
}

async function main() {
  const flags = parseArgs(process.argv.slice(2));
  const root = path.resolve(flags.root && flags.root !== true ? String(flags.root) : (process.platform === 'win32' ? 'C:/afs' : path.join(os.tmpdir(), 'afs')));
  const mdPath = path.resolve(ROOT, flags.md && flags.md !== true ? String(flags.md) : 'docs/forge/SMOKE.md');
  const jsonPath = path.resolve(ROOT, flags.json && flags.json !== true ? String(flags.json) : 'docs/forge/SMOKE.json');
  const only = flags.only && flags.only !== true ? String(flags.only).split(',').map((s) => s.trim()).filter(Boolean) : null;
  if (only) for (const id of only) if (!CASES.some((c) => c.id === id)) throw new Error(`No case "${id}". Available: ${CASES.map((c) => c.id).join(', ')}`);

  await prepareRoot(root);
  const { ForgeEngine } = await import('../server/forge-pipeline.mjs');
  const { blenderInfo, godotInfo } = await import('../server/blender.mjs');
  const engine = new ForgeEngine({
    root: ROOT, libraryDir: path.join(root, 'lib'), runtimeDir: path.join(root, 'rt'), host: 'smoke', allowLocalPaths: true,
  });
  await engine.init();
  const [blender, godot] = await Promise.all([blenderInfo(), godotInfo()]);
  console.log(`Blender: ${blender.ok ? blender.version : 'none'} ${blender.path}`);
  console.log(`Godot:   ${godot.ok ? godot.version : 'none'} ${godot.path}`);
  if (!blender.ok) throw new Error(blender.error || 'Blender not found');
  const notes = [];
  if (!godot.ok || !String(godot.version).startsWith('4.5.1')) notes.push(`Godot for checks: ${godot.version || 'not found'} (4.5.1 required; ASTRA_GODOT=${process.env.ASTRA_GODOT || '—'}).`);

  // the cases to run: the requested ones plus what they need, in the CASES order
  const want = new Set(only || CASES.map((c) => c.id));
  for (let grow = true; grow;) {
    grow = false;
    for (const c of CASES) if (want.has(c.id)) for (const n of c.needs || []) if (!want.has(n)) { want.add(n); grow = true; }
  }
  const plan = CASES.filter((c) => want.has(c.id));

  let current = null;
  let stopping = false;
  process.on('SIGINT', () => {
    if (stopping) process.exit(130);
    stopping = true;
    console.error('\nStopping: cancelling the current job…');
    if (current) void engine.cancelJob(current).catch(() => {});
  });

  const startedAt = new Date().toISOString();
  const results = new Map();
  const assets = new Map();
  for (const c of plan) {
    if (stopping) break;
    const t0 = Date.now();
    console.log(`\n=== ${c.id} ${c.preset ? `(${c.preset})` : ''}`);
    try {
      if ((c.needs || []).some((n) => results.get(n)?.status !== 'completed')) {
        const missing = c.needs.filter((n) => results.get(n)?.status !== 'completed');
        results.set(c.id, await summarize(c, null, { status: 'skipped', error: `Skipped: required case failed: ${missing.join(', ')}.` }));
        continue;
      }
      let jobId;
      if (c.library) {
        if (!existsSync(c.input)) {
          results.set(c.id, await summarize(c, null, { status: 'skipped', error: `Skipped: no clip folder ${c.input}.` }));
          continue;
        }
        jobId = (await engine.importClips({ folder: c.input })).jobId;
      } else {
        let asset;
        const file = c.file ? firstExisting(c.file) : null;
        if (c.file && !file) {
          results.set(c.id, await summarize(c, null, { status: 'skipped', error: `Skipped: no input file ${[].concat(c.file).join(' | ')}.` }));
          continue;
        }
        if (file) {
          c.inputLabel = short(file) + (c.label ? ` (${c.label})` : "");
          asset = await engine.createAsset({ kind: c.kind, name: c.name, source: { type: 'upload' }, ...(c.profile ? { profile: c.profile } : {}) }, { path: file, originalName: path.basename(file) });
        } else if (c.fromCase) {
          const from = assets.get(c.fromCase);
          asset = await engine.createAsset({ kind: c.kind, name: c.name, source: { type: 'asset', assetId: from } });
        } else {
          asset = await engine.createAsset({ kind: c.kind, name: c.name, source: c.source });
        }
        assets.set(c.id, asset.id);
        let steps = c.steps;
        if (c.partFile) {
          // replace_part: the part file goes in by path (CLI --direct); the case is skipped without it
          const part = firstExisting(c.partFile);
          if (!part) {
            results.set(c.id, await summarize(c, null, { status: 'skipped', error: `Skipped: part file missing (${[].concat(c.partFile).join(' | ')}).` }));
            continue;
          }
          c.inputLabel = `${c.inputLabel} + ${short(part)}`;
          steps = steps.map((st) => (st.step === 'replace_part' ? { ...st, params: { ...st.params, part_file: part } } : st));
        }
        const run = await engine.startRun(asset.id, steps ? { steps } : { preset: c.preset });
        if (run.warnings?.length) console.log(`  run warnings: ${run.warnings.join(' | ')}`);
        jobId = run.jobId;
      }
      current = jobId;
      const seen = new Map();
      const timer = setInterval(async () => {
        const j = await engine.getJob(jobId).catch(() => null);
        for (const s of j?.steps || []) {
          const key = `${s.status}`;
          if (seen.get(s.index) !== key) {
            seen.set(s.index, key);
            console.log(`  [${Math.round((Date.now() - t0) / 1000)} s] ${s.step}: ${s.status}${s.status === 'failed' ? ` — ${String(s.result?.error || s.message).slice(0, 400)}` : ''}`);
          }
        }
      }, 2000);
      const job = await engine.waitJob(jobId);
      clearInterval(timer);
      current = null;
      const row = await summarize(c, JSON.parse(JSON.stringify(job)));
      results.set(c.id, row);
      console.log(`  → ${row.status} in ${fmt(row.seconds, 1)} s${row.error ? `: ${row.error.slice(0, 400)}` : ''}`);
    } catch (err) {
      current = null;
      results.set(c.id, await summarize(c, null, { status: 'error', error: err.message }));
      console.log(`  → error: ${err.message}`);
    }
  }

  // merge with an earlier full run when only a subset ran now
  let previous = [];
  if (only && existsSync(jsonPath)) {
    try { previous = JSON.parse(await fs.readFile(jsonPath, 'utf8')).cases || []; } catch { previous = []; }
  }
  const cases = CASES.map((c) => results.get(c.id) || previous.find((p) => p.id === c.id)).filter(Boolean);
  const summary = {
    startedAt, finishedAt: new Date().toISOString(), root: root.replace(/\\/g, '/'), jsonPath: path.relative(ROOT, jsonPath).replace(/\\/g, '/'),
    blender: { path: blender.path, version: blender.version }, godot: { path: godot.path, version: godot.version },
    only, notes, cases,
  };
  await fs.mkdir(path.dirname(jsonPath), { recursive: true });
  await fs.writeFile(jsonPath, JSON.stringify(summary, null, 2));
  await fs.writeFile(mdPath, renderMd(summary));
  console.log(`\nDone: ${cases.filter((r) => r.status === 'completed').length}/${cases.length} ok. ${path.relative(ROOT, mdPath)}, ${path.relative(ROOT, jsonPath)}`);
  return cases.every((r) => r.asExpected) ? 0 : 1;
}

main().then((code) => process.exit(code), (err) => {
  console.error(`Error: ${err.message}`);
  process.exit(2);
});
