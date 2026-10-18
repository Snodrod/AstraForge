// Review view: review of the characters built in a batch (docs/forge/REVIEW.md).
//
// GET  /api/forge/review?batch=<name>   one row per character: status in plain Russian, tear QA
//                                        numbers, URLs of concept / paint compare / previews / GLB,
//                                        and the stored decision.
// POST /api/forge/review/export         writes review_decisions.json next to the batch state file.
//
// The batch state file (build.state.json of the batch driver) only says WHICH assets belong to the
// batch and gives their role, faction and concept image. Everything else (status, previews, tear
// numbers) is read from the asset manifests, so a rework run that replaces a step shows up on the
// next refresh even when the state file is stale. The decision itself lives in the manifest
// (qa.decision, qa.comment, qa.game_unit, qa.at) and is written by PATCH /assets/:id.
import path from 'node:path';
import { promises as fs } from 'node:fs';
import { forgeError, isUuid, readJsonFile, writeJsonAtomic } from './forge-pipeline.mjs';
import { listSteps } from './forge-steps.mjs';

export const DECISIONS = ['approved', 'rework', 'rejected'];
export const REVIEW_STATE_ENV = 'ASTRA_FORGE_REVIEW_STATE';
export const REVIEW_ROLES_ENV = 'ASTRA_FORGE_REVIEW_ROLES';
export const REVIEW_DECISIONS_ENV = 'ASTRA_FORGE_REVIEW_DECISIONS';
export const DECISIONS_FILE_NAME = 'review_decisions.json';

const BATCH_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
// Steps whose GLB is not the character itself (the Godot package holds a copy in a nested folder).
const NOT_MODEL_STEPS = new Set(['godot_package']);

const round1 = (n) => Math.round(n * 10) / 10;
const isNum = (n) => typeof n === 'number' && Number.isFinite(n);
const pct = (frac) => (isNum(frac) ? round1(frac * 100) : null);
const fmt = (n) => String(n);

// ---------------------------------------------------------------------------- plain-language reasons

const SIDE = { left: { f: 'left', n: 'left' }, right: { f: 'right', n: 'right' } };

const CHECK_TITLES = {
  cell_fit: 'figure does not fit the frame cell',
  frames_body_inside_cell: 'body goes outside the frame',
  ground_penetration_cm: 'sinks into the ground',
  foot_slide_cm: 'feet slide on the ground',
  foot_skate_cm: 'feet slide on the ground',
  tris_within_target: 'over the triangle budget',
  no_nan_verts: 'mesh has broken vertices',
  height_matches: 'height does not match the target',
  grounded: 'model does not stand on the ground',
  silhouette_iou_min: 'silhouette changed a lot after the step',
};

function rigClause(clause) {
  let m;
  if ((m = clause.match(/^(left|right) arm ([\d.]+) H \(humanoid range ([\d.]+)\.\.([\d.]+)\)/))) {
    return `${SIDE[m[1]].f} arm is ${m[2]} of body height (humanoid range ${m[3]}–${m[4]})`;
  }
  if ((m = clause.match(/^shoulders at ([\d.]+) H \(humanoid range ([\d.]+)\.\.([\d.]+)\)/))) {
    return `shoulders at ${m[1]} of body height (humanoid range ${m[2]}–${m[3]})`;
  }
  if ((m = clause.match(/^(left|right) forearm ([\d.]+) H \(< ([\d.]+)\)/))) {
    return `${SIDE[m[1]].n} forearm is too short (${m[2]} of body height, need at least ${m[3]})`;
  }
  if ((m = clause.match(/^(left|right) leg ([\d.]+) H \(< ([\d.]+)\)/))) {
    return `${SIDE[m[1]].f} leg is too short (${m[2]} of body height, need at least ${m[3]})`;
  }
  if ((m = clause.match(/^(left|right) upper\/forearm ([\d.]+) \(limit ([\d.]+)\.\.([\d.]+)\)/))) {
    return `${m[1]} arm: upper arm and forearm are out of proportion (${m[2]}, allowed ${m[3]}–${m[4]})`;
  }
  if (/arm landmarks are asymmetric/.test(clause)) {
    const hands = clause.match(/mirrored Hand ([\d.]+) H apart \(limit ([\d.]+)\)/);
    return 'arms are asymmetric'
      + (hands ? ` (hands are ${hands[1]} of body height apart, limit ${hands[2]})` : '')
      + (/no hand holds a detected prop/.test(clause) ? ', and no weapon was found in the hand: the pose is off or the prop was not recognized' : '');
  }
  return clause;
}

