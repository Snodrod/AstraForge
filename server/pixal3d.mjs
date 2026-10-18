// Pixal3D as the second 3D engine (docs/forge/pixal3d.md): configuration, "is it installed" check and the lifecycle of the
// separate portable ComfyUI-Pixal3D (127.0.0.1:8191). The studio's own Hunyuan ComfyUI stays on 8190 (server/runtime.mjs).
// Nothing here talks to the GPU: generation is server/pixal3d-run.mjs, the hand-over between the two engines is server/gpu-handover.mjs.
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, openSync, closeSync, promises as fs } from 'node:fs';
import path from 'node:path';
import { root, data } from './storage.mjs';
import { localUrl } from './schema.mjs';

export const DEFAULT_PIXAL3D_DIR = 'C:/AstraForge/ComfyUI-Pixal3D';
export const DEFAULT_PIXAL3D_URL = 'http://127.0.0.1:8191';

/** What has to be on disk for Pixal3D (shape only). [relative path inside the install folder, what it is]. */
export const PIXAL3D_REQUIREMENTS = [
  ['python_embeded/python.exe', 'Python for ComfyUI-Pixal3D'],
  ['ComfyUI/main.py', 'ComfyUI'],
  ['ComfyUI/custom_nodes/ComfyUI-Trellis2/nodes.py', 'Trellis2/Pixal3D nodes'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/ss_flow_img_dit_1_3B_64_bf16.safetensors', 'Pixal3D weights: coarse shape (single view)'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/ss_flow_img_dit_1_3B_64_bf16_mv.safetensors', 'Pixal3D weights: coarse shape (multi-view)'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/slat_flow_img2shape_dit_1_3B_512_bf16.safetensors', 'Pixal3D weights: shape 512 (single view)'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/slat_flow_img2shape_dit_1_3B_512_bf16_mv.safetensors', 'Pixal3D weights: shape 512 (multi-view)'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/slat_flow_img2shape_dit_1_3B_1024_bf16.safetensors', 'Pixal3D weights: shape 1024 (single view)'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/slat_flow_img2shape_dit_1_3B_1024_bf16_mv.safetensors', 'Pixal3D weights: shape 1024 (multi-view)'],
  ['ComfyUI/models/TencentARC/Pixal3D/ckpts/shape_dec_next_dc_f16c32_fp16.safetensors', 'Pixal3D weights: shape decoder'],
  ['ComfyUI/models/facebook/dinov3-vitl16-pretrain-lvd1689m/model.safetensors', 'DINOv3 ViT-L/16'],
  ['ComfyUI/models/Ruicheng/moge-2-vitl/model.pt', 'MoGe-2 (camera from a single view)'],
];

/** Typical numbers shown in the UI and docs (RTX 4070 12 GB, militia, see docs/forge/pixal3d.md). */
export const PIXAL3D_ESTIMATE = { multiSeconds: 232, singleSeconds: 355, vramGb: 9.5 };

function readPathsJson(rootDir) {
  try { return JSON.parse(readFileSync(path.join(rootDir, 'config', 'paths.json'), 'utf8')); } catch { return {}; }
}

/**
 * Where Pixal3D lives: ASTRA_PIXAL3D_DIR, else config/paths.json (key pixal3dDir, written by the installer), else the default folder.
 * The URL (ASTRA_PIXAL3D_URL) must be a local http address; port 8191 by default.
 */
export function pixal3dConfig({ env = process.env, appRoot = root } = {}) {
  const paths = readPathsJson(appRoot);
  let dir = DEFAULT_PIXAL3D_DIR, source = 'default';
  if (env.ASTRA_PIXAL3D_DIR) { dir = env.ASTRA_PIXAL3D_DIR; source = 'env'; }
  else if (typeof paths.pixal3dDir === 'string' && paths.pixal3dDir.trim()) { dir = paths.pixal3dDir.trim(); source = 'paths.json'; }
  let url = DEFAULT_PIXAL3D_URL;
  const wanted = env.ASTRA_PIXAL3D_URL || paths.pixal3dUrl;
  if (wanted) {
    const parsed = localUrl.safeParse(String(wanted).replace(/\/+$/, ''));
    if (!parsed.success) throw new Error('ASTRA_PIXAL3D_URL must be a local http address (for example, http://127.0.0.1:8191).');
    url = parsed.data;
  }
  const port = Number(new URL(url).port) || 80;
  return { dir: path.resolve(dir), url, port, source };
}

