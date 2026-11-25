import { spawn, spawnSync } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildNativePixal3DLaunch } from '../../server/pixal3d-native.mjs';
import { pixal3dConfig, pixal3dEnv } from '../../server/pixal3d.mjs';
import { getPixal3DBackend } from '../../server/pixal3d-backends.mjs';
import { normalizeGlb } from '../../server/glb-normalize.mjs';
import { validateGeneratedMesh } from '../../server/mesh-validation.mjs';
import { views_generate } from '../views/views_generate.mjs';
import { buildOrcShapeWorkflow, buildOrcTextureWorkflow, parseOrcOptions } from './multiview_orc_workflow.mjs';
import { resolveIdentityConfig } from './identity_config.mjs';
import { createNativeMonitor } from './native_monitor.mjs';
import { assertApprovedAttempt, createAttempt, sha256File, writeManifest } from './attempt_store.mjs';

const moduleFile = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(moduleFile), '..', '..');
const url = 'http://127.0.0.1:8195';
const slots = ['front', 'left', 'back', 'right'];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function nativeOrcViewPath(viewsDir, slot) {
  return path.join(viewsDir, 'norm', `cut_${slot}.png`);
}

export function resolveApprovedAposeInput(input) {
  const attemptDir = path.resolve(input);
  const manifest = assertApprovedAttempt(attemptDir, 'apose');
  return { image: path.join(attemptDir, 'apose-front.png'), attemptDir, manifest };
}

export function resolveApprovedViewsDir(input) {
  const attemptDir = path.resolve(input);
  const manifest = assertApprovedAttempt(attemptDir, 'views');
  return { viewsDir: path.join(attemptDir, 'views'), attemptDir, manifest };
}

export function buildOrcReport({ stage, started, now = Date.now(), glbPath, normalized, validation, vram, profile = 'quality', textureSize = 2048 }) {
  return {
    backend: 'windows-rocm',
    stage,
    profile,
    geometryResolution: 512,
    denoiserResolution: 1024,
    textureSize: stage === 'texture' ? textureSize : null,
    seconds: Math.round((now - started) / 100) / 10,
    glbBytes: normalized.buffer.length,
    glbPath,
    validation,
    vram,
    fallbackEvents: [],
  };
}

export function resolveOrcTextureProfile(profile) {
  if (profile === 'light') return { textureSize: 1024, textureSteps: 4, lighten: true };
  if (profile === 'balanced') return { textureSize: 1024, textureSteps: 4, lighten: false };
  return { textureSize: 2048, textureSteps: 12, lighten: false };
}

async function json(route, init) {
  const response = await fetch(url + route, init);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || body.error || `${route}: ${response.status}`);
  return body;
}

async function uploadImage(file, slot) {
  const form = new FormData();
  form.append('image', new Blob([readFileSync(file)], { type: 'image/png' }), `orc-${slot}.png`);
  form.append('overwrite', 'true');
  const uploaded = await json('/upload/image', { method: 'POST', body: form });
  return uploaded.name;
}

