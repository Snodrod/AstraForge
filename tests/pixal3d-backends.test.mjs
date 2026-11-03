import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { getPixal3DBackend, listPixal3DBackends } from '../server/pixal3d-backends.mjs';
import { pixal3dConfig } from '../server/pixal3d.mjs';

const appRoot = path.resolve('C:/AstraForge-test');

test('Pixal3D backend registry preserves Windows NVIDIA and WSL and adds native AMD', () => {
  assert.deepEqual(listPixal3DBackends().map((item) => item.id), ['windows', 'wsl', 'windows-rocm']);
  assert.equal(getPixal3DBackend('windows').attention, 'flash_attn');
  assert.equal(getPixal3DBackend('wsl').attention, 'sdpa');
  assert.equal(getPixal3DBackend('windows-rocm').attention, 'sdpa');
});

test('native AMD backend has an isolated absolute runtime and an optional explicit override', () => {
  const native = getPixal3DBackend('windows-rocm', {}, appRoot);
  assert.equal(native.runtimeRoot, path.join(appRoot, 'runtime', 'ComfyUI-Pixal3D-AMD-Native'));
  assert.equal(native.pythonPath, path.join(native.runtimeRoot, 'python_embeded', 'python.exe'));
  assert.ok(path.isAbsolute(native.runtimeRoot));
  const custom = getPixal3DBackend('windows-rocm', { ASTRA_PIXAL3D_NATIVE_DIR: 'D:/NativePixal' }, appRoot);
  assert.equal(custom.runtimeRoot, path.resolve('D:/NativePixal'));
  assert.equal(custom.qualityProfile, 'balanced-1024');
  assert.equal(custom.memoryPolicy.minFree1024Bytes, 11 * 1024 ** 3);
});

test('native AMD capabilities fail closed until hardware probes unlock them', () => {
  const native = getPixal3DBackend('windows-rocm', {}, appRoot);
  assert.deepEqual(native.capabilities, {
    single512: false,
    single1024: false,
    multi512: false,
    multi1024: false,
    uvTexture512: false,
    uvTexture1024: false,
  });
  assert.ok(Object.isFrozen(native));
  assert.ok(Object.isFrozen(native.capabilities));
  assert.throws(() => getPixal3DBackend('unknown'), /windows, wsl or windows-rocm/);
});

test('pixal3dConfig selects the native runtime without changing the default backend', () => {
  const base = pixal3dConfig({ env: {}, appRoot });
  assert.equal(base.backend, 'windows');
  assert.equal(base.dir, path.resolve('C:/AstraForge/ComfyUI-Pixal3D'));
  const native = pixal3dConfig({ env: { ASTRA_PIXAL3D_BACKEND: 'windows-rocm' }, appRoot });
  assert.equal(native.backend, 'windows-rocm');
  assert.equal(native.dir, path.join(appRoot, 'runtime', 'ComfyUI-Pixal3D-AMD-Native'));
  assert.equal(native.nativeDir, native.dir);
  assert.throws(() => pixal3dConfig({ env: { ASTRA_PIXAL3D_BACKEND: 'directml' }, appRoot }), /windows, wsl or windows-rocm/);
});