/** Disk check only (no network): which required files are missing. */
export function pixal3dInstallState(cfg = pixal3dConfig(), exists = existsSync) {
  if (!exists(cfg.dir)) {
    return { installed: false, missing: [], message: `Pixal3D is not installed: folder not found ${cfg.dir}. Set it in ASTRA_PIXAL3D_DIR or config\\paths.json (key pixal3dDir).` };
  }
  const missing = PIXAL3D_REQUIREMENTS.filter(([rel]) => !exists(path.join(cfg.dir, rel))).map(([rel, label]) => ({ path: rel, label }));
  if (missing.length) {
    const names = missing.slice(0, 3).map((m) => m.label).join('; ') + (missing.length > 3 ? ` and ${missing.length - 3} more` : '');
    return { installed: false, missing, message: `Pixal3D is not fully installed (${cfg.dir}): missing - ${names}. See ASTRAFORGE_README.md in that folder.` };
  }
  return { installed: true, missing: [], message: '' };
}

/**
 * Which engine runs a request: 'hunyuan' (default of the API), 'pixal3d', or 'auto' = Pixal3D when it is installed, else Hunyuan.
 * An explicit 'pixal3d' without an installation is an error with the reason in Russian (code pixal3d_not_installed).
 */
export function resolveEngine(requested = 'hunyuan', cfg = pixal3dConfig(), exists = existsSync) {
  const wanted = requested || 'hunyuan';
  if (!['auto', 'hunyuan', 'pixal3d'].includes(wanted)) throw new Error('Unknown 3D engine: ' + wanted + ' (expected pixal3d, hunyuan or auto).');
  if (wanted === 'hunyuan') return 'hunyuan';
  const state = pixal3dInstallState(cfg, exists);
  if (wanted === 'auto') return state.installed ? 'pixal3d' : 'hunyuan';
  if (!state.installed) throw Object.assign(new Error(state.message), { code: 'pixal3d_not_installed' });
  return 'pixal3d';
}

async function probe(url, ms = 1500, fetchFn = fetch) {
  try {
    const res = await fetchFn(url + '/system_stats', { signal: AbortSignal.timeout(ms) });
    return res.ok;
  } catch { return false; }
}

/** Install state plus "is the ComfyUI on 8191 answering right now". */
export async function pixal3dStatus({ cfg = pixal3dConfig(), fetchFn = fetch, exists = existsSync } = {}) {
  const state = pixal3dInstallState(cfg, exists);
  const running = state.installed ? await probe(cfg.url, 1200, fetchFn) : false;
  return {
    installed: state.installed, running, url: cfg.url, dir: cfg.dir, missing: state.missing, message: state.message,
    startedByStudio: !!child && !child.killed, estimate: PIXAL3D_ESTIMATE,
  };
}