function checkGroups(names) {
  const groups = new Map();
  for (const raw of names) {
    const [base, ...rest] = raw.split(':');
    const key = base.trim();
    if (!groups.has(key)) groups.set(key, []);
    if (rest.length) groups.get(key).push(rest.join(':').trim());
  }
  return [...groups].map(([key, clips]) => (CHECK_TITLES[key] || key) + (clips.length ? ` (${clips.join(', ')})` : ''));
}

/**
 * Raw error of a failed step (English from Blender/Python, or already Russian) -> one plain-Russian
 * sentence for the card. `titleOf(step)` gives the step title.
 */
export function explainFailure(step, error, titleOf = (s) => s) {
  const title = titleOf(step);
  const text = String(error ?? '').trim();
  if (!text) return `Step "${title}" failed with no explanation.`;
  if (/^Step "/.test(text)) return text.replace(/"([a-z_]+)"/g, (all, id) => `"${titleOf(id)}"`);
  let m;
  if ((m = text.match(/^rig not built:\s*(.*)$/is))) {
    const detail = m[1].trim();
    if ((m = detail.match(/^not a humanoid of supported proportions:\s*(.+)$/is))) {
      const parts = m[1].split(';').map((c) => c.trim()).filter(Boolean).map(rigClause);
      return `Rig not built: the model does not look like a human — ${parts.join('; ')}.`;
    }
    if ((m = detail.match(/^landmarks fail the proportion checks:\s*(.+)$/is))) {
      const parts = m[1].split(';').map((c) => c.trim()).filter((c) => c && !/^correct params\.markers/.test(c)).map(rigClause);
      return `Rig not built: body landmarks failed the proportion checks — ${parts.join('; ')}. Joint points can be corrected by hand.`;
    }
    if ((m = detail.match(/^landmark confidence ([\d.]+) below ([\d.]+)/))) {
      return `Rig not built: head, arms and legs were detected with low confidence (${m[1]}, need ${m[2]}).`;
    }
    if (/^no ground support/.test(detail)) {
      return 'Rig not built: the figure has no ground support — it floats or has no legs instead of standing on two feet.';
    }
    return `Rig not built: ${detail.split(/\r?\n/)[0].slice(0, 200)}`;
  }
  if ((m = text.match(/checks failed:\s*(.+)$/im))) {
    const names = m[1].split(',').map((s) => s.trim().replace(/\.$/, '')).filter(Boolean);
    return `Step "${title}" failed its checks: ${checkGroups(names).join('; ')}.`;
  }
  if (/\btimed? ?out\b|\btimeout\b/i.test(text)) return `Step "${title}" exceeded its time limit and was stopped.`;
  return `Step "${title}" failed: ${text.split(/\r?\n/).find((l) => l.trim())?.slice(0, 200) || text.slice(0, 200)}`;
}

// ---------------------------------------------------------------------------- tear QA

/**
 * stats.deform_qa of the motion_style result -> numbers for the card. Fractions become percent;
 * `ok` is false for a clip over its limit. Null when the step measured nothing.
 */
export function tearSummary(deformQa, deformGate = null) {
  if (!deformQa || typeof deformQa !== 'object') return null;
  const clips = Object.entries(deformQa)
    .filter(([, v]) => v && typeof v === 'object')
    .map(([clip, v]) => ({
      clip,
      tornPct: pct(v.worst_torn_frac),
      robePct: pct(v.worst_robe_frac),
      limitPct: pct(v.worst_limit),
      ok: v.ok !== false,
      frame: v.worst_frame ?? null,
      overFrames: v.over_frames ?? null,
      frames: v.frames_measured ?? null,
      bones: Object.keys(v.torn_by_bone_m2 || {}).slice(0, 4),
    }));
  if (!clips.length) return null;
  const worst = clips.reduce((a, b) => ((b.tornPct ?? -1) > (a.tornPct ?? -1) ? b : a));
  const worstRobe = clips.reduce((a, b) => ((b.robePct ?? -1) > (a.robePct ?? -1) ? b : a));
  return {
    gate: deformGate,
    ok: clips.every((c) => c.ok),
    failedClips: clips.filter((c) => !c.ok).length,
    worst: { clip: worst.clip, tornPct: worst.tornPct, robePct: worst.robePct, limitPct: worst.limitPct },
    worstRobe: worstRobe.robePct !== null ? { clip: worstRobe.clip, robePct: worstRobe.robePct } : null,
    clips,
  };
}

/** The batch driver keeps the same numbers as flat maps in state.qa (fallback for old manifests). */
function tearFromState(qa) {
  if (!qa?.clips_torn_pct || typeof qa.clips_torn_pct !== 'object') return null;
  const deform = {};
  for (const [clip, torn] of Object.entries(qa.clips_torn_pct)) {
    const robe = qa.clips_robe_pct?.[clip];
    deform[clip] = { worst_torn_frac: torn / 100, worst_robe_frac: isNum(robe) ? robe / 100 : null, worst_limit: 0.01, ok: torn <= 1 };
  }
  return tearSummary(deform);
}

function tearReason(tear) {
  const w = tear.worst;
  let text = `Built in full, but the mesh tears: worst clip "${w.clip}" — ${fmt(w.tornPct)}% of the surface`;
  if (isNum(w.limitPct)) text += ` (limit ${fmt(w.limitPct)}%)`;
  if (isNum(tear.worstRobe?.robePct) && tear.worstRobe.robePct > 0) text += `, clothing or cloak — up to ${fmt(tear.worstRobe.robePct)}%`;
  return `${text}. Clips with tearing: ${tear.failedClips} of ${tear.clips.length}.`;
}

// ---------------------------------------------------------------------------- files

/** Path of a result file inside the asset folder as `v001/05_motion_style/preview.png` (or null). */
function relInAsset(id, abs, entry) {
  const p = String(abs || '').replace(/\\/g, '/');
  if (!p) return null;
  const marker = `/assets/${id}/`.toLowerCase();
  const at = p.toLowerCase().lastIndexOf(marker);
  let rel = at >= 0 ? p.slice(at + marker.length) : null;
  // A library moved to another folder leaves old absolute paths: rebuild from the entry.
  if (!rel && entry?.version && entry?.dir) rel = `${entry.version}/${entry.dir}/${p.split('/').pop()}`;
  if (!rel) return null;
  const parts = rel.split('/');
  if (parts.length < 2 || parts.some((part) => !part || part === '.' || part === '..')) return null;
  return parts.join('/');
}

async function isFile(file) {
  try {
    return (await fs.stat(file)).isFile();
  } catch {
    return false;
  }
}

const firstString = (...values) => values.find((v) => typeof v === 'string' && v) || null;

function pickSheet(outputs) {
  const sheets = outputs?.sheets;
  if (!sheets || typeof sheets !== 'object') return null;
  const names = Object.keys(sheets).sort();
  return sheets[names.find((n) => /^idle\.png$/.test(n)) || names.find((n) => !/_(nw|se)\./.test(n)) || names[0]] || null;
}

// ---------------------------------------------------------------------------- the service

export class ForgeReview {
  /**
   * @param {import('./forge-pipeline.mjs').ForgeEngine} engine
   * @param {{stateFile?:string, rolesFile?:string, decisionsFile?:string, env?:object}} [options]
   */
  constructor(engine, options = {}) {
    this.engine = engine;
    this.options = options;
  }

  get env() {
    return this.options.env || process.env;
  }

  /** Russian title of a pipeline step (the registry's own title). */
  titleOf = (step) => {
    this._titles ||= new Map(listSteps().map((s) => [s.step, s.title]));
    return this._titles.get(step) || step;
  };

  /** Resolve the batch: state file, decisions file and name. */
  resolveBatch(batch) {
    const base = this.options.stateFile
      || this.env[REVIEW_STATE_ENV]
      || path.join(path.dirname(this.engine.libraryDir), 'incoming', 'build', 'build.state.json');
    const baseName = path.basename(base).replace(/\.state\.json$/i, '').replace(/\.json$/i, '');
    let name = baseName;
    let stateFile = base;
    const asked = typeof batch === 'string' && batch.trim() ? batch.trim() : null;
    if (asked && asked !== baseName) {
      if (!BATCH_NAME.test(asked)) throw forgeError('Batch name: Latin letters, digits, "_" and "-" (up to 64 characters).', 400, 'validation');
      name = asked;
      stateFile = path.join(path.dirname(path.dirname(base)), asked, `${asked}.state.json`);
    }
    const isBase = name === baseName;
    const decisionsFile = (isBase && (this.options.decisionsFile || this.env[REVIEW_DECISIONS_ENV]))
      || path.join(path.dirname(stateFile), DECISIONS_FILE_NAME);
    const rolesFile = isBase
      ? this.options.rolesFile || this.env[REVIEW_ROLES_ENV] || null
      : null;
    return { name, stateFile, decisionsFile, rolesFile, explicit: !!asked, isBase };
  }

  async _readState(file) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await readJsonFile(file, null);
      } catch (err) {
        // The batch driver may be rewriting the file right now: one more try before giving up.
        if (attempt >= 1) throw forgeError('The batch state file cannot be read (corrupted or being written).', 503, 'bad_state');
        await new Promise((r) => setTimeout(r, 150));
      }
    }
  }

  async _items(batch) {
    const info = this.resolveBatch(batch);
    const state = await this._readState(info.stateFile);
    if (state) {
      const raw = state.items ?? state;
      const list = Array.isArray(raw) ? raw : Object.entries(raw || {}).map(([file, v]) => ({ file, ...v }));
      const roles = info.rolesFile ? await readJsonFile(info.rolesFile, null).catch(() => null) : null;
      let updated = null;
      try { updated = (await fs.stat(info.stateFile)).mtime.toISOString(); } catch { updated = null; }
      return { info, from: 'state', updated, roles, items: list.filter((i) => i && typeof i === 'object') };
    }
    if (info.explicit) throw forgeError(`Batch "${info.name}" not found: no file ${info.stateFile}.`, 404, 'not_found');
    // No batch state: every generation-based character of the library.
    const items = [];
    let names = [];
    try { names = await fs.readdir(this.engine.assetsDir); } catch { names = []; }
    for (const id of names.filter(isUuid)) {
      const m = await readJsonFile(path.join(this.engine.assetsDir, id, 'asset.json'), null).catch(() => null);
      if (m?.id && m.source?.type === 'job' && ['humanoid', 'creature'].includes(m.kind)) items.push({ assetId: m.id, title: m.name });
    }
    items.sort((a, b) => String(a.title).localeCompare(String(b.title)));
    return { info, from: 'library', updated: null, roles: null, items };
  }

  async _conceptUrl(manifest, item) {
    let imageId = firstString(manifest?.source?.image, item.imageId);
    if (!imageId && manifest?.source?.jobId && typeof this.engine._generationJob === 'function') {
      const job = await Promise.resolve(this.engine._generationJob(manifest.source.jobId)).catch(() => null);
      imageId = firstString(job?.imageId);
    }
    if (!imageId || !isUuid(imageId)) return null;
    return (await isFile(path.join(this.engine.studioAssetsDir, `${imageId}.json`))) ? `/api/assets/${imageId}` : null;
  }

  /** URL of a file by its path inside the asset folder, or null when the file is missing. */
  async _relUrl(id, rel) {
    if (!rel || !(await isFile(path.join(this.engine.assetDir(id), ...rel.split('/'))))) return null;
    return `${this.engine.urlBase}/assets/${id}/files/${rel.split('/').map(encodeURIComponent).join('/')}`;
  }

  _fileUrl(id, abs, entry) {
    return this._relUrl(id, relInAsset(id, abs, entry));
  }

  async _row(item, ctx) {
    const title = item.title || item.file || item.assetId || '—';
    const base = { assetId: null, name: title, file: item.file || null };
    const roles = ctx.roles?.[item.file] || {};
    const common = {
      role: item.role ?? roles.role ?? null,
      faction: item.faction ?? roles.faction ?? null,
      handWeapon: item.hand_weapon ?? roles.hand_weapon ?? null,
      notes: roles.notes ?? null,
      build: item.combo?.build ?? roles.build ?? null,
    };
    const empty = { concept: null, compare: null, motion: null, sprites: null, markers: null, poses: null, glb: null };
    const plain = (status, label, tone, reason, extra = {}) => ({
      ...base, ...common, kind: null, stateStatus: item.status || null, status, label, tone, reason,
      failedStep: null, failedStepTitle: null, error: null, tear: null, urls: empty, glbStep: null,
      decision: null, reviewable: false, canApprove: false, version: null, ...extra,
    });
    if (item.status === 'skipped') {
      return plain('skipped', 'Skipped', 'muted', `Skipped when the batch was built: role "${item.role || '—'}" is not handled by the pipeline.`);
    }
    if (!isUuid(item.assetId)) return plain('missing', 'No asset', 'muted', 'The batch has no asset for this character: the build never got as far as creating a passport.');
    const manifest = await readJsonFile(this.engine._manifestPath(item.assetId), null).catch(() => null);
    if (!manifest) {
      return plain('missing', 'No passport', 'muted', 'Asset passport not found: it may have been moved to the trash.', { assetId: item.assetId });
    }
    const id = manifest.id;
    const pipeline = Array.isArray(manifest.pipeline) ? manifest.pipeline : [];
    const latest = new Map();
    for (const entry of pipeline) if (entry?.step) latest.set(entry.step, entry);
    const entryOf = (step) => latest.get(step) || null;
    const outputsOf = (entry) => (entry?.result?.outputs && typeof entry.result.outputs === 'object' ? entry.result.outputs : {});

    // ---- status from the manifest
    const entries = [...latest.values()];
    const running = entries.some((e) => e.status === 'pending');
    const failed = entries.find((e) => e.status === 'failed' && !e.result?.skipped);
    const motion = entryOf('motion_style');
    const tear = tearSummary(motion?.result?.stats?.deform_qa, motion?.result?.stats?.deform_gate) || tearFromState(item.qa);
    const tearBad = tear ? !tear.ok : item.status === 'failed_qa';
    let status;
    let label;
    let tone;
    let reason;
    let failedStep = null;
    let error = null;
    if (running) {
      status = 'running'; label = 'Building'; tone = 'busy'; reason = 'A Forge job is currently running for this asset.';
    } else if (failed) {
      status = 'failed_forge';
      failedStep = failed.step;
      error = firstString(failed.result?.error, item.error);
      label = `Stopped: ${this.titleOf(failed.step)}`;
      tone = 'bad';
      reason = explainFailure(failed.step, error, this.titleOf);
    } else if (!pipeline.length) {
      status = 'new'; label = 'Not built'; tone = 'muted'; reason = 'The passport has no pipeline steps.';
    } else if (latest.get('render_sprites')?.status !== 'done') {
      // Nothing failed, but the pipeline stopped before the sprites: not a finished character.
      status = 'incomplete'; label = 'Incomplete'; tone = 'muted';
      reason = `All completed steps passed, but the pipeline did not reach sprites (last step: "${this.titleOf(pipeline[pipeline.length - 1].step)}").`;
    } else if (tearBad && tear) {
      status = 'failed_qa'; label = 'Mesh tears'; tone = 'warn'; reason = tearReason(tear);
    } else if (tearBad) {
      status = 'failed_qa'; label = 'Mesh tears'; tone = 'warn';
      reason = `Built in full, but the animation check flagged mesh tearing. ${String(item.qa_fail || '').slice(0, 200)}`.trim();
    } else {
      status = 'built'; label = 'Built'; tone = 'good';
      reason = tear ? `All steps passed, mesh tearing within the limit (worst: "${tear.worst.clip}" — ${fmt(tear.worst.tornPct)}%).` : 'All steps passed.';
    }

    // ---- files
    const compareEntry = entryOf('paint_from_concept');
    const rigEntry = entryOf('rig_humanoid') || entryOf('rig_creature');
    const sprites = entryOf('render_sprites');
    const motionOut = outputsOf(motion);
    const conventional = (entry, file) => (entry?.version && entry?.dir ? `${entry.version}/${entry.dir}/${file}` : null);
    const [concept, compare, motionUrl, spritesUrl, markers, poses] = await Promise.all([
      this._conceptUrl(manifest, item),
      this._fileUrl(id, outputsOf(compareEntry).compare, compareEntry),
      this._fileUrl(id, firstString(motionOut.preview, outputsOf(entryOf('retarget')).preview), motion || entryOf('retarget')),
      this._fileUrl(id, firstString(outputsOf(sprites).preview, outputsOf(sprites).preview_frames, pickSheet(outputsOf(sprites))), sprites),
      this._fileUrl(id, firstString(outputsOf(rigEntry).markers_png, conventional(rigEntry, 'markers.png')), rigEntry),
      this._fileUrl(id, firstString(outputsOf(rigEntry).poses_png, conventional(rigEntry, 'poses.png')), rigEntry),
    ]);
    let glb = null;
    let glbStep = null;
    for (let i = pipeline.length - 1; i >= 0 && !glb; i -= 1) {
      const e = pipeline[i];
      const file = e?.status === 'done' && !NOT_MODEL_STEPS.has(e.step) ? outputsOf(e).glb : null;
      if (typeof file === 'string') {
        glb = await this._fileUrl(id, file, e);
        if (glb) glbStep = e.step;
      }
    }
    if (!glb && typeof manifest.source?.file === 'string' && /^source\/[^/]+$/.test(manifest.source.file)) {
      glb = await this._relUrl(id, manifest.source.file);
      if (glb) glbStep = 'source';
    }

    const qa = manifest.qa || {};
    const decision = DECISIONS.includes(qa.decision)
      ? { decision: qa.decision, comment: qa.comment || '', game_unit: qa.game_unit || '', at: qa.at || qa.reviewedAt || null }
      : null;
    return {
      ...base, ...common,
      assetId: id,
      name: manifest.name || title,
      kind: manifest.kind || null,
      stateStatus: item.status || null,
      status, label, tone, reason,
      failedStep, failedStepTitle: failedStep ? this.titleOf(failedStep) : null, error,
      tear,
      urls: { concept, compare, motion: motionUrl, sprites: spritesUrl, markers, poses, glb },
      glbStep,
      version: pipeline.map((e) => e?.version).filter(Boolean).pop() || null,
      decision,
      reviewable: true,
      canApprove: status === 'built' || status === 'failed_qa',
    };
  }

  /** Counters shown above the grid. */
  static counts(items) {
    const c = { total: items.length, reviewable: 0, built: 0, failed_qa: 0, failed_forge: 0, skipped: 0, other: 0, approved: 0, rework: 0, rejected: 0, notReviewed: 0 };
    for (const r of items) {
      if (['built', 'failed_qa', 'failed_forge', 'skipped'].includes(r.status)) c[r.status] += 1;
      else c.other += 1;
      if (!r.reviewable) continue;
      c.reviewable += 1;
      if (r.decision) c[r.decision.decision] += 1;
      else c.notReviewed += 1;
    }
    return c;
  }

  async list({ batch } = {}) {
    const src = await this._items(batch);
    const ctx = { roles: src.roles };
    const items = await Promise.all(src.items.map((item) => this._row(item, ctx)));
    return {
      batch: {
        name: src.info.name,
        source: src.from,
        stateFile: src.from === 'state' ? src.info.stateFile : null,
        stateUpdated: src.updated,
        decisionsFile: src.info.decisionsFile,
        generatedAt: new Date().toISOString(),
      },
      counts: ForgeReview.counts(items),
      items,
    };
  }

  /** Write review_decisions.json: the decided characters only, as a JSON list. */
  async exportDecisions({ batch } = {}) {
    const review = await this.list({ batch });
    const decisions = review.items
      .filter((r) => r.reviewable && r.decision)
      .map((r) => ({
        assetId: r.assetId,
        name: r.name,
        decision: r.decision.decision,
        comment: r.decision.comment,
        game_unit: r.decision.game_unit,
        file: r.file,
        role: r.role,
        faction: r.faction,
        status: r.status,
        failedStep: r.failedStep,
        reason: r.reason,
        at: r.decision.at,
      }));
    const file = this.resolveBatch(batch).decisionsFile;
    await writeJsonAtomic(file, decisions);
    return { ok: true, path: file, count: decisions.length, batch: review.batch.name, counts: review.counts };
  }
}
