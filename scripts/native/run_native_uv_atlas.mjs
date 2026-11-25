import { spawn, spawnSync } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildNativePixal3DLaunch } from '../../server/pixal3d-native.mjs';
import { pixal3dConfig, pixal3dEnv } from '../../server/pixal3d.mjs';
import { getPixal3DBackend } from '../../server/pixal3d-backends.mjs';
import { normalizeGlb } from '../../server/glb-normalize.mjs';
import { validateGeneratedMesh } from '../../server/mesh-validation.mjs';
import { buildUvAtlasWorkflow, parseAtlasOptions } from './uv_atlas_workflow.mjs';

const moduleFile = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(moduleFile), '..', '..');
const url = 'http://127.0.0.1:8194';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function json(route, init) {
  const response = await fetch(url + route, init);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || body.error || `${route}: ${response.status}`);
  return body;
}

export function buildUvAtlasReport({ textureSize, started, now = Date.now(), glbPath, normalized, validation, vram }) {
  return {
    backend: 'windows-rocm',
    denoiserResolution: 1024,
    textureSize,
    seconds: Math.round((now - started) / 100) / 10,
    glbBytes: normalized.buffer.length,
    glbPath,
    validation,
    vram,
    fallbackEvents: [],
  };
}

export function buildUvAtlasEnvironment(config, env = process.env) {
  return pixal3dEnv(config, env);
}

async function main() {
  const options = parseAtlasOptions();
  if (!existsSync(options.inputImage)) throw new Error(`Input PNG not found: ${options.inputImage}`);
  if (!existsSync(options.inputGlb)) throw new Error(`Input GLB not found: ${options.inputGlb}`);
  if (await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false)) {
    throw new Error('Port 8194 is already answering; refusing to reuse or stop another process.');
  }

  const env = { ...process.env, ASTRA_PIXAL3D_BACKEND: 'windows-rocm', ASTRA_PIXAL3D_URL: url, ASTRA_AMD_NAF_TILE: '2' };
  const config = pixal3dConfig({ env, appRoot: root });
  const descriptor = getPixal3DBackend('windows-rocm', env, root);
  const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
  mkdirSync(diagnostics, { recursive: true });
  const launch = buildNativePixal3DLaunch({
    config,
    descriptor,
    tempDir: path.join(diagnostics, `uv-${options.textureSize}-temp`),
    dbFile: path.join(diagnostics, `uv-${options.textureSize}.db`),
    env: buildUvAtlasEnvironment(config, env),
  });
  const logFile = path.join(diagnostics, `baton-uv${options.textureSize}-server.log`);
  const fd = openSync(logFile, 'a');
  const child = spawn(launch.command, launch.args, {
    cwd: launch.cwd, env: launch.env, windowsHide: true, stdio: ['ignore', fd, fd],
  });
  closeSync(fd);
  let vramStarted = false;
  try {
    let ready = false;
    for (let i = 0; i < 180 && !ready; i += 1) {
      if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during startup (${child.exitCode}). See ${logFile}`);
      ready = await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false);
      if (!ready) await sleep(1000);
    }
    if (!ready) throw new Error('Native AMD ComfyUI did not start on port 8194.');

    const form = new FormData();
    form.append('image', new Blob([readFileSync(options.inputImage)], { type: 'image/png' }), `baton-uv-${options.textureSize}.png`);
    form.append('overwrite', 'true');
    const uploaded = await json('/upload/image', { method: 'POST', body: form });
    const workflow = buildUvAtlasWorkflow({
      imageName: uploaded.name,
      glbPath: path.resolve(options.inputGlb),
      textureSize: options.textureSize,
    });
    vramStarted = await fetch(url + '/af/vram/start', { method: 'POST', signal: AbortSignal.timeout(3000) }).then((r) => r.ok).catch(() => false);
    const started = Date.now();
    const queued = await json('/prompt', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: workflow }),
    });
    let item = null;
    for (let attempt = 0; attempt < 2400 && !item; attempt += 1) {
      if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during generation (${child.exitCode}). See ${logFile}`);
      const history = await json('/history/' + queued.prompt_id);
      const record = history[queued.prompt_id];
      const error = record?.status?.messages?.find((message) => message[0] === 'execution_error');
      if (error) throw new Error(error[1]?.exception_message || 'Native UV generation failed.');
      if (record?.status?.completed) item = Object.values(record.outputs || {}).flatMap((output) => output['3d'] || [])[0];
      if (!item) await sleep(1500);
    }
    if (!item) throw new Error('Native UV generation timed out after 60 minutes.');
    const params = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || '', type: item.type || 'output' });
    const response = await fetch(url + '/view?' + params);
    if (!response.ok) throw new Error(`GLB download failed: ${response.status}`);
    const raw = Buffer.from(await response.arrayBuffer());
    const normalized = normalizeGlb(raw);
    const validation = validateGeneratedMesh(normalized.buffer, {
      requireUvTexture: true,
      expectedUvTextureSize: options.textureSize,
    });
    const outDir = path.resolve(options.outputDir || path.join(root, 'data', 'runtime', 'baton'));
    mkdirSync(outDir, { recursive: true });
    const glbPath = path.join(outDir, `amd-pixal3d-native-single-uv${options.textureSize}.glb`);
    writeFileSync(glbPath, normalized.buffer);
    const vram = vramStarted ? await json('/af/vram/stop', { method: 'POST' }).catch(() => null) : null;
    vramStarted = false;
    const report = buildUvAtlasReport({
      textureSize: options.textureSize, started, glbPath, normalized, validation, vram,
    });
    const jsonFile = path.resolve(options.jsonFile || path.join(diagnostics, `baton-uv${options.textureSize}.json`));
    writeFileSync(jsonFile, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  } finally {
    if (vramStarted) await fetch(url + '/af/vram/stop', { method: 'POST' }).catch(() => {});
    await fetch(url + '/free', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unload_models: true, free_memory: true }),
    }).catch(() => {});
    if (child.exitCode === null && process.platform === 'win32') {
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === moduleFile) await main();
