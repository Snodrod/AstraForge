import { spawn, spawnSync } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildNativePixal3DLaunch } from '../../server/pixal3d-native.mjs';
import { pixal3dConfig, pixal3dEnv } from '../../server/pixal3d.mjs';
import { getPixal3DBackend } from '../../server/pixal3d-backends.mjs';
import { buildQwenAposeWorkflow, parseAposeOptions } from './qwen_apose_workflow.mjs';
import { resolveIdentityConfig } from './identity_config.mjs';
import { createAttempt, sha256File, writeManifest } from './attempt_store.mjs';

const moduleFile = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(moduleFile), '..', '..');
const url = 'http://127.0.0.1:8196';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function buildAposeReport({ id, status, started, now = Date.now(), source, output, seed, steps, identity, workflow, assets, loaderCommit, artifacts = {}, error = null }) {
  return {
    version: 1,
    id,
    stage: 'apose',
    backend: 'windows-rocm',
    status,
    seed,
    steps,
    seconds: Math.round((now - started) / 100) / 10,
    source: { path: source, sha256: sha256File(source) },
    output,
    identity,
    models: assets.map(({ path: assetPath, bytes, sha256 }) => ({ path: assetPath, bytes, sha256 })),
    ggufLoaderCommit: loaderCommit,
    workflow,
    artifacts,
    error,
  };
}

async function json(route, init) {
  const response = await fetch(url + route, init);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || body.error || `${route}: ${response.status}`);
  return body;
}

async function uploadImage(file) {
  const form = new FormData();
  form.append('image', new Blob([readFileSync(file)], { type: 'image/png' }), 'orc-apose-source.png');
  form.append('overwrite', 'true');
  const uploaded = await json('/upload/image', { method: 'POST', body: form });
  return uploaded.name;
}

async function runGraph(workflow, child, logFile) {
  const queued = await json('/prompt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt: workflow }),
  });
  for (let attempt = 0; attempt < 2400; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during A-pose generation (${child.exitCode}). See ${logFile}`);
    const history = await json('/history/' + queued.prompt_id);
    const record = history[queued.prompt_id];
    const error = record?.status?.messages?.find((message) => message[0] === 'execution_error');
    if (error) throw new Error(error[1]?.exception_message || 'Native A-pose generation failed.');
    if (record?.status?.completed) return Object.values(record.outputs || {}).flatMap((output) => output.images || [])[0];
    await sleep(1500);
  }
  throw new Error('Native A-pose generation timed out after 60 minutes.');
}

async function main() {
  const options = parseAposeOptions(process.argv.slice(2));
  const source = path.resolve(options.input);
  if (!existsSync(source)) throw new Error(`Input image not found: ${source}`);
  const identity = resolveIdentityConfig({
    identityRoot: path.join(root, 'scripts', 'native', 'identity'),
    presetFile: options.presetFile,
    negativeProfile: options.negativeProfile,
    negativeFile: options.negativeFile,
    purpose: 'apose',
  });
  if (identity.preset.kind !== 'character') throw new Error('A-pose requires a character preset.');
  const attempt = createAttempt(options.outputDir || path.join(root, 'data', 'runtime', 'identity-apose'), 'apose');
  const output = path.join(attempt.dir, 'apose-front.png');
  const comparison = path.join(attempt.dir, 'comparison.jpg');
  const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
  mkdirSync(diagnostics, { recursive: true });
  if (await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false)) {
    throw new Error('Port 8196 is already answering; refusing to reuse or stop another process.');
  }

  const assetManifest = JSON.parse(readFileSync(path.join(root, 'scripts', 'native', 'qwen-apose-assets.json'), 'utf8'));
  const loaderRoot = path.join(root, 'runtime', 'ComfyUI-Pixal3D-AMD-Native', 'ComfyUI', 'custom_nodes', 'ComfyUI-GGUF');
  const revision = spawnSync('git', ['-C', loaderRoot, 'rev-parse', 'HEAD'], { encoding: 'utf8', windowsHide: true });
  if (revision.error || revision.status !== 0) throw new Error('Unable to determine the ComfyUI-GGUF revision.');
  const loaderCommit = revision.stdout.trim();
  const env = {
    ...process.env,
    ASTRA_PIXAL3D_BACKEND: 'windows-rocm',
    ASTRA_PIXAL3D_URL: url,
    ASTRA_AMD_NAF_TILE: '2',
  };
  const config = pixal3dConfig({ env, appRoot: root });
  const descriptor = getPixal3DBackend('windows-rocm', env, root);
  const launch = buildNativePixal3DLaunch({
    config,
    descriptor,
    tempDir: path.join(diagnostics, `qwen-apose-${attempt.id}-temp`),
    dbFile: path.join(diagnostics, `qwen-apose-${attempt.id}.db`),
    env: pixal3dEnv(config, env),
  });
  const logFile = path.join(diagnostics, `qwen-apose-${attempt.id}-server.log`);
  const fd = openSync(logFile, 'a');
  const child = spawn(launch.command, launch.args, {
    cwd: launch.cwd, env: launch.env, windowsHide: true, stdio: ['ignore', fd, fd],
  });
  closeSync(fd);
  const started = Date.now();
  let workflow = null;
  try {
    let ready = false;
    for (let poll = 0; poll < 180 && !ready; poll += 1) {
      if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during startup (${child.exitCode}). See ${logFile}`);
      ready = await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((r) => r.ok).catch(() => false);
      if (!ready) await sleep(1000);
    }
    if (!ready) throw new Error('Native AMD ComfyUI did not start on port 8196.');
    const image = await uploadImage(source);
    workflow = buildQwenAposeWorkflow({
      image, seed: options.seed, steps: options.steps,
      prompt: identity.prompt, negative: identity.negative,
    });
    const item = await runGraph(workflow, child, logFile);
    if (!item) throw new Error('Native A-pose generation returned no PNG.');
    const params = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || '', type: item.type || 'output' });
    const response = await fetch(url + '/view?' + params);
    if (!response.ok) throw new Error(`PNG download failed: ${response.status}`);
    writeFileSync(output, Buffer.from(await response.arrayBuffer()));
    const comparisonResult = spawnSync(launch.command, [
      '-I', path.join(root, 'scripts', 'native', 'apose_comparison.py'),
      '--source', source, '--generated', output, '--output', comparison,
    ], { cwd: root, stdio: 'inherit', windowsHide: true });
    if (comparisonResult.error || comparisonResult.status !== 0) throw new Error(`A-pose comparison failed (${comparisonResult.error || comparisonResult.status}).`);
    const report = buildAposeReport({
      id: attempt.id, status: 'success', started, source, output,
      seed: options.seed, steps: options.steps, identity, workflow,
      assets: assetManifest.assets, loaderCommit,
      artifacts: {
        'apose-front.png': sha256File(output),
        'comparison.jpg': sha256File(comparison),
      },
    });
    writeManifest(attempt.dir, report);
    if (options.jsonFile) {
      const jsonFile = path.resolve(options.jsonFile);
      mkdirSync(path.dirname(jsonFile), { recursive: true });
      writeFileSync(jsonFile, JSON.stringify(report, null, 2) + '\n');
    }
    console.log(JSON.stringify({ ...report, attemptDir: attempt.dir, review: 'pending' }, null, 2));
  } catch (error) {
    const report = buildAposeReport({
      id: attempt.id, status: 'error', started, source, output,
      seed: options.seed, steps: options.steps, identity, workflow,
      assets: assetManifest.assets, loaderCommit, error: error.message,
    });
    writeManifest(attempt.dir, report);
    throw error;
  } finally {
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
