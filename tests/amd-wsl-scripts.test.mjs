import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

test('AMD WSL scripts exclude CUDA wheels and verify the GPU', () => {
  const install = read('scripts/wsl/install-pixal3d-amd.sh');
  assert.doesNotMatch(install, /download\.pytorch\.org\/whl\/cu|flash_attn-.*win_amd64/);
  assert.match(install, /torch\.version\.hip/);
  assert.match(install, /9070 XT/);
  assert.match(install, /PYTORCH_ROCM_ARCH=gfx1201/);
  assert.match(install, /--no-build-isolation/);
  const verify = read('scripts/wsl/verify_pixal3d_amd.py');
  for (const name of ['cumesh', 'flex_gemm', 'o_voxel']) assert.match(verify, new RegExp(name));
});

test('AMD WSL launcher forwards ComfyUI runtime arguments', () => {
  const start = read('scripts/wsl/start-pixal3d-amd.sh');
  assert.match(start, /ComfyUI\/main\.py/);
  assert.match(start, /--listen 127\.0\.0\.1/);
  assert.match(start, /--disable-auto-launch/);
  assert.match(start, /"\$@"/);
});

test('AMD node patch disables unused NVIDIA-only imports', () => {
  const patch = read('scripts/wsl/patch_trellis2_amd.py');
  assert.match(patch, /nvdiffrast/);
  assert.match(patch, /ASTRAFORGE AMD ROCm PATCH/);
  assert.match(patch, /allow_tf32/);
  assert.match(patch, /ov_float3/);
  assert.match(patch, /ov_int3/);
  assert.match(patch, /ov_int4/);
});

test('CuMesh ROCm patch replaces CUDA-only standard tuple support', () => {
  const patch = read('scripts/wsl/patches/cumesh-rocm.patch');
  assert.match(patch, /rocprim::tuple<int&, int&, int&>/);
});

test('O-Voxel ROCm patch makes its unused nvdiffrast import optional', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, readFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const root = mkdtempSync(join(tmpdir(), 'astraforge-ovoxel-'));
  mkdirSync(join(root, 'src', 'convert'), { recursive: true });
  mkdirSync(join(root, 'o_voxel'));
  writeFileSync(join(root, 'src', 'convert', 'flexible_dual_grid.cpp'), '');
  writeFileSync(join(root, 'o_voxel', 'postprocess.py'), 'import nvdiffrast.torch as dr\n');
  const patch = fileURLToPath(new URL('../scripts/wsl/patch_trellis2_amd.py', import.meta.url));
  const result = spawnSync('python', ['-c', [
    'import importlib.util, sys',
    'spec = importlib.util.spec_from_file_location("patch", sys.argv[1])',
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'module.patch_o_voxel(__import__("pathlib").Path(sys.argv[2]))',
  ].join(';'), patch, root], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const patched = readFileSync(join(root, 'o_voxel', 'postprocess.py'), 'utf8');
  assert.match(patched, /try:.*ASTRAFORGE AMD ROCm PATCH/);
  assert.match(patched, /except ImportError:\r?\n    dr = None/);
});
