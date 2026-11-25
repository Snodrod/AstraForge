import { spawn, spawnSync } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import { freemem } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildNativePixal3DLaunch } from '../../server/pixal3d-native.mjs';
import { pixal3dConfig, pixal3dEnv } from '../../server/pixal3d.mjs';
import { getPixal3DBackend } from '../../server/pixal3d-backends.mjs';
import { views_generate } from '../views/views_generate.mjs';
import { createNativeMonitor } from './native_monitor.mjs';
import { assertApprovedAttempt, createAttempt, readManifest, sha256File, writeManifest } from './attempt_store.mjs';

const moduleFile = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(moduleFile), '..', '..');
const url = 'http://127.0.0.1:8195';
const slots = ['front', 'left', 'back', 'right'];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const nativePython = path.join(root, 'runtime', 'ComfyUI-Pixal3D-AMD-Native', 'python_embeded', 'python.exe');
const repairScript = path.join(root, 'scripts', 'native', 'belt_repair.py');

function booleanOption(value, name) {
  if (!['true', 'false'].includes(value)) throw new Error(`${name} must be true or false.`);
  return value === 'true';
}

export function parseBeltRepairOptions(argv) {
  const options = {
    input: null,
    outputDir: null,
    jsonFile: null,
    configFile: path.join(root, 'scripts', 'native', 'identity', 'belt-repair-orc.json'),
    prepareOnly: false,
    debug: false,
    seed: 42,
  };
  const names = {
    '--input': 'input', '--output': 'outputDir', '--json': 'jsonFile', '--config': 'configFile',
    '--prepare-only': 'prepareOnly', '--debug': 'debug', '--seed': 'seed',
  };
  for (let index = 0; index < argv.length; index += 2) {
    const key = names[argv[index]];
    if (!key || argv[index + 1] === undefined) throw new Error(`Unknown option: ${argv[index]}`);
    options[key] = argv[index + 1];
  }
  if (!options.input) throw new Error('--input is required.');
  options.prepareOnly = typeof options.prepareOnly === 'boolean' ? options.prepareOnly : booleanOption(options.prepareOnly, '--prepare-only');
  options.debug = typeof options.debug === 'boolean' ? options.debug : booleanOption(options.debug, '--debug');
  options.seed = Number(options.seed);
  if (!Number.isSafeInteger(options.seed) || options.seed < 0) throw new Error('--seed must be a non-negative safe integer.');
  return options;
}

export function readRepairSource(input) {
  const attemptDir = path.resolve(input);
  const manifest = readManifest(attemptDir);
  if (manifest.stage !== 'views') throw new Error('Repair source stage must be views.');
  if (manifest.status !== 'success') throw new Error('Repair source did not complete successfully.');
  for (const [relative, expected] of Object.entries(manifest.artifacts || {})) {
    const file = path.resolve(attemptDir, relative);
    if (path.relative(attemptDir, file).startsWith('..') || !existsSync(file) || sha256File(file) !== expected) {
      throw new Error(`Repair source artifact SHA-256 mismatch: ${relative}`);
    }
  }
  const review = JSON.parse(readFileSync(path.join(attemptDir, 'review.json'), 'utf8'));
  return { attemptDir, viewsDir: path.join(attemptDir, 'views'), manifest, review };
}

export function assertRepairMemoryBudget(freeBytes = freemem(), minimumBytes = 12 * 1024 ** 3) {
  if (freeBytes < minimumBytes) {
    const required = Math.round(minimumBytes / 1024 ** 3);
    const available = (freeBytes / 1024 ** 3).toFixed(1);
    throw new Error(`At least ${required} GiB free system RAM is required before MV-Adapter; only ${available} GiB is available. Close other large applications first.`);
  }
}

export function assertDistinctDonorViews(donorViews) {
  const seen = new Map();
  for (const slot of slots) {
    const hash = sha256File(path.join(donorViews, 'raw', `${slot}.png`));
    if (seen.has(hash)) throw new Error(`Duplicate donor views: ${slot}=${seen.get(hash)}`);
    seen.set(hash, slot);
  }
}

function runRepairPython(command, sourceViews, attemptDir, configFile, donorViews = null) {
  const args = ['-I', repairScript, command, '--source', sourceViews, '--output', attemptDir, '--config', configFile];
  if (donorViews) args.push('--donor', donorViews);
  const result = spawnSync(nativePython, args, { cwd: root, stdio: 'inherit', windowsHide: true });
  if (result.error || result.status !== 0) throw new Error(`Belt repair ${command} failed (${result.error || result.status}).`);
}

