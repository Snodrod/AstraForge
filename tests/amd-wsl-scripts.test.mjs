import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const upstreamUvPipeline = [
  'from comfy.utils import ProgressBar',
  "        if hasattr(mesh, 'visual') and hasattr(mesh.visual, 'uv') and mesh.visual.uv is not None:",
  '            uvs = mesh.visual.uv.copy()',
  '        else:',
  '            _cumesh = cumesh.CuMesh()',
  '            _cumesh.init(vertices_torch, faces_torch)',
  "            print('Unwrapping mesh ...')",
  '            vertices_torch, faces_torch, uvs_torch, vmap = _cumesh.uv_unwrap(',
  '                compute_charts_kwargs={',
  '                    "threshold_cone_half_angle_rad": np.radians(mesh_cluster_threshold_cone_half_angle_rad),',
  '                    "refine_iterations": 0,',
  '                    "global_iterations": 1,',
  '                    "smooth_strength": 1,',
  '                },',
  '                return_vmaps=True,',
  '                verbose=True,',
  '            )',
  '            ',
  '            del _cumesh',
  '            gc.collect()      ',
  '            ',
  '            vertices_torch = vertices_torch.cuda()',
  '            faces_torch = faces_torch.cuda()',
  '            uvs_torch = uvs_torch.cuda()',
  '            vertices = vertices_torch.cpu().numpy()',
  '            faces = faces_torch.cpu().numpy()',
  '            uvs = uvs_torch.cpu().numpy()',
  '            normals = normals[vmap.cpu().numpy()]',
  '        if bake_on_vertices:',
  '            pass',
  "        print('Finalizing mesh ...')",
  '        ctx = dr.RasterizeCudaContext()',
  '        uvs_torch = torch.cat([uvs_torch * 2 - 1, torch.zeros_like(uvs_torch[:, :1]), torch.ones_like(uvs_torch[:, :1])], dim=-1).unsqueeze(0)',
  '        rast, _ = dr.rasterize(',
  '            ctx, uvs_torch, faces_torch,',
  '            resolution=[texture_size, texture_size],',
  '        )',
  '        ',
  '        torch.cuda.synchronize()',
  '        ',
  '        mask = rast[0, ..., 3] > 0',
  '        pos = dr.interpolate(vertices_torch.unsqueeze(0), rast, faces_torch)[0][0]',
  '            image_cond_model = self.load_pixal3d_image_cond_tex_1024()',
  '            cond = self.get_proj_cond_shape(',
  '',
].join('\n');

test('AMD WSL scripts exclude CUDA wheels and verify the GPU', () => {
  const install = read('scripts/wsl/install-pixal3d-amd.sh');
  assert.doesNotMatch(install, /download\.pytorch\.org\/whl\/cu|flash_attn-.*win_amd64/);
  assert.match(install, /torch\.version\.hip/);
  assert.match(install, /9070 XT/);
  assert.match(install, /PYTORCH_ROCM_ARCH=gfx1201/);
  assert.match(install, /--no-build-isolation/);
  assert.match(install, /transformers==4\.57\.6/);
  const verify = read('scripts/wsl/verify_pixal3d_amd.py');
  for (const name of ['cumesh', 'flex_gemm', 'o_voxel']) assert.match(verify, new RegExp(name));
});

test('AMD WSL launcher forwards ComfyUI runtime arguments', () => {
  const start = read('scripts/wsl/start-pixal3d-amd.sh');
  assert.match(start, /ComfyUI\/main\.py/);
  assert.match(start, /--listen 127\.0\.0\.1/);
  assert.match(start, /--disable-auto-launch/);
  assert.match(start, /"\$@"/);
  assert.match(start, /HF_HOME="\$ENGINE\/cache\/huggingface"/);
  assert.match(start, /TORCH_HOME="\$ENGINE\/cache\/torch"/);
});

