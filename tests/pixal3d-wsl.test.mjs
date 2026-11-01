import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { toWslPath, pixal3dWslArgs } from '../server/pixal3d-wsl.mjs';
import {
  pixal3dConfig, pixal3dInstallState, startPixal3dRuntime, stopOwnedPixal3d,
  PIXAL3D_REQUIREMENTS, _resetPixal3dRuntime,
} from '../server/pixal3d.mjs';
import { pixal3dWorkflow } from '../server/schema.mjs';

const tmp = mkdtempSync(path.join(tmpdir(), 'af-pixal3d-wsl-'));
test.after(() => rmSync(tmp, { recursive: true, force: true }));

test('WSL paths are absolute, deterministic and never shell-interpolated', () => {
  assert.equal(toWslPath('C:\\work\\Astra Forge'), '/mnt/c/work/Astra Forge');
  assert.equal(toWslPath('D:/models/a.glb'), '/mnt/d/models/a.glb');
  assert.throws(() => toWslPath('relative/path'), /absolute Windows path/);
  const args = pixal3dWslArgs(
    { dir: 'C:\\work\\engine', port: 8191, wslDistro: 'AstraForge-AMD' },
    'C:\\work\\data\\runtime',
  );
  assert.deepEqual(args.slice(0, 3), ['-d', 'AstraForge-AMD', '--']);
  assert.ok(args.includes('--port') && args.includes('8191'));
  assert.ok(args.includes('--temp-directory') && args.includes('--database-url'));
  assert.ok(args.every((arg) => !arg.includes('C:\\')));
});

test('backend config keeps NVIDIA Windows defaults and accepts explicit WSL overrides', () => {
  const appRoot = path.join(tmp, 'app');
  mkdirSync(path.join(appRoot, 'config'), { recursive: true });
  assert.equal(pixal3dConfig({ env: {}, appRoot }).backend, 'windows');
  assert.equal(pixal3dConfig({ env: {}, appRoot }).wslDistro, 'AstraForge-AMD');
  writeFileSync(path.join(appRoot, 'config', 'paths.json'), JSON.stringify({
    pixal3dBackend: 'wsl', pixal3dWslDistro: 'Project-Distro',
  }));
  assert.equal(pixal3dConfig({ env: {}, appRoot }).backend, 'wsl');
  assert.equal(pixal3dConfig({ env: {}, appRoot }).wslDistro, 'Project-Distro');
  assert.equal(pixal3dConfig({ env: { ASTRA_PIXAL3D_BACKEND: 'windows' }, appRoot }).backend, 'windows');
  assert.throws(() => pixal3dConfig({ env: { ASTRA_PIXAL3D_BACKEND: 'cuda' }, appRoot }), /backend/i);
});

function fakeWslInstall(name) {
  const dir = path.join(tmp, name);
  for (const [rel] of PIXAL3D_REQUIREMENTS) {
    if (rel === 'python_embeded/python.exe') continue;
    mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    writeFileSync(path.join(dir, rel), 'x');
  }
  writeFileSync(path.join(dir, 'wsl-ready.json'), '{}\n');
  return { dir, url: 'http://127.0.0.1:8191', port: 8191, backend: 'wsl', wslDistro: 'AstraForge-AMD' };
}

test('WSL install state requires the ROCm probe, not Windows Python', () => {
  const cfg = fakeWslInstall('installed');
  assert.equal(pixal3dInstallState(cfg).installed, true);
  const missing = fakeWslInstall('not-ready');
  rmSync(path.join(missing.dir, 'wsl-ready.json'));
  const state = pixal3dInstallState(missing);
  assert.equal(state.installed, false);
  assert.ok(state.missing.some((item) => item.path === 'wsl-ready.json'));
});

function fakeChild() {
  const child = new EventEmitter();
  Object.assign(child, { pid: 4242, killed: false, exitCode: null, unref() {}, kill() { this.killed = true; } });
  return child;
}

test('WSL runtime starts through wsl.exe and terminates only its own distro', async () => {
  _resetPixal3dRuntime();
  const cfg = fakeWslInstall('runtime');
  const runtimeDir = path.join(tmp, 'runtime-data');
  const launches = [];
  let probes = 0;
  try {
    const result = await startPixal3dRuntime({
      cfg, runtimeDir, sleep: async () => {},
      fetchFn: async () => {
        probes += 1;
        if (probes === 1) throw new Error('ECONNREFUSED');
        return { ok: true };
      },
      spawnFn: (exe, args, options) => {
        launches.push({ exe, args, options });
        return fakeChild();
      },
    });
    assert.equal(result.running, true);
    assert.equal(launches[0].exe, 'wsl.exe');
    assert.deepEqual(launches[0].args.slice(0, 3), ['-d', 'AstraForge-AMD', '--']);
    stopOwnedPixal3d({ spawnFn: (exe, args) => {
      launches.push({ exe, args });
      return { on() {} };
    } });
    assert.equal(launches[1].exe, 'wsl.exe');
    assert.deepEqual(launches[1].args, ['-d', 'AstraForge-AMD', '--terminate']);
    assert.ok(!launches[1].args.includes('--shutdown'));
  } finally {
    _resetPixal3dRuntime();
  }
});

test('WSL workflow selects SDPA while the Windows graph retains FlashAttention', () => {
  const windows = pixal3dWorkflow({ front: 'f.png' });
  assert.equal(windows['1'].inputs.backend, 'flash_attn');
  assert.equal(windows['1'].inputs.sparse_backend, 'flash_attn');
  const amd = pixal3dWorkflow({ front: 'f.png' }, { runtimeBackend: 'wsl' });
  assert.equal(amd['1'].inputs.backend, 'sdpa');
  assert.equal(amd['1'].inputs.sparse_backend, 'sdpa');
});