async function runGraph(workflow, child, logFile) {
  const queued = await json('/prompt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: workflow }),
  });
  for (let attempt = 0; attempt < 2400; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during generation (${child.exitCode}). See ${logFile}`);
    const history = await json('/history/' + queued.prompt_id);
    const record = history[queued.prompt_id];
    const error = record?.status?.messages?.find((message) => message[0] === 'execution_error');
    if (error) throw new Error(error[1]?.exception_message || 'Native multi-view generation failed.');
    if (record?.status?.completed) return Object.values(record.outputs || {}).flatMap((output) => output['3d'] || [])[0];
    await sleep(1500);
  }
  throw new Error('Native multi-view generation timed out after 60 minutes.');
}

async function main() {
  const options = parseOrcOptions(process.argv.slice(2));
  let approvedApose = null;
  let approvedViews = null;
  let identity = null;
  let attempt = null;
  let viewsDir;
  if (options.stage === 'views') {
    approvedApose = resolveApprovedAposeInput(options.input);
    identity = resolveIdentityConfig({
      identityRoot: path.join(root, 'scripts', 'native', 'identity'),
      presetFile: options.presetFile,
      negativeProfile: options.negativeProfile,
      negativeFile: options.negativeFile,
      purpose: 'views',
    });
    if (identity.preset.kind !== 'character') throw new Error('The native multi-view runner requires a character preset.');
    attempt = createAttempt(options.outputDir || path.join(root, 'data', 'runtime', 'identity-views'), 'views');
    viewsDir = path.join(attempt.dir, 'views');
  } else {
    approvedViews = resolveApprovedViewsDir(options.viewsDir);
    viewsDir = approvedViews.viewsDir;
    for (const slot of slots) {
      if (!existsSync(nativeOrcViewPath(viewsDir, slot))) throw new Error(`Prepared view not found: ${nativeOrcViewPath(viewsDir, slot)}`);
    }
  }
  if (options.stage === 'texture' && !existsSync(options.inputGlb)) throw new Error(`Input GLB not found: ${options.inputGlb}`);
  if (await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false)) {
    throw new Error('Port 8195 is already answering; refusing to reuse or stop another process.');
  }

  const outputDir = path.resolve(options.outputDir || path.join(root, 'data', 'runtime', 'identity-multiview-output'));
  if (options.stage !== 'views') mkdirSync(outputDir, { recursive: true });
  const { textureSize, textureSteps, lighten } = resolveOrcTextureProfile(options.profile);
  let textureInputGlb = options.inputGlb ? path.resolve(options.inputGlb) : null;
  if (options.stage === 'texture' && lighten) {
    const lightGlb = path.join(outputDir, 'amd-pixal3d-native-multiview-light-shape.glb');
    console.error('· Lightening Draft 512 to at most 120000 triangles');
    const result = spawnSync(
      path.join(root, 'runtime', 'ComfyUI-Pixal3D-AMD-Native', 'python_embeded', 'python.exe'),
      ['-I', path.join(root, 'scripts', 'native', 'lighten_glb.py'), textureInputGlb, lightGlb, '--target-faces', '120000'],
      { cwd: root, stdio: 'inherit' },
    );
    if (result.error || result.status !== 0) throw new Error(`GLB lightening failed (${result.error || result.status}).`);
    textureInputGlb = lightGlb;
  }
  const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
  mkdirSync(diagnostics, { recursive: true });
  const runId = attempt?.id || approvedViews?.manifest.id || 'native';
  const runName = `orc-${options.stage}-${runId}${options.profile === 'quality' ? '' : `-${options.profile}`}`;
  const env = {
    ...process.env,
    ASTRA_PIXAL3D_BACKEND: 'windows-rocm',
    ASTRA_PIXAL3D_URL: url,
    ASTRA_AMD_NAF_TILE: '2',
    ASTRA_SDXL_CONFIG: path.join(root, 'data', 'models', 'mvadapter', 'sdxl-config'),
  };
  const config = pixal3dConfig({ env, appRoot: root });
  const descriptor = getPixal3DBackend('windows-rocm', env, root);
  const launch = buildNativePixal3DLaunch({
    config,
    descriptor,
    tempDir: path.join(diagnostics, `${runName}-temp`),
    dbFile: path.join(diagnostics, `${runName}.db`),
    env: pixal3dEnv(config, env),
  });
  const logFile = path.join(diagnostics, `${runName}-server.log`);
  const monitor = options.debug ? createNativeMonitor({
    engineUrl: url,
    jsonlFile: path.join(diagnostics, `${runName}-debug.jsonl`),
    summaryFile: path.join(diagnostics, `${runName}-debug.json`),
  }) : null;
  if (monitor) await monitor.start();
  const fd = openSync(logFile, 'a');
  const child = spawn(launch.command, launch.args, {
    cwd: launch.cwd, env: launch.env, windowsHide: true, stdio: ['ignore', fd, fd],
  });
  closeSync(fd);
  const started = Date.now();
  let vramStarted = false;
  try {
    let ready = false;
    for (let poll = 0; poll < 180 && !ready; poll += 1) {
      if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during startup (${child.exitCode}). See ${logFile}`);
      ready = await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false);
      if (!ready) await sleep(1000);
    }
    if (!ready) throw new Error('Native AMD ComfyUI did not start on port 8195.');

    if (options.stage === 'views') {
      const views = await views_generate({
        input: approvedApose.image, outDir: viewsDir, provider: 'mvadapter', comfyUrl: url,
        size: options.viewSize, steps: 30, seed: options.viewSeed,
        prompt: identity.prompt, negative: identity.negative,
        useGeneratedFront: options.coherentFront,
        onProgress: (message) => console.error('·', message),
      });
      const sheet = path.join(attempt.dir, 'views-sheet.jpg');
      const sheetResult = spawnSync(launch.command, [
        '-I', path.join(root, 'scripts', 'views', 'contact_sheet.py'),
        sheet, `A-pose=${viewsDir}`,
      ], { cwd: root, stdio: 'inherit', windowsHide: true });
      if (sheetResult.error || sheetResult.status !== 0) throw new Error(`Views contact sheet failed (${sheetResult.error || sheetResult.status}).`);
      const artifacts = { 'views-sheet.jpg': sha256File(sheet) };
      for (const slot of slots) {
        const relative = `views/norm/cut_${slot}.png`;
        artifacts[relative] = sha256File(path.join(attempt.dir, relative));
      }
      const report = {
        version: 1,
        id: attempt.id,
        stage: 'views',
        backend: 'windows-rocm',
        status: 'success',
        seed: options.viewSeed,
        seconds: Math.round((Date.now() - started) / 100) / 10,
        sourceApose: {
          attemptDir: approvedApose.attemptDir,
          manifestSha256: sha256File(path.join(approvedApose.attemptDir, 'manifest.json')),
        },
        identity,
        viewsDir,
        views,
        artifacts,
        error: null,
      };
      writeManifest(attempt.dir, report);
      if (options.jsonFile) {
        const jsonFile = path.resolve(options.jsonFile);
        mkdirSync(path.dirname(jsonFile), { recursive: true });
        writeFileSync(jsonFile, JSON.stringify(report, null, 2) + '\n');
      }
      console.log(JSON.stringify({ ...report, attemptDir: attempt.dir, review: 'pending' }, null, 2));
      return;
    }

    const images = {};
    for (const slot of slots) images[slot] = await uploadImage(nativeOrcViewPath(viewsDir, slot), slot);
    const workflow = options.stage === 'shape'
      ? buildOrcShapeWorkflow(images)
      : buildOrcTextureWorkflow({ images, glbPath: textureInputGlb, textureSize, textureSteps });
    vramStarted = await fetch(url + '/af/vram/start', { method: 'POST', signal: AbortSignal.timeout(3000) }).then((r) => r.ok).catch(() => false);
    const item = await runGraph(workflow, child, logFile);
    if (!item) throw new Error('Native multi-view generation returned no GLB.');
    const params = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || '', type: item.type || 'output' });
    const response = await fetch(url + '/view?' + params);
    if (!response.ok) throw new Error(`GLB download failed: ${response.status}`);
    const normalized = normalizeGlb(Buffer.from(await response.arrayBuffer()));
    const validation = validateGeneratedMesh(normalized.buffer, {
      requireUvTexture: options.stage === 'texture',
      expectedUvTextureSize: options.stage === 'texture' ? textureSize : undefined,
    });
    const name = options.stage === 'shape'
      ? 'amd-pixal3d-native-multiview-draft512.glb'
      : `amd-pixal3d-native-multiview-${options.profile}-uv${textureSize}.glb`;
    const glbPath = path.join(outputDir, name);
    writeFileSync(glbPath, normalized.buffer);
    const vram = vramStarted ? await json('/af/vram/stop', { method: 'POST' }).catch(() => null) : null;
    vramStarted = false;
    const report = buildOrcReport({ stage: options.stage, started, glbPath, normalized, validation, vram, profile: options.profile, textureSize });
    const jsonFile = path.resolve(options.jsonFile || path.join(diagnostics, `${runName}.json`));
    writeFileSync(jsonFile, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    if (attempt) {
      writeManifest(attempt.dir, {
        version: 1,
        id: attempt.id,
        stage: 'views',
        backend: 'windows-rocm',
        status: 'error',
        seed: options.viewSeed,
        seconds: Math.round((Date.now() - started) / 100) / 10,
        sourceApose: {
          attemptDir: approvedApose.attemptDir,
          manifestSha256: sha256File(path.join(approvedApose.attemptDir, 'manifest.json')),
        },
        identity,
        viewsDir,
        artifacts: {},
        error: error.message,
      });
    }
    throw error;
  } finally {
    if (vramStarted) await fetch(url + '/af/vram/stop', { method: 'POST' }).catch(() => {});
    await fetch(url + '/free', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unload_models: true, free_memory: true }),
    }).catch(() => {});
    if (monitor) {
      const summary = await monitor.stop().catch((error) => {
        console.error('· Native debug monitor failed:', error.message);
        return null;
      });
      if (summary) console.error('· Native debug:', JSON.stringify(summary));
    }
    if (child.exitCode === null && process.platform === 'win32') {
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === moduleFile) await main();