test('AMD node patch disables unused NVIDIA-only imports', () => {
  const patch = read('scripts/wsl/patch_trellis2_amd.py');
  assert.match(patch, /nvdiffrast/);
  assert.match(patch, /ASTRAFORGE AMD ROCm PATCH/);
  assert.match(patch, /allow_tf32/);
  assert.match(patch, /FLEX_GEMM_ALGO = 'implicit_gemm'/);
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

test('AMD precision is applied whenever lazy decoders are loaded', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, readFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const root = mkdtempSync(join(tmpdir(), 'astraforge-lazy-precision-'));
  const pipelineDir = join(root, 'trellis2', 'pipelines');
  mkdirSync(pipelineDir, { recursive: true });
  const pipeline = join(pipelineDir, 'trellis2_image_to_3d.py');
  writeFileSync(pipeline, [
    "            self.models['sparse_structure_decoder'].to(self._device)",
    "            if hasattr(self.models['sparse_structure_decoder'], 'low_vram'):",
    "                self.models['sparse_structure_decoder'].low_vram = self.low_vram",
    "            self.models['shape_slat_decoder'].to(self._device)",
    "            if hasattr(self.models['shape_slat_decoder'], 'low_vram'):",
    "                self.models['shape_slat_decoder'].low_vram = self.low_vram",
    '',
  ].join('\n'));
  const patch = fileURLToPath(new URL('../scripts/wsl/patch_trellis2_amd.py', import.meta.url));
  const result = spawnSync('python', ['-c', [
    'import importlib.util, sys',
    'spec = importlib.util.spec_from_file_location("patch", sys.argv[1])',
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'module.patch_lazy_precision(__import__("pathlib").Path(sys.argv[2]))',
  ].join(';'), patch, root], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const patched = readFileSync(pipeline, 'utf8');
  assert.match(patched, /if os\.environ\.get\('ASTRA_AMD_ROCM'\) == '1':/);
  assert.match(patched, /self\.models\['sparse_structure_decoder'\]\.convert_to_fp32\(\)/);
  assert.match(patched, /decoder = self\.models\['shape_slat_decoder'\]/);
  assert.match(patched, /decoder\.blocks\.float\(\)/);
  assert.match(patched, /decoder\.forward = bf16_safe_forward/);
  assert.equal((patched.match(/def bf16_safe_forward/g) || []).length, 1);
});

test('AMD vertex colors skip the CUDA UV unwrap branch', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, readFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const root = mkdtempSync(join(tmpdir(), 'astraforge-vertex-colors-'));
  const pipelineDir = join(root, 'trellis2', 'pipelines');
  mkdirSync(pipelineDir, { recursive: true });
  const pipeline = join(pipelineDir, 'trellis2_image_to_3d.py');
  writeFileSync(pipeline, upstreamUvPipeline);
  const patch = fileURLToPath(new URL('../scripts/wsl/patch_trellis2_amd.py', import.meta.url));
  const result = spawnSync('python', ['-c', [
    'import importlib.util, sys',
    'spec = importlib.util.spec_from_file_location("patch", sys.argv[1])',
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'module.patch_vertex_colors(__import__("pathlib").Path(sys.argv[2]))',
  ].join(';'), patch, root], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const patched = readFileSync(pipeline, 'utf8');
  assert.match(patched, /if not bake_on_vertices and hasattr\(mesh, 'visual'\)/);
  assert.match(patched, /elif not bake_on_vertices:/);
  assert.match(patched, /image_cond_model\.naf_tile_factor = int\(os\.environ\.get\('ASTRA_AMD_NAF_TILE', '2'\)\)/);
});

test('NAF uses a bounded PyTorch attention fallback without NATTEN', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, readFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const root = mkdtempSync(join(tmpdir(), 'astraforge-naf-'));
  const layers = join(root, 'src', 'layers');
  mkdirSync(layers, { recursive: true });
  const attentions = join(layers, 'attentions.py');
  writeFileSync(attentions, [
    'try:',
    '    NATTEN_RECENT = False',
    '    from natten.functional import na2d_av, na2d_qk',
    'except:',
    '    NATTEN_RECENT = True',
    '    from natten import na2d',
    '',
  ].join('\n'));
  const hubconf = join(root, 'hubconf.py');
  writeFileSync(hubconf, 'dependencies = ["torch", "natten"]\n');
  const patch = fileURLToPath(new URL('../scripts/wsl/patch_trellis2_amd.py', import.meta.url));
  const result = spawnSync('python', ['-c', [
    'import importlib.util, sys',
    'spec = importlib.util.spec_from_file_location("patch", sys.argv[1])',
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'module.patch_naf(__import__("pathlib").Path(sys.argv[2]))',
  ].join(';'), patch, root], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const patched = readFileSync(attentions, 'utf8');
  assert.match(patched, /except ImportError:/);
  assert.match(patched, /def na2d\(/);
  assert.match(patched, /ASTRA_AMD_NAF_ROW_BLOCK/);
  assert.match(patched, /row_block = max\(1, min\(height,/);
  assert.doesNotMatch(patched, /row_block = 1\s/);
  assert.match(patched, /torch\.einsum/);
  assert.doesNotMatch(patched, /^        [kv] = F\.pad\([kv],/m);
  assert.match(patched, /band_start = max\(0, start - pad_h\)/);
  assert.match(patched, /F\.pad\(k\[:, :, band_start:band_end, :\]/);
  assert.doesNotMatch(patched, /except:\r?\n/);
  assert.match(readFileSync(hubconf, 'utf8'), /dependencies = \["torch"\]/);
  const legacy = patched.replace(/^        row_block = .*$/m, '        row_block = 1');
  assert.notEqual(legacy, patched);
  writeFileSync(attentions, legacy);
  const upgrade = spawnSync('python', [patch, '--naf', root], { encoding: 'utf8' });
  assert.equal(upgrade.status, 0, upgrade.stderr);
  const upgraded = readFileSync(attentions, 'utf8');
  assert.match(upgraded, /ASTRA_AMD_NAF_ROW_BLOCK/);
  const previousBand = upgraded.replace(/        pad_h = [\s\S]*?            vb = F\.unfold\(vb, kernel_size=\(kh, kw\), dilation=\(dh, dw\)\)/, [
    '        pad_h = (kh - 1) * dh // 2',
    '        pad_w = (kw - 1) * dw // 2',
    '        k = F.pad(k, (pad_w, pad_w, pad_h, pad_h))',
    '        v = F.pad(v, (pad_w, pad_w, pad_h, pad_h))',
    '        outputs = []',
    '        import os',
    "        row_block = max(1, min(height, int(os.environ.get('ASTRA_AMD_NAF_ROW_BLOCK', '2'))))",
    '        for start in range(0, height, row_block):',
    '            rows = min(row_block, height - start)',
    '            band_height = rows + (kh - 1) * dh',
    '            band = slice(start, start + band_height)',
    '            kb = F.unfold(k[:, :, band, :], kernel_size=(kh, kw), dilation=(dh, dw))',
    '            vb = F.unfold(v[:, :, band, :], kernel_size=(kh, kw), dilation=(dh, dw))',
  ].join('\n'));
  assert.notEqual(previousBand, upgraded);
  writeFileSync(attentions, previousBand);
  const memoryUpgrade = spawnSync('python', [patch, '--naf', root], { encoding: 'utf8' });
  assert.equal(memoryUpgrade.status, 0, memoryUpgrade.stderr);
  assert.doesNotMatch(readFileSync(attentions, 'utf8'), /^        [kv] = F\.pad\([kv],/m);
});

test('NAF verification covers odd shapes and bounded benchmark memory', () => {
  const verify = read('scripts/wsl/verify_naf_amd.py');
  assert.match(verify, /\(\(5, 7\), \(1, 1\)\)/);
  const benchmark = read('scripts/wsl/benchmark_naf_amd.py');
  assert.match(benchmark, /max_memory_allocated/);
  assert.match(benchmark, /ASTRA_AMD_NAF_ROW_BLOCK/);
  assert.match(benchmark, /\[1, 2, 4, 8\]/);
});

test('AMD UV texturing replaces CuMesh unwrap and CUDA rasterization', async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, readFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const root = mkdtempSync(join(tmpdir(), 'astraforge-uv-texture-'));
  const pipelineDir = join(root, 'trellis2', 'pipelines');
  mkdirSync(pipelineDir, { recursive: true });
  const pipeline = join(pipelineDir, 'trellis2_image_to_3d.py');
  writeFileSync(pipeline, upstreamUvPipeline);
  const patch = fileURLToPath(new URL('../scripts/wsl/patch_trellis2_amd.py', import.meta.url));
  const result = spawnSync('python', ['-c', [
    'import importlib.util, sys',
    'spec = importlib.util.spec_from_file_location("patch", sys.argv[1])',
    'module = importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'module.patch_vertex_colors(__import__("pathlib").Path(sys.argv[2]))',
  ].join(';'), patch, root], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const patched = readFileSync(pipeline, 'utf8');
  assert.match(patched, /from comfy_extras\.nodes_mesh_postprocess import _uv_unwrap, _rasterize_uv_barycentric, _interp_vertex_attr/);
  assert.match(patched, /_uv_unwrap\(\s*vertices_torch\.cpu\(\), faces_torch\.cpu\(\)\.long\(\),\s*'adaptive', 1024, 2, 0\.0\)/);
  assert.match(patched, /_rasterize_uv_barycentric\(faces, uvs, texture_size\)/);
  assert.match(patched, /_interp_vertex_attr\(vertices_torch, faces_torch\.long\(\), face_idx, bary, mask\)/);
  assert.match(patched, /AMD UV rasterization produced an empty texture/);
  assert.match(patched, /ctx = dr\.RasterizeCudaContext\(\)/);
  assert.match(patched, /if bake_on_vertices:/);
});