let child = null;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** Environment of "Start ComfyUI (Pixal3D, 8191).bat": caches inside the install, no system CUDA/Python variables. */
export function pixal3dEnv(cfg, base = process.env) {
  const env = { ...base };
  const drop = new Set(['CUDA_PATH', 'CUDA_HOME', 'CUDA_BIN_PATH', 'CUDNN_PATH', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX']);
  for (const k of Object.keys(env)) if (drop.has(k.toUpperCase())) delete env[k];   // Windows names are case-insensitive
  const win = base.windir || base.SystemRoot || 'C:\\Windows';
  const pathKey = Object.keys(env).find((k) => k.toUpperCase() === 'PATH') || 'PATH';
  Object.assign(env, {
    HF_HOME: path.join(cfg.dir, 'cache', 'huggingface'), TORCH_HOME: path.join(cfg.dir, 'cache', 'torch'),
    HF_HUB_DISABLE_TELEMETRY: '1', HF_HUB_DISABLE_SYMLINKS_WARNING: '1', PYTHONUTF8: '1',
    [pathKey]: [path.join(win, 'System32'), path.join(win, 'System32', 'WindowsPowerShell', 'v1.0'), env[pathKey] || ''].join(path.delimiter),
  });
  return env;
}

/**
 * Argument list of the bat file plus two things of ours. --temp-directory is the marker that scripts/Stop.ps1 uses to find the
 * instance the studio started (an instance started by hand or by another program is never stopped by the studio).
 * --database-url gives the studio's instance its own sqlite file: two ComfyUIs of one folder would otherwise fight over
 * ComfyUI/user/comfyui.db (found when a second instance was started on another port).
 */
export function pixal3dArgs(cfg, tempDir, dbFile) {
  const args = ['-I', '-W', 'ignore::FutureWarning', path.join('ComfyUI', 'main.py'), '--windows-standalone-build', '--listen', '127.0.0.1',
    '--port', String(cfg.port), '--disable-auto-launch', '--temp-directory', tempDir];
  if (dbFile) args.push('--database-url', 'sqlite:///' + dbFile.replaceAll('\\', '/'));
  return args;
}
export const RUNTIME_DIR = path.join(data, 'runtime');
export const pixal3dTempDir = (runtimeDir = RUNTIME_DIR) => path.join(runtimeDir, 'pixal3d-temp');
export const pixal3dLogFile = (runtimeDir = RUNTIME_DIR) => path.join(runtimeDir, 'pixal3d.log');

async function logTail(file, lines = 12) {
  try { return (await fs.readFile(file, 'utf8')).split(/\r?\n/).filter(Boolean).slice(-lines).join('\n'); } catch { return ''; }
}

/**
 * Make sure the Pixal3D ComfyUI answers on its port: reuse a running one, otherwise start it hidden and wait for it.
 * @param {object} [o] cfg, signal, onProgress(text), timeoutMs, runtimeDir (log, pid and temp folder; default data/runtime), and for tests: spawnFn, fetchFn, exists, sleep
 * @returns {Promise<{running:true, reused?:true, pid?:number, seconds?:number}>}
 */
export async function startPixal3dRuntime(o = {}) {
  const { cfg = pixal3dConfig(), signal, onProgress, timeoutMs = 240000, spawnFn = spawn, fetchFn = fetch, exists = existsSync, sleep = wait, runtimeDir = RUNTIME_DIR } = o;
  const state = pixal3dInstallState(cfg, exists);
  if (!state.installed) throw Object.assign(new Error(state.message), { code: 'pixal3d_not_installed' });
  if (await probe(cfg.url, 1500, fetchFn)) return { running: true, reused: true };
  if (child && !child.killed && child.exitCode === null) {
    onProgress?.('Pixal3D is already starting');
  } else {
    onProgress?.('Starting the Pixal3D engine (the first start takes 1-2 minutes)');
    const python = path.join(cfg.dir, 'python_embeded', 'python.exe'), tempDir = pixal3dTempDir(runtimeDir), logFile = pixal3dLogFile(runtimeDir);
    await fs.mkdir(tempDir, { recursive: true });   // also creates runtimeDir
    const fd = openSync(logFile, 'a');
    try {
      child = spawnFn(python, pixal3dArgs(cfg, tempDir, path.join(runtimeDir, 'pixal3d.db')), { cwd: cfg.dir, windowsHide: true, detached: true, stdio: ['ignore', fd, fd], env: pixal3dEnv(cfg) });
      child.unref?.();
    } finally { closeSync(fd); }
    const mine = child;
    mine.on?.('error', (e) => { void fs.appendFile(logFile, 'spawn error: ' + e.message + '\n').catch(() => {}); if (child === mine) child = null; });
    mine.on?.('exit', () => { if (child === mine) child = null; });
    await fs.writeFile(path.join(runtimeDir, 'pixal3d.pid'), String(mine.pid || '')).catch(() => {});
  }
  const started = Date.now(), mine = child;
  while (Date.now() - started < timeoutMs) {
    signal?.throwIfAborted();
    if (await probe(cfg.url, 1500, fetchFn)) return { running: true, pid: mine?.pid, seconds: Math.round((Date.now() - started) / 100) / 10 };
    if (!child) {
      const tail = await logTail(pixal3dLogFile(runtimeDir));
      throw new Error('The Pixal3D engine exited on startup. Last lines of data/runtime/pixal3d.log:\n' + tail);
    }
    await sleep(1000);
  }
  throw new Error('The Pixal3D engine did not respond within ' + Math.round(timeoutMs / 1000) + ' s. See data/runtime/pixal3d.log.');
}

/** Stop only the instance this studio process started (called from runtime.mjs on shutdown). */
export function stopOwnedPixal3d() {
  if (!child) return;
  if (process.platform === 'win32') spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }).on('error', () => {});
  else child.kill();
  child = null;
}

/** Test hook: forget the child without killing it. */
export function _resetPixal3dRuntime() { child = null; }