async function main() {
  const options = parseBeltRepairOptions(process.argv.slice(2));
  const source = readRepairSource(options.input);
  const configFile = path.resolve(options.configFile);
  if (!options.prepareOnly) assertRepairMemoryBudget();
  const attempt = createAttempt(options.outputDir || path.join(root, 'data', 'runtime', 'orc-views-repair'), 'views');
  const started = Date.now();
  runRepairPython('prepare', source.viewsDir, attempt.dir, configFile);
  if (options.prepareOnly) {
    const report = {
      version: 1,
      id: attempt.id,
      stage: 'views',
      operation: 'belt-repair-prepare',
      status: 'prepared',
      sourceAttempt: source.attemptDir,
      sourceManifestSha256: sha256File(path.join(source.attemptDir, 'manifest.json')),
      maskPreview: path.join(attempt.dir, 'mask-preview.jpg'),
      error: null,
    };
    writeManifest(attempt.dir, report);
    console.log(JSON.stringify({ ...report, attemptDir: attempt.dir, review: 'pending' }, null, 2));
    return;
  }

  const repairConfig = JSON.parse(readFileSync(configFile, 'utf8'));
  if (!repairConfig.prompt?.trim() || typeof repairConfig.negative !== 'string') throw new Error('Repair config requires prompt and negative.');
  const aposeAttemptDir = source.manifest.sourceApose?.attemptDir;
  if (!aposeAttemptDir) throw new Error('Repair source does not record its A-pose attempt.');
  assertApprovedAttempt(aposeAttemptDir, 'apose');
  const aposeImage = path.join(aposeAttemptDir, 'apose-front.png');
  if (await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((response) => response.ok).catch(() => false)) {
    throw new Error('Port 8195 is already answering; refusing to reuse or stop another process.');
  }

  const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
  mkdirSync(diagnostics, { recursive: true });
  const runName = `orc-belt-repair-${attempt.id}`;
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
  try {
    let ready = false;
    for (let poll = 0; poll < 180 && !ready; poll += 1) {
      if (child.exitCode !== null) throw new Error(`Native AMD ComfyUI exited during startup (${child.exitCode}). See ${logFile}`);
      ready = await fetch(url + '/system_stats', { signal: AbortSignal.timeout(1500) }).then((response) => response.ok).catch(() => false);
      if (!ready) await sleep(1000);
    }
    if (!ready) throw new Error('Native AMD ComfyUI did not start on port 8195.');

    const donorViews = path.join(attempt.dir, 'donor-views');
    const donor = await views_generate({
      input: aposeImage, outDir: donorViews, provider: 'mvadapter', comfyUrl: url,
      size: 512, steps: 30, seed: options.seed,
      prompt: repairConfig.prompt, negative: repairConfig.negative,
      useGeneratedFront: true,
      onProgress: (message) => console.error('· donor:', message),
    });
    assertDistinctDonorViews(donorViews);
    runRepairPython('composite', source.viewsDir, attempt.dir, configFile, donorViews);
    const composite = path.join(attempt.dir, 'composite');
    const viewsDir = path.join(attempt.dir, 'views');
    const views = await views_generate({
      input: path.join(composite, 'front.png'), outDir: viewsDir, provider: 'manual',
      manual: Object.fromEntries(slots.slice(1).map((slot) => [slot, path.join(composite, `${slot}.png`)])),
      onProgress: (message) => console.error('· repaired:', message),
    });
    const sheet = path.join(attempt.dir, 'views-sheet.jpg');
    const sheetResult = spawnSync(nativePython, [
      '-I', path.join(root, 'scripts', 'views', 'contact_sheet.py'), sheet, `Repaired=${viewsDir}`,
    ], { cwd: root, stdio: 'inherit', windowsHide: true });
    if (sheetResult.error || sheetResult.status !== 0) throw new Error(`Repair contact sheet failed (${sheetResult.error || sheetResult.status}).`);

    const artifacts = {
      'mask-preview.jpg': sha256File(path.join(attempt.dir, 'mask-preview.jpg')),
      'views-sheet.jpg': sha256File(sheet),
      'composition.json': sha256File(path.join(attempt.dir, 'composition.json')),
    };
    for (const slot of slots) {
      const relative = `views/norm/cut_${slot}.png`;
      artifacts[relative] = sha256File(path.join(attempt.dir, relative));
    }
    const report = {
      version: 1,
      id: attempt.id,
      stage: 'views',
      operation: 'belt-repair',
      backend: 'windows-rocm',
      status: 'success',
      seed: options.seed,
      seconds: Math.round((Date.now() - started) / 100) / 10,
      sourceAttempt: source.attemptDir,
      sourceManifestSha256: sha256File(path.join(source.attemptDir, 'manifest.json')),
      sourceApose: source.manifest.sourceApose,
      prompt: repairConfig.prompt,
      negative: repairConfig.negative,
      repairConfig: configFile,
      donor,
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
  } catch (error) {
    writeManifest(attempt.dir, {
      version: 1,
      id: attempt.id,
      stage: 'views',
      operation: 'belt-repair',
      backend: 'windows-rocm',
      status: 'error',
      seed: options.seed,
      seconds: Math.round((Date.now() - started) / 100) / 10,
      sourceAttempt: source.attemptDir,
      sourceManifestSha256: sha256File(path.join(source.attemptDir, 'manifest.json')),
      repairConfig: configFile,
      artifacts: {},
      error: error.message,
    });
    throw error;
  } finally {
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
