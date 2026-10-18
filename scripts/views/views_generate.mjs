#!/usr/bin/env node
// views_generate — from ONE front view of a T-pose character to the set front / left / back / right
// (optionally frontLeft / frontRight), normalised to one framing, plus views.json.
//
// Providers:
//   codex      ChatGPT image model through the local Codex app (best quality, ~1-2 min per view)
//   mvadapter  MV-Adapter in AstraForge's own ComfyUI (offline, one batch of views in ~1 minute)
//   manual     no generation: only the files you supply with --manual slot=path
// User-supplied views (manual) are always kept; generated views fill only the missing slots
// unless `overwrite` is set.
//
// A views folder:  views.json | raw/<slot>.png | norm/norm_<slot>.png (generator input), cut_<slot>.png, thumb_<slot>.jpg | work/
//
//   node scripts/views/views_generate.mjs --input front.png --out <dir> --provider codex|mvadapter|manual
//        [--slots left,back,right] [--manual back=file.png ...] [--seed 21] [--size 640] [--steps 30]
//        [--note "extra details"] [--overwrite] [--comfy http://127.0.0.1:8190]
import { promises as fs, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { SLOTS, SLOT_META, GENERATED_SLOTS, codexPrompt, generationOrder } from './views_prompts.mjs';
import { generateImage, codexStatus } from './codex_image.mjs';
import { generateMvadapterViews, mvadapterStatus } from './views_mvadapter.mjs';
import { runPython, DEFAULT_COMFY_URL } from './comfy_client.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PREP = path.join(HERE, 'views_prep.py');
export const PROVIDERS = ['codex', 'mvadapter', 'manual'];
const now = () => new Date().toISOString();
const round1 = (n) => Math.round(n * 10) / 10;

export const rawPath = (dir, slot) => path.join(dir, 'raw', slot + '.png');

export async function loadViews(dir) {
  try { return JSON.parse(await fs.readFile(path.join(dir, 'views.json'), 'utf8')); } catch (e) {
    if (e.code === 'ENOENT') return null;
    throw e;
  }
}

export async function saveViews(dir, views) {
  views.updatedAt = now();
  const file = path.join(dir, 'views.json');
  const tmp = file + '.' + process.pid + '.tmp';
  await fs.writeFile(tmp, JSON.stringify(views, null, 2));
  await fs.rename(tmp, file);
  return views;
}

async function blankViews(dir) {
  await fs.mkdir(path.join(dir, 'raw'), { recursive: true });
  return { version: 1, createdAt: now(), updatedAt: now(), slots: {}, providers: [], normalized: null };
}

/** Copy any image into raw/<slot>.png (PNG, alpha kept). */
async function storeRaw(dir, slot, source) {
  await fs.mkdir(path.join(dir, 'raw'), { recursive: true });
  const dest = rawPath(dir, slot);
  if (/\.png$/i.test(source)) await fs.copyFile(source, dest);
  else await runPython(path.join(HERE, 'views_prep.py'), ['topng', '--in', source, '--out', dest]);
  return dest;
}

/** Put a user-supplied image into a slot (source: manual). Normalisation is left to the caller (normalizeViews). */
export async function putManualView(dir, slot, file, { name } = {}) {
  if (!SLOTS.includes(slot)) throw new Error('Unknown view: ' + slot);
  const views = (await loadViews(dir)) || (await blankViews(dir));
  await storeRaw(dir, slot, file);
  views.slots[slot] = { source: slot === 'front' ? 'input' : 'manual', file: 'raw/' + slot + '.png', name: name || path.basename(file), addedAt: now() };
  if (views.normalized) views.normalized.stale = true;
  return saveViews(dir, views);
}

export async function removeView(dir, slot) {
  const views = await loadViews(dir);
  if (!views?.slots[slot]) return views;
  if (slot === 'front') throw new Error('The front view cannot be deleted: the others are built from it.');
  await fs.rm(rawPath(dir, slot), { force: true });
  delete views.slots[slot];
  if (views.normalized) views.normalized.stale = true;
  return saveViews(dir, views);
}

/** Re-cut every present view and put them all in one framing (norm/). */
export async function normalizeViews(dir, { signal } = {}) {
  const views = await loadViews(dir);
  if (!views?.slots.front) throw new Error('No front view.');
  const started = Date.now();
  const args = ['normalize', '--out', path.join(dir, 'norm'), '--cache', path.join(dir, 'cache'), '--ref', 'front'];
  const present = SLOTS.filter((s) => views.slots[s] && existsSync(rawPath(dir, s)));
  for (const s of present) args.push('--view', s + '=' + rawPath(dir, s));
  const report = await runPython(PREP, args, { signal, timeoutMs: 15 * 60 * 1000 });
  const files = {};
  for (const s of present) files[s] = { norm: `norm/norm_${s}.png`, cut: `norm/cut_${s}.png`, thumb: `norm/thumb_${s}.jpg` };
  // the finished set, one framing: <dir>/front.png, left.png, back.png, right.png (+ frontLeft / frontRight)
  for (const s of SLOTS) {
    if (files[s]) await fs.copyFile(path.join(dir, files[s].norm), path.join(dir, s + '.png'));
    else await fs.rm(path.join(dir, s + '.png'), { force: true });
  }
  views.normalized = { ...report, files, stale: false, at: now(), seconds: round1((Date.now() - started) / 1000) };
  return saveViews(dir, views);
}

/** Absolute paths of the generator inputs (norm images) for the slots that exist. */
export function normFiles(dir, views, only) {
  const out = {};
  for (const s of only || SLOTS) {
    const f = views.normalized?.files?.[s];
    if (f && existsSync(path.join(dir, f.norm))) out[s] = path.join(dir, f.norm);
  }
  return out;
}

/**
 * @param {object} o
 * @param {string} o.outDir
 * @param {string} [o.input] front image (needed unless outDir already has one)
 * @param {'codex'|'mvadapter'|'manual'} [o.provider]
 * @param {string[]} [o.slots] slots to generate (default left, back, right)
 * @param {Object<string,string>} [o.manual] slot -> file
 * @param {boolean} [o.overwrite] regenerate slots that are already filled
 * @param {number} [o.seed] @param {number} [o.size] @param {number} [o.steps] @param {string} [o.note] @param {string} [o.aspect]
 */
export async function views_generate(o) {
  const dir = path.resolve(o.outDir);
  const provider = o.provider || 'manual';
  if (!PROVIDERS.includes(provider)) throw new Error('Unknown view source: ' + provider);
  const say = (t) => o.onProgress?.(t);
  const timings = {};
  let views = (await loadViews(dir)) || (await blankViews(dir));
  await fs.mkdir(path.join(dir, 'work'), { recursive: true });
  if (o.input) {
    await storeRaw(dir, 'front', path.resolve(o.input));
    views.slots.front = { source: 'input', file: 'raw/front.png', name: path.basename(o.input), addedAt: now() };
  }
  if (!views.slots.front || !existsSync(rawPath(dir, 'front'))) throw new Error('The front view is required first.');
  for (const [slot, file] of Object.entries(o.manual || {})) {
    if (slot === 'front') continue;
    if (!SLOTS.includes(slot)) throw new Error('Unknown view: ' + slot);
    await storeRaw(dir, slot, path.resolve(file));
    views.slots[slot] = { source: 'manual', file: 'raw/' + slot + '.png', name: path.basename(file), addedAt: now() };
  }
  await saveViews(dir, views);

  const requested = (o.slots?.length ? o.slots : ['left', 'back', 'right']).filter((s) => GENERATED_SLOTS.includes(s));
  const manualSlots = new Set(Object.keys(o.manual || {}));
  const todo = requested.filter((s) => !manualSlots.has(s) && (o.overwrite || !views.slots[s]));
  const skipped = requested.filter((s) => !todo.includes(s));
  const errors = {};
  if (provider !== 'manual' && todo.length) {
    const front = rawPath(dir, 'front');
    const stamp = Date.now();
    if (provider === 'codex') {
      const status = await codexStatus();
      if (!status.available) throw new Error(status.message);
      for (const slot of generationOrder(todo)) {
        say(`ChatGPT · drawing view "${slot}"`);
        const hasBack = !!views.slots.back && existsSync(rawPath(dir, 'back'));
        const refs = [front, ...(hasBack && slot !== 'back' ? [rawPath(dir, 'back')] : [])];
        const prompt = codexPrompt(slot, { hasBack: refs.length > 1, note: o.note });
        const out = path.join(dir, 'work', `codex_${slot}_${stamp}.png`);
        try {
          const r = await generateImage({ prompt, refs, aspect: o.aspect || '3:4', outPath: out, timeoutSec: o.timeoutSec || 600, signal: o.signal });
          await fs.copyFile(r.file, rawPath(dir, slot));
          views.slots[slot] = { source: 'codex', provider: 'codex', file: 'raw/' + slot + '.png', seconds: r.seconds, prompt, refs: refs.map((x) => path.basename(x)), generatedAt: now() };
          timings['codex_' + slot] = r.seconds;
          if (views.normalized) views.normalized.stale = true;
          await saveViews(dir, views);
        } catch (e) {
          if (e.name === 'AbortError') throw e;
          errors[slot] = e.message;
          say(`ChatGPT · view "${slot}" failed: ${e.message}`);
        }
      }
    } else {
      say('MV-Adapter · local');
      const r = await generateMvadapterViews({
        front, slots: todo, workDir: path.join(dir, 'work'), comfyUrl: o.comfyUrl || DEFAULT_COMFY_URL,
        size: o.size, steps: o.steps, seed: o.seed, signal: o.signal, onProgress: say,
      });
      for (const [slot, file] of Object.entries(r.files)) {
        await fs.copyFile(file, rawPath(dir, slot));
        views.slots[slot] = {
          source: 'mvadapter', provider: 'mvadapter', file: 'raw/' + slot + '.png', seconds: round1(r.seconds), size: r.size, steps: r.steps, seed: r.seed,
          azimuth: SLOT_META[slot].azimuth,
          generatedAt: now(),
        };
      }
      timings.mvadapter = r.seconds;
      if (r.frontGenerated) await fs.copyFile(r.frontGenerated, path.join(dir, 'work', 'mvadapter_front.png'));
      if (views.normalized) views.normalized.stale = true;
      views.providers.push({ provider, slots: Object.keys(r.files), seconds: r.seconds, size: r.size, steps: r.steps, seed: r.seed, at: now() });
      await saveViews(dir, views);
    }
  }
  say('Normalizing all views to one frame');
  views = await normalizeViews(dir, { signal: o.signal });
  views.lastRun = { provider, requested, generated: todo.filter((s) => !errors[s]), skipped, errors, timings, at: now() };
  await saveViews(dir, views);
  if (todo.length && Object.keys(errors).length === todo.length) throw new Error('Views failed: ' + Object.entries(errors).map(([k, v]) => `${k}: ${v}`).join('; '));
  return views;
}

// ------------------------------------------------------------------ CLI
function parseArgs(argv) {
  const a = { manual: {} };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = () => argv[++i];
    if (k === '--input') a.input = v();
    else if (k === '--out') a.outDir = v();
    else if (k === '--provider') a.provider = v();
    else if (k === '--slots') a.slots = v().split(',').map((s) => s.trim()).filter(Boolean);
    else if (k === '--manual') { const [s, ...p] = v().split('='); a.manual[s] = p.join('='); }
    else if (k === '--seed') a.seed = Number(v());
    else if (k === '--size') a.size = Number(v());
    else if (k === '--steps') a.steps = Number(v());
    else if (k === '--note') a.note = v();
    else if (k === '--aspect') a.aspect = v();
    else if (k === '--comfy') a.comfyUrl = v();
    else if (k === '--overwrite') a.overwrite = true;
    else if (k === '--status') a.status = true;
    else if (k === '--help' || k === '-h') a.help = true;
  }
  return a;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const a = parseArgs(process.argv.slice(2));
  if (a.status) {
    console.log(JSON.stringify({ codex: await codexStatus(), mvadapter: await mvadapterStatus(a.comfyUrl) }, null, 2));
  } else if (a.help || !a.outDir) {
    console.log('node scripts/views/views_generate.mjs --input front.png --out <dir> --provider codex|mvadapter|manual [--slots left,back,right]\n'
      + '  [--manual back=file.png] [--seed 21] [--size 640] [--steps 30] [--note "details"] [--overwrite] [--comfy http://127.0.0.1:8190]\n'
      + 'node scripts/views/views_generate.mjs --status');
  } else {
    try {
      const t0 = Date.now();
      const v = await views_generate({ ...a, onProgress: (t) => console.error('·', t) });
      console.log(JSON.stringify({ outDir: path.resolve(a.outDir), slots: Object.fromEntries(Object.entries(v.slots).map(([k, x]) => [k, x.source])), lastRun: v.lastRun, seconds: round1((Date.now() - t0) / 1000) }, null, 2));
    } catch (e) {
      console.error('Error:', e.message);
      process.exit(1);
    }
  }
}
