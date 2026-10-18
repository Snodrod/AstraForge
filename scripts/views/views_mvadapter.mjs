// Local provider: MV-Adapter (image -> multi-view) inside AstraForge's own ComfyUI.
// One front image in, the other views out; SDXL base + mvadapter_i2mv_sdxl.safetensors, all local files.
import { promises as fs, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SLOT_META } from './views_prompts.mjs';
import { DATA, DEFAULT_COMFY_URL, requireNodes, uploadImage, runWorkflow, downloadOutput, runPython } from './comfy_client.mjs';
import { withEngineGpu } from '../../server/gpu-handover.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const MVA_DIR = path.join(DATA, 'models', 'mvadapter');
export const MVA_WEIGHTS = 'mvadapter_i2mv_sdxl.safetensors';
export const MVA_CHECKPOINT = 'sd_xl_base_1.0.safetensors';
const PROMPT = 'high quality, best quality';
const NEGATIVE = 'watermark, ugly, deformed, noisy, blurry, low contrast';

/**
 * Largest square size that fits a 12 GB card next to the desktop. Measured peaks (RTX 4070):
 * 4 views 512 px -> 8.7 GB, 640 px -> 9.7 GB, 768 px -> 11.0 GB and the card starts to page (77 s for 3 steps).
 */
export function safeSize(size, numViews) {
  const wanted = Math.max(512, Math.min(768, Math.round((Number(size) || 640) / 64) * 64));
  if (numViews > 4) return Math.min(wanted, 512);
  return Math.min(wanted, 640);
}

export function mvadapterWorkflow({ refName, azimuths, size, steps, cfg, seed, prefix, prompt = PROMPT, negative = NEGATIVE, checkpoint = MVA_CHECKPOINT }) {
  return {
    1: { class_type: 'LoadImage', inputs: { image: refName } },
    2: {
      class_type: 'AstraMVAdapterViews',
      inputs: {
        reference: ['1', 0], ckpt_name: checkpoint, azimuths: azimuths.join(','), width: size, height: size, steps, cfg, seed,
        prompt, negative_prompt: negative, offload: 'text+vae', adapter_dir: MVA_DIR, adapter_name: MVA_WEIGHTS,
      },
    },
    3: { class_type: 'SaveImage', inputs: { images: ['2', 0], filename_prefix: prefix } },
  };
}

/** {ready, reasons[]} — what the UI shows next to the "locally" option. */
export async function mvadapterStatus(comfyUrl = DEFAULT_COMFY_URL) {
  const reasons = [];
  if (!existsSync(path.join(MVA_DIR, MVA_WEIGHTS))) reasons.push('missing weights ' + MVA_WEIGHTS + ' in data/models/mvadapter');
  if (!existsSync(path.join(MVA_DIR, 'sdxl-config', 'model_index.json'))) reasons.push('missing sdxl-config folder (scripts/views/make_sdxl_config.py)');
  let engine = false;
  try {
    const r = await fetch(comfyUrl + '/object_info/AstraMVAdapterViews', { signal: AbortSignal.timeout(4000) });
    const v = r.ok ? await r.json() : null;
    engine = !!v?.AstraMVAdapterViews;
    const ckpts = v?.AstraMVAdapterViews?.input?.required?.ckpt_name?.[0] || [];
    if (engine && !ckpts.includes(MVA_CHECKPOINT)) reasons.push('missing ' + MVA_CHECKPOINT);
  } catch { /* engine offline */ }
  if (!engine) reasons.push('the 3D engine is not running or does not know the MV-Adapter node (restart with Stop.cmd / Start.cmd)');
  return { ready: reasons.length === 0, reasons };
}

/**
 * Generate views from the front image.
 * @param {{front:string, slots:string[], workDir:string, comfyUrl?:string, size?:number, steps?:number, cfg?:number, seed?:number,
 *          signal?:AbortSignal, onProgress?:(s:string)=>void}} o
 */
export async function generateMvadapterViews(o) {
  const comfyUrl = o.comfyUrl || DEFAULT_COMFY_URL;
  const steps = Math.max(8, Math.min(60, Number(o.steps) || 30));
  const cfg = Number(o.cfg) || 3;
  const seed = Number.isFinite(Number(o.seed)) ? Number(o.seed) : 21;
  const wanted = [...new Set(o.slots)].filter((s) => s !== 'front' && SLOT_META[s]);
  if (!wanted.length) throw new Error('No views selected.');
  // azimuth 0 always rides along: the views attend to each other, and the generated front keeps them coherent
  const order = ['front', 'frontLeft', 'left', 'back', 'right', 'frontRight'];
  const slots = order.filter((s) => s === 'front' || wanted.includes(s));
  const azimuths = slots.map((s) => SLOT_META[s].azimuth);
  let size = safeSize(o.size, slots.length);
  const status = await mvadapterStatus(comfyUrl);
  if (!status.ready) throw new Error('Local MV-Adapter is not ready: ' + status.reasons.join('; ') + '.');
  await requireNodes(comfyUrl, ['AstraMVAdapterViews', 'LoadImage', 'SaveImage']);
  await fs.mkdir(o.workDir, { recursive: true });
  const refFile = path.join(o.workDir, 'mva_reference.png');
  const prepare = (sz) => runPython(path.join(HERE, 'views_prep.py'), ['mvaref', '--in', o.front, '--out', refFile, '--size', String(sz)], { signal: o.signal });
  o.onProgress?.('Preparing the reference image');
  await prepare(size);
  const started = Date.now();
  const attempt = async (sz) => {
    const refName = await uploadImage(comfyUrl, refFile, 'astra-mva-ref.png');
    const wf = mvadapterWorkflow({ refName, azimuths, size: sz, steps, cfg, seed, prefix: 'astra_views/mva_' + Date.now() });
    const run = await withEngineGpu('hunyuan', () => runWorkflow(comfyUrl, wf, {
      signal: o.signal,
      onProgress: (t) => o.onProgress?.('MV-Adapter · ' + t),
    }), { signal: o.signal, failOpen: true, timeoutMs: 60 * 60 * 1000, urls: { hunyuan: comfyUrl }, onWait: () => o.onProgress?.('Waiting for the GPU to become free'), onStep: (t) => o.onProgress?.(t) });
    return run.outputs;
  };
  o.onProgress?.(`MV-Adapter · ${slots.length} views, ${size}×${size}, ${steps} steps`);
  let outputs;
  try {
    outputs = await attempt(size);
  } catch (e) {
    if (/out of memory|CUDA|alloc/i.test(String(e.message)) && size > 512) {
      size = 512;
      o.onProgress?.('Out of VRAM, retrying at 512×512');
      await prepare(size);
      outputs = await attempt(size);
    } else throw e;
  }
  const images = Object.values(outputs).flatMap((x) => x.images || []);
  if (images.length < slots.length) throw new Error(`MV-Adapter returned ${images.length} images instead of ${slots.length}.`);
  const files = {};
  let frontGenerated;
  for (let i = 0; i < slots.length; i++) {
    const dest = path.join(o.workDir, 'mva_' + slots[i] + '.png');
    await downloadOutput(comfyUrl, images[i], dest);
    if (slots[i] === 'front') frontGenerated = dest; else files[slots[i]] = dest;
  }
  return { files, frontGenerated, seconds: Math.round((Date.now() - started) / 100) / 10, size, steps, seed, azimuths };
}
