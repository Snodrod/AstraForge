import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { getPixal3DBackend } from '../server/pixal3d-backends.mjs';
import { buildNativePixal3DLaunch } from '../server/pixal3d-native.mjs';
import { pixal3dWorkflow } from '../server/schema.mjs';
import { validatePixal3dRequest } from '../server/pixal3d.mjs';

const root = path.resolve('C:/AstraForge-native-test');
const descriptor = getPixal3DBackend('windows-rocm', {}, root);
const config = { backend: 'windows-rocm', dir: descriptor.runtimeRoot, nativeDir: descriptor.runtimeRoot, port: 8191 };

test('native AMD launch uses only native Windows Python and paths', () => {
  const launch = buildNativePixal3DLaunch({ config, descriptor, tempDir: path.join(root, 'temp'), dbFile: path.join(root, 'pixal3d.db') });
  assert.equal(launch.command, descriptor.pythonPath);
  assert.equal(launch.cwd, descriptor.runtimeRoot);
  assert.ok(path.isAbsolute(launch.command));
  assert.ok(launch.args.includes(path.join('ComfyUI', 'main.py')));
  assert.equal(launch.args[launch.args.indexOf('--port') + 1], '8191');
  assert.ok(launch.args.includes('--disable-async-offload'));
  assert.ok(!/wsl\.exe|\/mnt\/[a-z]/i.test([launch.command, ...launch.args].join(' ')));
  assert.equal(launch.env.ASTRA_PIXAL3D_NATIVE, '1');
  assert.equal(launch.env.ASTRA_AMD_ROCM, '1');
  assert.equal(launch.env.HF_HUB_OFFLINE, '1');
  assert.equal(launch.env.TRANSFORMERS_OFFLINE, '1');
});

test('native AMD launch rejects mixed backends, relative paths and Linux runtime paths', () => {
  assert.throws(() => buildNativePixal3DLaunch({ config: { ...config, backend: 'wsl' }, descriptor }), /windows-rocm/);
  assert.throws(() => buildNativePixal3DLaunch({ config: { ...config, dir: 'relative' }, descriptor: { ...descriptor, runtimeRoot: 'relative', pythonPath: 'relative/python.exe' } }), /absolute/);
  assert.throws(() => buildNativePixal3DLaunch({ config: { ...config, dir: '/mnt/c/px' }, descriptor: { ...descriptor, runtimeRoot: '/mnt/c/px', pythonPath: '/mnt/c/px/python' } }), /native Windows/);
});

test('native AMD workflow uses SDPA and selects a 1024 UV atlas only for explicit native UV', () => {
  const graph = pixal3dWorkflow({ front: 'f.png' }, { runtimeBackend: 'windows-rocm' });
  assert.equal(graph['1'].inputs.backend, 'sdpa');
  assert.equal(graph['1'].inputs.sparse_backend, 'sdpa');
  assert.equal(graph['40'], undefined);
  assert.equal(pixal3dWorkflow({ front: 'f.png' }, { runtimeBackend: 'windows' })['1'].inputs.backend, 'flash_attn');
  const textured = pixal3dWorkflow({ front: 'f.png' }, { runtimeBackend: 'windows-rocm', quality: 'draft', uvTexture: true });
  assert.equal(textured['40'].inputs.texture_size, 1024);
  assert.equal(textured['40'].inputs.bake_on_vertices, false);
  assert.equal(pixal3dWorkflow({ front: 'f.png' }, { runtimeBackend: 'wsl', quality: 'draft', uvTexture: true })['40'].inputs.texture_size, 512);
});

test('native AMD request validation gates UV 1024 separately and blocks unsupported combinations', () => {
  const shape = { engine: 'pixal3d', quality: 'draft', vertexColors: false, uvTexture: false };
  assert.throws(() => validatePixal3dRequest(shape, { backend: 'windows-rocm' }, { multi: false }), (error) => error.code === 'pixal3d_native_not_ready');
  assert.throws(() => validatePixal3dRequest({ ...shape, quality: 'standard' }, { backend: 'windows-rocm' }, { multi: true }), (error) => error.code === 'pixal3d_amd_multiview_1024_unsupported');
  assert.throws(() => validatePixal3dRequest({ ...shape, vertexColors: true }, { backend: 'windows-rocm', nativeCapabilities: { single512: true } }, { multi: false }), (error) => error.code === 'pixal3d_native_capability_unsupported');
  const uv = { ...shape, uvTexture: true };
  assert.throws(() => validatePixal3dRequest(uv, { backend: 'windows-rocm', nativeCapabilities: { single512: true } }, { multi: false }), (error) => error.code === 'pixal3d_native_not_ready' && error.capability === 'uvTexture1024');
  assert.doesNotThrow(() => validatePixal3dRequest(uv, { backend: 'windows-rocm', nativeCapabilities: { single512: true, uvTexture1024: true } }, { multi: false }));
  assert.throws(() => validatePixal3dRequest({ ...uv, quality: 'standard' }, { backend: 'windows-rocm', nativeCapabilities: { single1024: true, uvTexture1024: true } }, { multi: false }), (error) => error.code === 'pixal3d_texturing_requires_draft');
});

test('native AMD config reads only explicitly verified capabilities from native-ready.json', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { pixal3dConfig } = await import('../server/pixal3d.mjs');
  const appRoot = mkdtempSync(path.join(tmpdir(), 'astra-native-ready-'));
  const runtime = path.join(appRoot, 'runtime', 'ComfyUI-Pixal3D-AMD-Native');
  mkdirSync(runtime, { recursive: true });
  writeFileSync(path.join(runtime, 'native-ready.json'), JSON.stringify({
    capabilities: { single512: true, single1024: 'true', multi512: true },
  }));
  const cfg = pixal3dConfig({ env: { ASTRA_PIXAL3D_BACKEND: 'windows-rocm' }, appRoot });
  const shape = { engine: 'pixal3d', quality: 'draft', vertexColors: false, uvTexture: false };
  assert.doesNotThrow(() => validatePixal3dRequest(shape, cfg, { multi: false }));
  assert.throws(() => validatePixal3dRequest({ ...shape, quality: 'standard' }, cfg, { multi: false }), (error) => error.code === 'pixal3d_native_not_ready');
  assert.equal(cfg.nativeCapabilities.multi512, true);
});
